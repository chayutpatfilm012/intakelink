"""FR-11: Cross-Matter Guard. Author tests (Solution Design Report §8.4).

The first test (case 5) is a worked example drafted by AI to show the pattern.
Cases 6-9 are written by the author.

Pattern for every test:
  1. ARRANGE  - build a record with make_record(...) and an AuditLog
  2. ACT      - call review_action(...) (or the helper `act` below)
  3. ASSERT   - check the result, the error code, and the audit log
"""
from __future__ import annotations

import pytest

from conftest import (ATTORNEY, OTHER_ATTORNEY, PARALEGAL, SENIOR, make_record,
                      tier1_flag, tier2_flag)
from intakelink.models import Action, FlagType, HoldStatus, ReviewStatus
from intakelink.review import ReviewError, review_action


def act(record, action, actor, index, audit, reason=None):
    """Run one reviewer action against the record's current version."""
    return review_action(record, action, actor, index=index, audit=audit,
                         expected_version=record.version, reason=reason)


# ---------------------------------------------------------------------------
# Case 5 (WORKED EXAMPLE, drafted by AI):
# Tier 2: paralegal, senior paralegal, and another matter's attorney each try
# validate, override_guard and release_hold. Every attempt is refused, and the
# audit log gets one "denied" entry per attempt.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("actor", [PARALEGAL, SENIOR, OTHER_ATTORNEY])
@pytest.mark.parametrize("action", [Action.VALIDATE, Action.OVERRIDE_GUARD,
                                    Action.RELEASE_HOLD])
def test_tier2_hold_refuses_everyone_but_the_responsible_attorney(index, audit, actor, action):
    # ARRANGE: a record for M-1042 with a tier-2 flag -> the system holds it.
    record = make_record(flags=[tier2_flag()], audit=audit)
    assert record.hold_status == HoldStatus.HELD_FOR_ATTORNEY
    denials_before = len(audit.denials())

    # ACT + ASSERT: the action is refused with one of the two expected codes.
    with pytest.raises(ReviewError) as err:
        act(record, action, actor, index, audit, reason="trying anyway")
    assert err.value.code in ("GUARD_BLOCKED", "NOT_AUTHORIZED")

    # ASSERT: nothing changed, and the refusal was logged.
    assert record.hold_status == HoldStatus.HELD_FOR_ATTORNEY
    assert record.review_status == ReviewStatus.CANDIDATE
    assert len(audit.denials()) == denials_before + 1
    assert audit.denials()[-1].actor_id == actor.user_id


# ---------------------------------------------------------------------------
# Case 6 (AUTHOR): Tier 2 - only the responsible attorney (ATTORNEY, a.reyes,
# for M-1042) can release_hold, and it needs a reason.
#   - release_hold WITHOUT a reason -> ReviewError, code "REASON_REQUIRED"
#   - release_hold WITH a reason    -> hold_status becomes HoldStatus.NONE
#   - hint: also check record.hold_released_by == ATTORNEY.user_id
# ---------------------------------------------------------------------------

def test_tier2_hold_release_requires_reason_and_responsible_attorney(
    index, audit
):
    # Arrange: Create a record with a Tier-2 blocking flag
    record = make_record(flags=[tier2_flag()], audit=audit)

    assert record.hold_status == HoldStatus.HELD_FOR_ATTORNEY

    # Act: Attorney attempts to release hold without a reason
    with pytest.raises(ReviewError) as err:
        act(
            record,
            Action.RELEASE_HOLD,
            ATTORNEY,
            index,
            audit,
        )

    # Assert: Reason is required
    assert err.value.code == "REASON_REQUIRED"
    assert record.hold_status == HoldStatus.HELD_FOR_ATTORNEY

    # Act: Responsible attorney releases hold with a reason
    act(
        record,
        Action.RELEASE_HOLD,
        ATTORNEY,
        index,
        audit,
        reason="Reviewed and cleared the Tier-2 risk",
    )

    # Assert: Hold is released
    assert record.hold_status == HoldStatus.NONE

    # Assert: Correct attorney is recorded
    assert record.hold_released_by == ATTORNEY.user_id


# ---------------------------------------------------------------------------
# Case 7 (AUTHOR): Tier 2 - after the attorney releases the hold, a PARALEGAL
# still cannot validate (code "NOT_AUTHORIZED"), but the ATTORNEY can
# (review_status becomes ReviewStatus.APPROVED).
# ---------------------------------------------------------------------------

def test_tier2_after_hold_release_only_attorney_can_validate(index, audit):
    # Arrange: Create a record with a Tier-2 blocking flag
    record = make_record(flags=[tier2_flag()], audit=audit)

    assert record.hold_status == HoldStatus.HELD_FOR_ATTORNEY
    assert record.review_status == ReviewStatus.CANDIDATE

    # Attorney releases the hold with a reason
    act(
        record,
        Action.RELEASE_HOLD,
        ATTORNEY,
        index,
        audit,
        reason="Tier-2 risk reviewed and cleared",
    )

    assert record.hold_status == HoldStatus.NONE
    assert record.hold_released_by == ATTORNEY.user_id

    # Act: Paralegal attempts to validate
    with pytest.raises(ReviewError) as err:
        act(
            record,
            Action.VALIDATE,
            PARALEGAL,
            index,
            audit,
        )

    # Assert: Paralegal is not authorized
    assert err.value.code == "NOT_AUTHORIZED"
    assert record.review_status == ReviewStatus.CANDIDATE

    # Act: Attorney validates the record
    act(
        record,
        Action.VALIDATE,
        ATTORNEY,
        index,
        audit,
    )

    # Assert: Attorney validation succeeds
    assert record.review_status == ReviewStatus.APPROVED


