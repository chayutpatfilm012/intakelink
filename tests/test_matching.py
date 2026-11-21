"""Slice 3: Evidence Matcher (C6) rules, thresholds and outcome flags."""
from __future__ import annotations

import pytest

from conftest import PARALEGAL, make_record
from intakelink.access import MatterIndex
from intakelink.audit import AuditLog
from intakelink.matching import (match, norm_date, norm_name, norm_org,
                                 run_matching, score_matter)
from intakelink.models import (Action, Fact, FlagType, Matter, ProcessingStatus,
                               ReviewStatus)
from intakelink.review import ReviewError, review_action, write_flags

DANA = Matter("M-1042", "Dana Whitfield", "1984-03-12", ["CLM-55821"], "2026-06-14",
              responsible_attorney="a.reyes", assigned_staff=["p.lee"],
              insurers=["Harborline Mutual"], providers=["Bayview Orthopedics"])
MARCUS = Matter("M-2077", "Marcus Bell", "1979-11-02", ["CLM-60310"], "2026-05-02",
                responsible_attorney="a.khan", assigned_staff=["p.lee"],
                insurers=["Keystone Casualty"], providers=["Northgate Imaging"])


def facts(*pairs: tuple[str, str], page: int = 1) -> list[Fact]:
    return [Fact(f"F-{i}", fld, val, page=page, confidence=0.9)
            for i, (fld, val) in enumerate(pairs, start=1)]


@pytest.fixture
def two() -> MatterIndex:
    return MatterIndex([DANA, MARCUS])


# --- each rule and its weight ----------------------------------------------
@pytest.mark.parametrize("pairs, score, rules", [
    ([("matterNumber", "M-1042")], 1.0, ["matter_number"]),
    ([("claimNumber", "CLM-55821")], 0.6, ["claim_number"]),
    ([("clientName", "Dana Whitfield"), ("dateOfBirth", "03/12/1984")], 0.5, ["name_dob", "name_dob"]),
    ([("clientName", "Dana Whitfield")], 0.2, ["name_only"]),
    ([("clientName", "Dana Whitfield"), ("dateOfBirth", "01/01/1990")], 0.2, ["name_only"]),
    ([("incidentDate", "June 14, 2026")], 0.2, ["incident_date"]),
    ([("insurer", "Harborline Mutual")], 0.1, ["insurer_provider"]),
    ([("provider", "Bayview Orthopedics")], 0.1, ["insurer_provider"]),
    ([("dateOfBirth", "03/12/1984")], 0.0, []),          # DOB alone earns nothing
])
def test_rule_weights(pairs, score, rules):
    p = score_matter(facts(*pairs), DANA)
    assert p.score == score
    assert [e.rule for e in p.evidence] == rules


def test_score_is_capped_and_every_point_has_evidence():
    p = score_matter(facts(("matterNumber", "M-1042"), ("claimNumber", "CLM-55821"),
                           ("clientName", "Dana Whitfield"), ("dateOfBirth", "1984-03-12"),
                           ("incidentDate", "2026-06-14"), ("insurer", "Harborline Mutual")), DANA)
    assert p.score == 1.0
    assert sum(e.weight for e in p.evidence) == pytest.approx(2.4)   # 1.0+0.6+0.5+0.2+0.1, uncapped
    assert {e.fact_id for e in p.evidence} == {"F-1", "F-2", "F-3", "F-4", "F-5", "F-6"}


def test_each_rule_counts_once_per_matter():
    p = score_matter(facts(("claimNumber", "CLM-55821"), ("claimNumber", "clm 55821"),
                           ("insurer", "Harborline Mutual"), ("provider", "Bayview Orthopedics")), DANA)
    assert p.score == 0.7
    assert [e.rule for e in p.evidence] == ["claim_number", "insurer_provider"]


def test_evidence_points_to_the_earliest_page():
    fs = [Fact("F-9", "claimNumber", "CLM-55821", page=3, confidence=0.9),
          Fact("F-2", "claimNumber", "CLM-55821", page=1, confidence=0.9)]
    assert score_matter(fs, DANA).evidence[0].fact_id == "F-2"


# --- normalization (exact, no fuzzy matching) ------------------------------
@pytest.mark.parametrize("a, b", [
    ("Whitfield, Dana", "Dana Whitfield"),
    ("DANA M. WHITFIELD", "Dana Whitfield"),
    ("Carter, James, Jr.", "James Carter Jr."),
    ("José Núñez", "Jose Nunez"),
])
def test_equal_names(a, b):
    assert norm_name(a) == norm_name(b)


@pytest.mark.parametrize("a, b", [
    ("Dana Whitfeld", "Dana Whitfield"),       # typo: no fuzzy match
    ("James Carter", "James Carter Jr."),       # suffix distinguishes people
    ("Dana Whitfield-Ross", "Dana Whitfield"),
])
def test_different_names(a, b):
    assert norm_name(a) != norm_name(b)


@pytest.mark.parametrize("text", ["2026-06-14", "06/14/2026", "6/14/2026", "06-14-2026",
                                  "June 14, 2026", "Jun 14, 2026", "14 June 2026"])
def test_date_formats(text):
    assert norm_date(text) == "2026-06-14"


def test_unparseable_date_matches_nothing():
    assert norm_date("14/06/2026") is None      # day-first is not assumed
    assert score_matter(facts(("incidentDate", "sometime in June")), DANA).score == 0


