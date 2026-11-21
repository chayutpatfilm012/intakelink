"""IntakeLink service: wires C1-C10 together for the Workbench and the demo.

Pipeline for each received file:
    intake (C1-C3) -> extraction (C4) -> matching + guard + duplicates (C5-C7)
    -> READY_FOR_REVIEW, or EXTRACTION_FAILED

Reviewer actions go through ``review.review_action`` only. After a correction
the service re-runs the checks; after an approval it files the record (C10).
The service never sets review status itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .access import MatterIndex, can_handle_intake, is_on_matter
from .audit import AuditLog
from .extraction import Extractor, RuleBasedExtractor, extract_into
from .filing import INTAKE_QUEUE, FilingReceipt, MockCaseManagementSystem, Notifier
from .guard import PriorDocument, run_checks
from .intake import DocumentStore, Intake, PreparedText
from .models import (Action, Actor, CandidateRecord, HoldStatus, ProcessingStatus,
                     ReviewStatus)
from .review import ReviewError, review_action


@dataclass
class RecordView:
    record: CandidateRecord
    text: Optional[PreparedText]
    receipt: Optional[FilingReceipt]


class IntakeLinkService:
    def __init__(self, workdir: Path, index: MatterIndex, *,
                 extractor: Optional[Extractor] = None,
                 audit_path: Optional[Path] = None) -> None:
        workdir = Path(workdir)
        self.index = index
        self.audit = AuditLog(audit_path)
        self.store = DocumentStore(workdir / "originals")
        self.intake = Intake(self.store, self.audit)
        self.extractor = extractor or RuleBasedExtractor()
        self.cms = MockCaseManagementSystem()
        self.notifier = Notifier()
        self.records: dict[str, CandidateRecord] = {}
        self.texts: dict[str, Optional[PreparedText]] = {}
        self._order: list[str] = []

    # -- pipeline ------------------------------------------------------------
    def receive(self, data: bytes, filename: str, channel: str,
                received_at: Optional[datetime] = None) -> CandidateRecord:
        result = self.intake.receive(data, filename, channel, received_at)
        record = result.record
        self.records[record.record_id] = record
        self.texts[record.record_id] = result.text
        self._order.append(record.record_id)
        extract_into(record, result.text, self.extractor, self.audit)
        if record.processing_status == ProcessingStatus.EXTRACTION_FAILED:
            self.notifier.send(INTAKE_QUEUE, "Document could not be read",
                               f"{record.record_id} ({filename}) needs a person: "
                               f"{result.error or 'extraction failed'}.",
                               record.record_id, self.audit)
            return record
        self._check(record)
        return record

    def _history_before(self, record_id: str) -> list[PriorDocument]:
        earlier = self._order[: self._order.index(record_id)]
        return [PriorDocument.from_record(self.records[r], self.texts[r]) for r in earlier
                if self.texts[r] is not None]

    def _check(self, record: CandidateRecord) -> None:
        was_held = record.hold_status == HoldStatus.HELD_FOR_ATTORNEY
        run_checks(record, self.texts[record.record_id], self.index,
                   self._history_before(record.record_id), self.audit)
        if record.hold_status == HoldStatus.HELD_FOR_ATTORNEY and not was_held:
            matter = self.index.get(record.target_matter_id())
            if matter:
                self.notifier.send(matter.responsible_attorney, "Document held for your review",
                                   f"{record.record_id} may contain another client's information "
                                   f"and is held until you release it.",
                                   record.record_id, self.audit)

    # -- reviewer side ----------------------------------------------------------
    def act(self, record_id: str, action: Action, actor: Actor, *, expected_version: int,
            reason: Optional[str] = None,
            change: Optional[dict[str, Any]] = None) -> CandidateRecord:
        record = self.records[record_id]
        review_action(record, action, actor, index=self.index, audit=self.audit,
                      expected_version=expected_version, reason=reason, change=change)
        if action == Action.CORRECT:
            self._check(record)           # any correction re-runs the checks
        if record.review_status == ReviewStatus.APPROVED:
            receipt = self.cms.file(record, self.audit)
            matter = self.index.get(receipt.matter_id)
            for user in sorted(set(matter.assigned_staff) | {matter.responsible_attorney}):
                self.notifier.send(user, "Document filed",
                                   f"{record_id} ({record.document_type}) filed to "
                                   f"{receipt.matter_id} as {receipt.cms_ref}.",
                                   record_id, self.audit)
        return record

    def can_view(self, actor: Actor, record: CandidateRecord) -> bool:
        """On the record's matter; a record with no matter only for intake staff."""
        matter = self.index.get(record.target_matter_id())
        return can_handle_intake(actor) if matter is None else is_on_matter(actor, matter)

    def view(self, record_id: str, actor: Actor, *, log: bool = True) -> RecordView:
        """Open a record. Refusals are always logged; ``log=False`` skips the
        success entry (the Workbench logs one per opened record, not per redraw)."""
        record = self.records[record_id]
        if not self.can_view(actor, record):
            self.audit.append(actor.user_id, "view", record_id, outcome="denied",
                              detail="NOT_AUTHORIZED: actor is not assigned to this matter")
            raise ReviewError("NOT_AUTHORIZED", "actor is not assigned to this matter")
        if log:
            self.audit.append(actor.user_id, "view", record_id)
        return RecordView(record, self.texts[record_id], self.cms.receipt_for(record_id))

    def visible_records(self, actor: Actor) -> list[CandidateRecord]:
        return [self.records[r] for r in self._order if self.can_view(actor, self.records[r])]

    def matters_for(self, actor: Actor) -> list[str]:
        return [m.matter_id for m in self.index.all() if is_on_matter(actor, m)]


__all__ = ["IntakeLinkService", "RecordView"]