# ---------------------------------------------------------------------------
# Case 8 (AUTHOR): Tier 1 (use tier1_flag()) -
#   - PARALEGAL validate is refused, code "GUARD_BLOCKED"
#   - override_guard without a reason is refused, code "REASON_REQUIRED"
#   - override_guard with a reason works, then validate gives APPROVED
#   - hint: a tier-1 record is NOT held: hold_status stays HoldStatus.NONE
# ---------------------------------------------------------------------------

def test_tier1_guard_requires_override_before_validation(index, audit):
    # Arrange: Create a record with a Tier-1 flag
    record = make_record(flags=[tier1_flag()], audit=audit)

    assert record.hold_status == HoldStatus.NONE
    assert record.review_status == ReviewStatus.CANDIDATE

    # Act: Paralegal attempts to validate before override
    with pytest.raises(ReviewError) as err:
        act(
            record,
            Action.VALIDATE,
            PARALEGAL,
            index,
            audit,
        )

    # Assert: Validation is blocked by Tier-1 guard
    assert err.value.code == "GUARD_BLOCKED"
    assert record.review_status == ReviewStatus.CANDIDATE

    # Act: Paralegal attempts override without a reason
    with pytest.raises(ReviewError) as err:
        act(
            record,
            Action.OVERRIDE_GUARD,
            PARALEGAL,
            index,
            audit,
        )

    # Assert: Override requires a reason
    assert err.value.code == "REASON_REQUIRED"

    # Act: Paralegal overrides the guard with a reason
    act(
        record,
        Action.OVERRIDE_GUARD,
        PARALEGAL,
        index,
        audit,
        reason="Tier-1 risk reviewed and accepted",
    )

    # Assert: Tier-1 does not create a hold
    assert record.hold_status == HoldStatus.NONE

    # Act: Paralegal validates after override
    act(
        record,
        Action.VALIDATE,
        PARALEGAL,
        index,
        audit,
    )

    # Assert: Validation succeeds
    assert record.review_status == ReviewStatus.APPROVED
    assert record.hold_status == HoldStatus.NONE


# ---------------------------------------------------------------------------
# Case 9 (AUTHOR): override_guard is refused (code "GUARD_BLOCKED") when a
# tier-2 flag is open too. Use make_record(flags=[tier1_flag(), tier2_flag()]).
# Which actor? Try the ATTORNEY: even the responsible attorney cannot use
# override_guard on a tier-2 record (they must use release_hold).
# ---------------------------------------------------------------------------

def test_tier2_blocks_guard_override_even_for_attorney(index, audit):
    # Arrange: Create a record with both Tier-1 and Tier-2 flags
    record = make_record(
        flags=[tier1_flag(), tier2_flag()],
        audit=audit,
    )

    assert record.hold_status == HoldStatus.HELD_FOR_ATTORNEY
    assert record.review_status == ReviewStatus.CANDIDATE

    # Save initial state
    decisions_before = len(record.decisions)

    # Act: Responsible attorney attempts to override the guard
    with pytest.raises(ReviewError) as err:
        act(
            record,
            Action.OVERRIDE_GUARD,
            ATTORNEY,
            index,
            audit,
            reason="Attempt to override while Tier-2 flag is open",
        )

    # Assert: Tier-2 blocks guard override. The refusal comes from the active
    # hold (checked first in review._check), not from the override rule itself;
    # see the next test for that rule.
    assert err.value.code == "GUARD_BLOCKED"
    assert "held for the responsible attorney" in err.value.message

    # Assert: Record remains unchanged
    assert record.hold_status == HoldStatus.HELD_FOR_ATTORNEY
    assert record.review_status == ReviewStatus.CANDIDATE

    # No decision should be created for a denied action
    assert len(record.decisions) == decisions_before


# ---------------------------------------------------------------------------
# Case 9b (DRAFTED BY AI, defense in depth):
# The override rule itself refuses a tier-2 risk. In normal use this rule is
# never reached: an open tier-2 flag always comes with an active hold, and
# release_hold clears the tier-2 flag. So the test arranges a state the system
# does not produce (tier-2 flag open, hold cleared) to prove the second line of
# defense holds on its own.
# ---------------------------------------------------------------------------
def test_override_rule_refuses_tier2_even_without_a_hold(index, audit):
    # ARRANGE: both flags open, hold removed by hand (test setup only).
    record = make_record(flags=[tier1_flag(), tier2_flag()], audit=audit)
    record.hold_status = HoldStatus.NONE
    assert record.has_open(FlagType.CROSS_MATTER_RISK)
    decisions_before = len(record.decisions)

    # ACT: the responsible attorney tries override_guard with a reason.
    with pytest.raises(ReviewError) as err:
        act(record, Action.OVERRIDE_GUARD, ATTORNEY, index, audit,
            reason="trying to clear both flags at once")

    # ASSERT: refused by the override rule, and nothing was cleared.
    assert err.value.code == "GUARD_BLOCKED"
    assert "tier-2 risk cannot be overridden" in err.value.message
    assert record.has_open(FlagType.CROSS_MATTER_NAME)
    assert record.has_open(FlagType.CROSS_MATTER_RISK)
    assert len(record.decisions) == decisions_before

