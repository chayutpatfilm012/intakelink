# Tests the author writes (FR-06 and FR-11)

Solution Design Report §8.4 says the tests for FR-06 and FR-11 are written by
the author, not generated, because they prove the core safety claims.
Write them in `tests/test_fr06_status_integrity.py` and
`tests/test_fr11_cross_matter_guard.py`, using the fixtures in `tests/conftest.py`
(`make_record`, `tier1_flag`, `tier2_flag`, `PARALEGAL`, `SENIOR`, `ATTORNEY`,
`OTHER_ATTORNEY`).

Each case below is a behavior the code already supports. Your test should
fail if that behavior ever breaks.

## FR-06: review status changes only through a reviewer action

1. `write_flags` with a tier-2 flag sets `hold_status` to `HELD_FOR_ATTORNEY`
   but leaves `review_status` as `candidate`.
2. Every entry in `record.decisions` has a non-empty `actor_id`.
3. Once a record is `approved` or `rejected`, any further action raises
   `ReviewError` with code `INVALID_TRANSITION`.
4. A correction sets `processing_status` to `MATCHING`, and validate is refused
   (`GUARD_BLOCKED`) until `write_flags` runs again.

## FR-11: Cross-Matter Guard

5. Tier 2: a paralegal, a senior paralegal, and another matter's attorney each
   try `validate`, `override_guard`, and `release_hold`. Every attempt is
   refused, and `audit.denials()` grows by one each time.
6. Tier 2: only the responsible attorney (`ATTORNEY`, for M-1042) can
   `release_hold`, and it needs a reason.
7. Tier 2: after the release, a paralegal still cannot validate; the attorney can.
8. Tier 1: validate is refused until `override_guard` is recorded with a reason;
   `override_guard` without a reason is refused with `REASON_REQUIRED`.
9. Tier 1: `override_guard` is refused when a tier-2 flag is also open.

Run with `pytest -q`. Record in `docs/AI_ASSISTANCE.md` that you wrote these.
