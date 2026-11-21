"""Review rules: the only path that changes review status (C8 + C9).

Implements §7.5 (approval guard), §7.6 (state transitions) and the I6
contract `reviewAction(recordId, action, change, reason, actor, expectedVersion)`.

Design rules enforced here:
  * Review status changes only through an attributed reviewer action (FR-06).
  * The system may set hold status, never review status.
  * A tier-2 hold can be released only by the matter's responsible attorney.
  * Every refused request is written to the audit log.
"""
from __future__ import annotations

from typing import Any, Optional

from .access import (MatterIndex, can_handle_escalation, is_on_matter,
                     is_responsible_attorney)
from .audit import AuditLog
from .models import (Action, Actor, CandidateRecord, Decision, Flag, FlagType,
                     HoldStatus, Origin, ProcessingStatus, ReviewStatus, Role,
                     utc_now)

REASON_REQUIRED_FOR = {Action.REJECT, Action.ESCALATE, Action.OVERRIDE_GUARD,
                       Action.RELEASE_HOLD}


class ReviewError(Exception):
    """Raised when a reviewer action is refused. `code` matches interface I6."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# System-side writes (no person involved): flags and hold status only.
# ---------------------------------------------------------------------------
def write_flags(record: CandidateRecord, flags: list[Flag], audit: AuditLog,
                system_id: str = "system") -> None:
    """Replace the record's computed flags after matching/checks (I5).

    Sets hold status for a tier-2 risk and marks the record ready for review.
    Never touches review status.
    """
    if record.selected_matter_id:
        # The reviewer already chose one matter with a reason; do not
        # re-raise the ambiguity the reviewer resolved.
        flags = [f for f in flags if f.type != FlagType.AMBIGUOUS_MATCH]
    record.flags = flags
    if any(f.type == FlagType.CROSS_MATTER_RISK and not f.cleared for f in flags):
        if record.hold_status != HoldStatus.HELD_FOR_ATTORNEY:
            record.hold_status = HoldStatus.HELD_FOR_ATTORNEY
            record.hold_released_by = None
            audit.append(system_id, "hold_set", record.record_id,
                         detail="tier-2 cross-matter risk")
    record.processing_status = ProcessingStatus.READY_FOR_REVIEW
    record.version += 1
    audit.append(system_id, "flags_written", record.record_id,
                 detail=", ".join(f.type.value for f in flags) or "none")


# ---------------------------------------------------------------------------
# Reviewer actions (I6)
# ---------------------------------------------------------------------------
def review_action(record: CandidateRecord, action: Action, actor: Actor, *,
                  index: MatterIndex, audit: AuditLog, expected_version: int,
                  reason: Optional[str] = None,
                  change: Optional[dict[str, Any]] = None) -> CandidateRecord:
    try:
        _check(record, action, actor, index, expected_version, reason, change)
    except ReviewError as err:
        audit.append(actor.user_id, action.value, record.record_id,
                     outcome="denied", detail=f"{err.code}: {err.message}")
        raise
    _apply(record, action, actor, reason, change or {})
    audit.append(actor.user_id, action.value, record.record_id,
                 detail=reason or "")
    return record


def _deny(code: str, message: str) -> None:
    raise ReviewError(code, message)


def _check(record: CandidateRecord, action: Action, actor: Actor,
           index: MatterIndex, expected_version: int,
           reason: Optional[str], change: Optional[dict[str, Any]]) -> None:
    if expected_version != record.version:
        _deny("STALE_VERSION", f"record is at version {record.version}")
    if record.is_final:
        _deny("INVALID_TRANSITION", f"record is already {record.review_status.value}")
    if action in REASON_REQUIRED_FOR and not (reason and reason.strip()):
        _deny("REASON_REQUIRED", f"{action.value} needs a written reason")

    matter = index.get(record.target_matter_id())

    # Access control (X1). A record with no matter yet is in the intake queue.
    if matter is not None and not is_on_matter(actor, matter):
        _deny("NOT_AUTHORIZED", "actor is not assigned to this matter")

    # Tier-2 hold: attorney-only, and only release_hold while it is active.
    if record.hold_status == HoldStatus.HELD_FOR_ATTORNEY:
        if action != Action.RELEASE_HOLD:
            _deny("GUARD_BLOCKED", "record is held for the responsible attorney")
        if not is_responsible_attorney(actor, matter):
            _deny("NOT_AUTHORIZED", "only the responsible attorney can release this hold")
        return
    if action == Action.RELEASE_HOLD:
        _deny("INVALID_TRANSITION", "record has no active hold")

    # After a tier-2 release, the decision stays with the responsible attorney.
    if record.hold_released_by and action in (Action.VALIDATE, Action.REJECT):
        if not is_responsible_attorney(actor, matter):
            _deny("NOT_AUTHORIZED", "a released tier-2 record is decided by the attorney")

    status = record.review_status
    if status == ReviewStatus.ESCALATED:
        if action not in (Action.VALIDATE, Action.REJECT, Action.OVERRIDE_GUARD):
            _deny("INVALID_TRANSITION", f"cannot {action.value} an escalated record")
        if not can_handle_escalation(actor, matter):
            _deny("NOT_AUTHORIZED", "only the escalation owner can decide this record")

    if action == Action.VALIDATE:
        if record.processing_status != ProcessingStatus.READY_FOR_REVIEW:
            _deny("GUARD_BLOCKED", "checks have not finished for this record")
        if record.target_matter_id() is None:
            _deny("GUARD_BLOCKED", "no matter selected")
        if record.open_blocking_flags():
            names = ", ".join(f.type.value for f in record.open_blocking_flags())
            _deny("GUARD_BLOCKED", f"open blocking flags: {names}")

    if action == Action.OVERRIDE_GUARD:
        if record.has_open(FlagType.CROSS_MATTER_RISK):
            _deny("GUARD_BLOCKED", "a tier-2 risk cannot be overridden")
        if not record.has_open(FlagType.CROSS_MATTER_NAME):
            _deny("INVALID_TRANSITION", "no tier-1 flag to override")

    if action == Action.CORRECT:
        if not change:
            _deny("INVALID_TRANSITION", "a correction needs a change")
        new_matter = change.get("selected_matter_id")
        if new_matter is not None:
            target = index.get(new_matter)
            if target is None:
                _deny("INVALID_TRANSITION", f"unknown matter {new_matter}")
            if not is_on_matter(actor, target):
                _deny("NOT_AUTHORIZED", "actor is not assigned to the new matter")
            if record.has_open(FlagType.AMBIGUOUS_MATCH) and not (reason and reason.strip()):
                _deny("REASON_REQUIRED", "name the evidence that decides an ambiguous match")


def _apply(record: CandidateRecord, action: Action, actor: Actor,
           reason: Optional[str], change: dict[str, Any]) -> None:
    prior: Any = record.review_status.value
    new: Any = prior

    if action == Action.VALIDATE:
        if record.selected_matter_id is None:
            record.selected_matter_id = record.target_matter_id()
        record.review_status = ReviewStatus.APPROVED
    elif action == Action.REJECT:
        record.review_status = ReviewStatus.REJECTED
    elif action == Action.ESCALATE:
        record.review_status = ReviewStatus.ESCALATED
    elif action == Action.OVERRIDE_GUARD:
        for f in record.flags:
            if f.type == FlagType.CROSS_MATTER_NAME:
                f.cleared = True
        prior, new = "tier-1 flag open", "tier-1 flag cleared"
    elif action == Action.RELEASE_HOLD:
        record.hold_status = HoldStatus.NONE
        record.hold_released_by = actor.user_id
        for f in record.flags:
            if f.type == FlagType.CROSS_MATTER_RISK:
                f.cleared = True
        prior, new = HoldStatus.HELD_FOR_ATTORNEY.value, HoldStatus.NONE.value
    elif action == Action.CORRECT:
        prior, new = _apply_correction(record, actor, change, reason)
        record.review_status = ReviewStatus.CORRECTED

    if action in (Action.VALIDATE, Action.REJECT, Action.ESCALATE):
        new = record.review_status.value

    record.decisions.append(Decision(actor.user_id, action, prior, new, reason, utc_now()))
    record.version += 1


def _apply_correction(record: CandidateRecord, actor: Actor,
                      change: dict[str, Any], reason: Optional[str]) -> tuple[Any, Any]:
    prior: dict[str, Any] = {}
    new: dict[str, Any] = {}

    if "selected_matter_id" in change:
        prior["selected_matter_id"] = record.selected_matter_id
        record.selected_matter_id = change["selected_matter_id"]
        new["selected_matter_id"] = record.selected_matter_id
        if reason:
            for f in record.flags:
                if f.type == FlagType.AMBIGUOUS_MATCH:
                    f.cleared = True

    if "document_type" in change:
        prior["document_type"] = record.document_type
        record.document_type = change["document_type"]
        new["document_type"] = record.document_type

    for fact_id, value in change.get("facts", {}).items():
        fact = next((f for f in record.facts if f.fact_id == fact_id), None)
        if fact is None:
            continue
        prior[fact_id] = fact.value
        fact.value = value
        fact.origin = Origin.STAFF
        fact.entered_by = actor.user_id
        new[fact_id] = value

    # Any correction re-runs matching and checks before approval (§7.5).
    record.processing_status = ProcessingStatus.MATCHING
    return prior, new


__all__ = ["ReviewError", "review_action", "write_flags", "Role"]
