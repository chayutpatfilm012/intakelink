"""FR-05: the reviewer can validate, correct, reject, or escalate,
and each action produces the correct record state."""
from __future__ import annotations

import pytest

from conftest import ATTORNEY, PARALEGAL, SENIOR, make_record
from intakelink.models import (Action, Flag, FlagType, Origin, ProcessingStatus,
                               ReviewStatus)
from intakelink.review import ReviewError, review_action, write_flags


def act(record, action, actor, index, audit, **kw):
    return review_action(record, action, actor, index=index, audit=audit,
                         expected_version=record.version, **kw)


def test_validate_approves_and_selects_top_matter(record, index, audit):
    act(record, Action.VALIDATE, PARALEGAL, index, audit)
    assert record.review_status == ReviewStatus.APPROVED
    assert record.selected_matter_id == "M-1042"


def test_reject_needs_reason_and_is_final(record, index, audit):
    with pytest.raises(ReviewError) as err:
        act(record, Action.REJECT, PARALEGAL, index, audit)
    assert err.value.code == "REASON_REQUIRED"
    act(record, Action.REJECT, PARALEGAL, index, audit, reason="Not our client's bill")
    assert record.review_status == ReviewStatus.REJECTED
    with pytest.raises(ReviewError) as err:
        act(record, Action.VALIDATE, PARALEGAL, index, audit)
    assert err.value.code == "INVALID_TRANSITION"


def test_escalate_then_owner_decides(record, index, audit):
    act(record, Action.ESCALATE, PARALEGAL, index, audit, reason="Unsure which claim")
    assert record.review_status == ReviewStatus.ESCALATED
    with pytest.raises(ReviewError) as err:
        act(record, Action.VALIDATE, PARALEGAL, index, audit)
    assert err.value.code == "NOT_AUTHORIZED"
    act(record, Action.VALIDATE, SENIOR, index, audit)
    assert record.review_status == ReviewStatus.APPROVED


def test_correct_fact_marks_staff_and_waits_for_recheck(record, index, audit):
    act(record, Action.CORRECT, PARALEGAL, index, audit,
        change={"facts": {"F-1": "CLM-55812"}})
    fact = record.facts[0]
    assert record.review_status == ReviewStatus.CORRECTED
    assert (fact.value, fact.origin, fact.entered_by) == ("CLM-55812", Origin.STAFF, "p.lee")
    assert record.decisions[-1].prior == {"F-1": "CLM-55821"}
    # Approval waits until matching and checks run again.
    assert record.processing_status == ProcessingStatus.MATCHING
    with pytest.raises(ReviewError) as err:
        act(record, Action.VALIDATE, PARALEGAL, index, audit)
    assert err.value.code == "GUARD_BLOCKED"
    write_flags(record, [], audit)
    act(record, Action.VALIDATE, PARALEGAL, index, audit)
    assert record.review_status == ReviewStatus.APPROVED


def test_ambiguous_match_blocks_until_matter_chosen_with_reason(index, audit):
    record = make_record(audit=audit, flags=[Flag(FlagType.AMBIGUOUS_MATCH, True, "0.70 vs 0.60")])
    with pytest.raises(ReviewError) as err:
        act(record, Action.VALIDATE, PARALEGAL, index, audit)
    assert err.value.code == "GUARD_BLOCKED"
    with pytest.raises(ReviewError) as err:
        act(record, Action.CORRECT, PARALEGAL, index, audit,
            change={"selected_matter_id": "M-1042"})
    assert err.value.code == "REASON_REQUIRED"
    act(record, Action.CORRECT, PARALEGAL, index, audit,
        change={"selected_matter_id": "M-1042"},
        reason="Date of birth on page 1 matches M-1042")
    write_flags(record, [Flag(FlagType.AMBIGUOUS_MATCH, True, "0.70 vs 0.60")], audit)
    assert not record.has_open(FlagType.AMBIGUOUS_MATCH)
    act(record, Action.VALIDATE, PARALEGAL, index, audit)
    assert record.review_status == ReviewStatus.APPROVED


def test_stale_version_is_refused(record, index, audit):
    old = record.version
    act(record, Action.CORRECT, PARALEGAL, index, audit, change={"document_type": "medical_record"})
    with pytest.raises(ReviewError) as err:
        review_action(record, Action.VALIDATE, ATTORNEY, index=index, audit=audit,
                      expected_version=old)
    assert err.value.code == "STALE_VERSION"