def test_org_names_ignore_case_punctuation_and_suffixes():
    assert norm_org("BAYVIEW ORTHOPEDICS, INC.") == norm_org("Bayview Orthopedics")
    assert norm_org("St. Alder Medical Center") == norm_org("St Alder Medical Center")


# --- outcome thresholds -----------------------------------------------------
def test_confident_match_has_no_flags(two):
    r = match(facts(("claimNumber", "CLM-55821"), ("clientName", "Dana Whitfield")), two)
    assert r.matter_id == "M-1042" and r.flags == []


def test_exactly_threshold_is_confident(two):
    r = match(facts(("clientName", "Dana Whitfield"), ("dateOfBirth", "03/12/1984"),
                    ("provider", "Bayview Orthopedics")), two)
    assert r.top.score == 0.6 and r.confident and r.flags == []


def test_below_threshold_is_low_confidence_with_no_proposal(two):
    r = match(facts(("clientName", "Dana Whitfield"), ("incidentDate", "2026-06-14"),
                    ("insurer", "Harborline Mutual")), two)
    assert r.top.score == 0.5
    assert r.matter_id is None
    assert [f.type for f in r.flags] == [FlagType.LOW_CONFIDENCE]
    assert not r.flags[0].blocking
    assert "M-1042 0.50" in r.flags[0].detail


def test_no_facts_is_low_confidence(two):
    r = match([], two)
    assert r.candidates == [] and r.has(FlagType.LOW_CONFIDENCE)


@pytest.mark.parametrize("second_pairs, ambiguous", [
    ([("clientName", "Marcus Bell"), ("dateOfBirth", "11/02/1979")], True),    # 0.6 vs 0.5
    ([("clientName", "Marcus Bell"), ("dateOfBirth", "11/02/1979"),
      ("provider", "Northgate Imaging")], True),                               # 0.6 vs 0.6
    ([("clientName", "Marcus Bell"), ("incidentDate", "2026-05-02")], False),   # 0.6 vs 0.4
])
def test_top_two_within_margin_is_ambiguous_and_blocking(two, second_pairs, ambiguous):
    fs = facts(("claimNumber", "CLM-55821"), *second_pairs)
    r = match(fs, two)
    assert r.has(FlagType.AMBIGUOUS_MATCH) is ambiguous
    if ambiguous:
        flag = next(f for f in r.flags if f.type == FlagType.AMBIGUOUS_MATCH)
        assert flag.blocking


def test_float_sums_do_not_change_the_outcome(two):
    # 0.5 + 0.2 is 0.7000000000000001 in floating point; the gap to 0.6 is still 0.1.
    r = match(facts(("clientName", "Dana Whitfield"), ("dateOfBirth", "03/12/1984"),
                    ("incidentDate", "2026-06-14"), ("claimNumber", "CLM-60310")), two)
    assert [p.score for p in r.candidates] == [0.7, 0.6]
    assert r.has(FlagType.AMBIGUOUS_MATCH)


def test_tie_is_ordered_by_matter_id(two):
    r = match(facts(("matterNumber", "M-2077"), ("matterNumber", "M-1042")), two)
    assert [p.matter_id for p in r.candidates] == ["M-1042", "M-2077"]
    assert r.has(FlagType.AMBIGUOUS_MATCH)


def test_matcher_ignores_ai_confidence(two):
    low = [Fact("F-1", "claimNumber", "CLM-55821", page=1, confidence=0.05)]
    assert match(low, two).top.score == 0.6


# --- record integration ------------------------------------------------------
def test_run_matching_attaches_no_proposal_when_not_confident(two):
    record = make_record()
    record.facts = facts(("clientName", "Dana Whitfield"))
    flags = run_matching(record, two)
    assert record.processing_status == ProcessingStatus.MATCHING
    assert record.proposals == []                       # low confidence: nothing proposed
    assert [f.type for f in flags] == [FlagType.LOW_CONFIDENCE]


def test_low_confidence_needs_a_person_to_choose_the_matter(two):
    audit = AuditLog()
    record = make_record(audit=audit)
    record.facts = facts(("clientName", "Dana Whitfield"))
    write_flags(record, run_matching(record, two), audit)
    with pytest.raises(ReviewError) as err:
        review_action(record, Action.VALIDATE, PARALEGAL, index=two, audit=audit,
                      expected_version=record.version)
    assert err.value.code == "GUARD_BLOCKED" and "no matter selected" in err.value.message

    review_action(record, Action.CORRECT, PARALEGAL, index=two, audit=audit,
                  expected_version=record.version, change={"selected_matter_id": "M-1042"},
                  reason="client confirmed by phone")
    write_flags(record, run_matching(record, two), audit)   # checks re-run after correction
    review_action(record, Action.VALIDATE, PARALEGAL, index=two, audit=audit,
                  expected_version=record.version)
    assert record.review_status == ReviewStatus.APPROVED
    assert record.selected_matter_id == "M-1042"


def test_ambiguous_match_from_matcher_blocks_validate(two):
    audit = AuditLog()
    record = make_record(audit=audit)
    record.facts = facts(("claimNumber", "CLM-55821"), ("clientName", "Marcus Bell"),
                         ("dateOfBirth", "11/02/1979"))
    write_flags(record, run_matching(record, two), audit)
    assert [p.matter_id for p in record.proposals[:2]] == ["M-1042", "M-2077"]
    with pytest.raises(ReviewError) as err:
        review_action(record, Action.VALIDATE, PARALEGAL, index=two, audit=audit,
                      expected_version=record.version)
    assert "AMBIGUOUS_MATCH" in err.value.message
