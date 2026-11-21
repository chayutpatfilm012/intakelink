"""FR-06: review status changes only through an explicit, attributed reviewer
action. Author tests (Solution Design Report §8.4).

Follow the ARRANGE / ACT / ASSERT pattern from the worked example in
tests/test_fr11_cross_matter_guard.py.
"""
from __future__ import annotations

import pytest

from conftest import ATTORNEY, PARALEGAL, make_record, tier2_flag
from intakelink.models import Action, HoldStatus, ProcessingStatus, ReviewStatus
from intakelink.review import ReviewError, review_action, write_flags


def act(record, action, actor, index, audit, reason=None, change=None):
    """Run one reviewer action against the record's current version."""
    return review_action(record, action, actor, index=index, audit=audit,
                         expected_version=record.version, reason=reason,
                         change=change)


# ---------------------------------------------------------------------------
# Case 1 (AUTHOR): the SYSTEM sets hold status, never review status.
#   - make_record(flags=[tier2_flag()], audit=audit)
#   - hold_status == HoldStatus.HELD_FOR_ATTORNEY
#   - review_status is still ReviewStatus.CANDIDATE
#   - record.decisions is empty (no person has acted yet)
# ---------------------------------------------------------------------------
def test_tier2_flag_sets_hold_without_changing_review_status(audit):
    # Arrange: Create a record without flags
    record = make_record(flags=[], audit=audit)

    initial_review_status = record.review_status
    initial_decisions_count = len(record.decisions)
    assert initial_review_status == ReviewStatus.CANDIDATE

    def hold_set_by_system():
        return [entry for entry in audit.for_record(record.record_id)
                if entry.event == "hold_set" and entry.actor_id == "system"]

    holds_logged_before = len(hold_set_by_system())

    # Act: Apply a tier-2 blocking flag
    write_flags(record, [tier2_flag()], audit)

    # Assert: System sets hold status
    assert record.hold_status == HoldStatus.HELD_FOR_ATTORNEY

    # System must not change review status
    assert record.review_status == initial_review_status

    # System must not create a review decision
    assert len(record.decisions) == initial_decisions_count

    # Assert: System records exactly one new hold in the audit log
    assert len(hold_set_by_system()) == holds_logged_before + 1


# ---------------------------------------------------------------------------
# Case 2 (AUTHOR): every decision is attributed.
#   - do a few actions (e.g. escalate with a reason, then ATTORNEY validate)
#   - every item in record.decisions has a non-empty actor_id
#   - hint: `all(d.actor_id for d in record.decisions)`
# ---------------------------------------------------------------------------
def test_every_decision_has_correct_actor_id(index, audit):
    # Arrange
    record = make_record(audit=audit)
    assert len(record.decisions) == 0

    # Act: Paralegal escalates the record
    act(
        record,
        Action.ESCALATE,
        PARALEGAL,
        index,
        audit,
        reason="Requires attorney review"
    )

    # Act: Attorney validates the record
    act(
        record,
        Action.VALIDATE,
        ATTORNEY,
        index,
        audit
    )

    # Assert: Both actions must create decisions
    assert len(record.decisions) == 2

    # Every decision must have a non-empty actor_id
    assert all(d.actor_id for d in record.decisions)

    # Actor IDs must match the actual users in order
    assert [d.actor_id for d in record.decisions] == [
        PARALEGAL.user_id,
        ATTORNEY.user_id
    ]


# ---------------------------------------------------------------------------
# Case 3 (AUTHOR): approved and rejected are final.
#   - approve a record (PARALEGAL validate), then try reject / escalate /
#     validate again -> each raises ReviewError with code "INVALID_TRANSITION"
#   - hint: @pytest.mark.parametrize over the actions, as in the example
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "final_action, expected_status",
    [
        (Action.VALIDATE, ReviewStatus.APPROVED),
        (Action.REJECT, ReviewStatus.REJECTED),
    ],
)
@pytest.mark.parametrize(
    "next_action",
    [
        Action.REJECT,
        Action.ESCALATE,
        Action.VALIDATE,
    ],
)
def test_approved_and_rejected_records_are_final(
    index, audit, final_action, expected_status, next_action
):
    # Arrange: Create a new record
    record = make_record(audit=audit)

    # Paralegal makes a final decision
    act(
        record,
        final_action,
        PARALEGAL,
        index,
        audit,
        reason="Final review decision",
    )

    assert record.review_status == expected_status

    # Save the number of decisions before another action
    decisions_before = len(record.decisions)

    # Act: Attempt another action on a finalized record
    with pytest.raises(ReviewError) as err:
        act(
            record,
            next_action,
            PARALEGAL,
            index,
            audit,
            reason="Attempt to modify finalized record",
        )

    # Assert: The transition must be rejected
    assert err.value.code == "INVALID_TRANSITION"

    # Final status must remain unchanged
    assert record.review_status == expected_status

    # No additional decision should be recorded
    assert len(record.decisions) == decisions_before


# ---------------------------------------------------------------------------
# Case 4 (AUTHOR): a correction waits for the checks to run again.
#   - PARALEGAL correct with change={"document_type": "medical_record"}
#   - processing_status becomes ProcessingStatus.MATCHING
#   - validate is refused with code "GUARD_BLOCKED"
#   - after write_flags(record, [], audit), validate works (APPROVED)
# ---------------------------------------------------------------------------
def test_correction_requires_rechecking_before_validation(index, audit):
    # Arrange: Create a record
    record = make_record(audit=audit)
    assert record.document_type == "medical_bill"

    # Act: Paralegal corrects the document type
    act(
        record,
        Action.CORRECT,
        PARALEGAL,
        index,
        audit,
        reason="Document is a medical record",
        change={"document_type": "medical_record"},
    )

    # Assert: Correction requires matching to run again
    assert record.document_type == "medical_record"
    assert record.processing_status == ProcessingStatus.MATCHING

    # Validate must be blocked before checks are rerun
    with pytest.raises(ReviewError) as err:
        act(
            record,
            Action.VALIDATE,
            PARALEGAL,
            index,
            audit,
        )

    assert err.value.code == "GUARD_BLOCKED"

    # Act: Rerun checks
    write_flags(record, [], audit)

    # Act: Validate after checks are completed
    act(
        record,
        Action.VALIDATE,
        PARALEGAL,
        index,
        audit,
    )

    # Assert: Validation succeeds
    assert record.review_status == ReviewStatus.APPROVED
