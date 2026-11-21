"""Slice 4: what the Cross-Matter Guard and duplicate checks DETECT.

These tests cover detection only: which flags are produced and what their
details reveal. How flagged records behave under review (hold, override,
release) is FR-11 and is tested by the author (docs/AUTHOR_TESTS.md).
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from intakelink.audit import AuditLog
from intakelink.guard import (PriorDocument, cross_matter_findings,
                              duplicate_findings, fact_conflict_findings, run_checks)
from intakelink.intake import DocumentStore, Intake, prepare_text
from intakelink.models import (CandidateRecord, Document, Fact, FlagType)
from intakelink.synthetic import gold_facts, load_manifest, matter_index

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
INDEX = matter_index()
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


_ids = itertools.count(1)


def F(field, value, page=1):
    return Fact(f"F-{next(_ids)}", field, value, page=page, confidence=0.9)


def own(matter_id: str) -> list[Fact]:
    m = INDEX.get(matter_id)
    return [F("clientName", m.client_name), F("dateOfBirth", m.date_of_birth),
            F("claimNumber", m.claim_numbers[0])]


def tiers(findings):
    return sorted((f.tier, f.other_matter_id, f.evidence) for f in findings)


# --- cross-matter -----------------------------------------------------------
@pytest.mark.parametrize("foreign, expected", [
    ([F("claimNumber", "CLM-60310", 2)], (2, "M-2077", "claim number")),
    ([F("matterNumber", "M-2077", 2)], (2, "M-2077", "matter number")),
    ([F("clientName", "Bell, Marcus", 3), F("dateOfBirth", "11/02/1979", 3)],
     (2, "M-2077", "name and date of birth")),
    ([F("clientName", "Marcus Bell", 2)], (1, "M-2077", "name")),
])
def test_other_clients_identifiers_are_found(foreign, expected):
    found = cross_matter_findings(own("M-1042") + foreign, INDEX.get("M-1042"), INDEX)
    assert tiers(found) == [expected]
    assert found[0].page == foreign[0].page


def test_claim_number_outranks_a_name_for_the_same_other_matter():
    facts = own("M-1042") + [F("clientName", "Marcus Bell", 2), F("claimNumber", "CLM-60310", 2)]
    assert tiers(cross_matter_findings(facts, INDEX.get("M-1042"), INDEX)) == [(2, "M-2077", "claim number")]


def test_own_identifiers_raise_nothing():
    assert cross_matter_findings(own("M-1042"), INDEX.get("M-1042"), INDEX) == []


def test_same_client_on_another_matter_is_not_foreign():
    # Elliot Marsh has M-1389 and M-1407; name + DOB fit both.
    facts = [F("clientName", "Elliot Marsh"), F("dateOfBirth", "12/12/1981"),
             F("claimNumber", "CLM-63772")]
    assert cross_matter_findings(facts, INDEX.get("M-1407"), INDEX) == []


def test_shared_name_alone_is_not_foreign():
    # Two unrelated clients named Maria Santos (M-1368, M-1372).
    facts = [F("clientName", "Maria Santos"), F("dateOfBirth", "08/30/1972")]
    assert cross_matter_findings(facts, INDEX.get("M-1368"), INDEX) == []


def test_shared_name_with_the_other_clients_dob_is_tier_two():
    facts = [F("clientName", "Maria Santos"), F("dateOfBirth", "08/30/1972"),
             F("dateOfBirth", "01/07/1998", 2)]
    assert tiers(cross_matter_findings(facts, INDEX.get("M-1368"), INDEX)) == \
        [(2, "M-1372", "name and date of birth")]


def test_flag_detail_does_not_reveal_the_other_matter():
    facts = own("M-1042") + [F("clientName", "Marcus Bell", 2), F("dateOfBirth", "11/02/1979", 2)]
    flag = cross_matter_findings(facts, INDEX.get("M-1042"), INDEX)[0].to_flag()
    assert flag.type == FlagType.CROSS_MATTER_RISK and flag.blocking
    assert "page 2" in flag.detail
    for secret in ("M-2077", "Marcus", "Bell", "1979"):
        assert secret not in flag.detail


def test_tier_one_flag_type():
    facts = own("M-1042") + [F("clientName", "Marcus Bell", 2)]
    flag = cross_matter_findings(facts, INDEX.get("M-1042"), INDEX)[0].to_flag()
    assert flag.type == FlagType.CROSS_MATTER_NAME and flag.blocking


def test_no_target_matter_means_no_cross_matter_check():
    record = _record("R-0001", b"x", [F("clientName", "Marcus Bell")])   # name only: low confidence
    result = run_checks(record, None, INDEX, [], AuditLog())
    assert result.cross_matter == []
    assert [f.type for f in result.flags] == [FlagType.LOW_CONFIDENCE]


# --- fact conflict (FR-07) -----------------------------------------------------
def conflicts(facts, target="M-1042"):
    return fact_conflict_findings(facts, INDEX.get(target), INDEX)


def test_target_name_with_a_different_dob_is_a_conflict():
    facts = [F("clientName", "Whitfield, Dana"), F("dateOfBirth", "03/21/1984"),
             F("claimNumber", "CLM-55821")]
    found = conflicts(facts)
    assert [(c.field, c.page) for c in found] == [("dateOfBirth", 1)]
    flag = found[0].to_flag()
    assert flag.type == FlagType.FACT_CONFLICT and flag.blocking
    assert flag.detail == "date of birth on page 1 differs from the matter record"
    assert "1984" not in flag.detail and "03/12" not in flag.detail   # record value not shown


def test_target_name_with_matching_dob_raises_nothing():
    assert conflicts(own("M-1042")) == []


def test_name_without_a_dob_on_that_page_raises_nothing():
    facts = [F("clientName", "Dana Whitfield", 1), F("dateOfBirth", "01/01/1990", 2)]
    assert conflicts(facts) == []


def test_unparseable_dob_is_not_compared():
    assert conflicts([F("clientName", "Dana Whitfield"), F("dateOfBirth", "unknown")]) == []


def test_another_clients_name_and_dob_is_not_a_fact_conflict():
    # Page 2 holds Marcus Bell's name + DOB: tier 2, not a conflict about Dana.
    facts = own("M-1042") + [F("clientName", "Marcus Bell", 2), F("dateOfBirth", "11/02/1979", 2)]
    assert conflicts(facts) == []
    assert tiers(cross_matter_findings(facts, INDEX.get("M-1042"), INDEX))[0][0] == 2


def test_shared_name_with_the_other_clients_dob_is_not_a_fact_conflict():
    # Two different clients named Maria Santos; the 1998 DOB is the other one's.
    facts = [F("clientName", "Maria Santos"), F("dateOfBirth", "01/07/1998")]
    assert conflicts(facts, "M-1368") == []
    assert tiers(cross_matter_findings(facts, INDEX.get("M-1368"), INDEX))[0][0] == 2


def test_same_client_on_another_matter_is_not_a_fact_conflict():
    facts = [F("clientName", "Elliot Marsh"), F("dateOfBirth", "12/12/1981"),
             F("claimNumber", "CLM-58115")]
    assert conflicts(facts, "M-1389") == []


def test_correcting_the_dob_clears_the_conflict_and_allows_approval():
    from intakelink.models import Action, ReviewStatus
    from intakelink.review import ReviewError, review_action
    from intakelink.synthetic import USERS
    audit = AuditLog()
    record = _record("R-0009", b"bill", [F("clientName", "Whitfield, Dana"),
                                         F("dateOfBirth", "03/21/1984"),
                                         F("claimNumber", "CLM-55821")])
    run_checks(record, None, INDEX, [], audit)
    assert record.has_open(FlagType.FACT_CONFLICT)
    with pytest.raises(ReviewError) as err:
        review_action(record, Action.VALIDATE, USERS["p.lee"], index=INDEX, audit=audit,
                      expected_version=record.version)
    assert err.value.code == "GUARD_BLOCKED" and "FACT_CONFLICT" in err.value.message

    dob = next(f for f in record.facts if f.field == "dateOfBirth")
    review_action(record, Action.CORRECT, USERS["p.lee"], index=INDEX, audit=audit,
                  expected_version=record.version, change={"facts": {dob.fact_id: "03/12/1984"}},
                  reason="DOB checked against the intake form")
    run_checks(record, None, INDEX, [], audit)          # checks re-run after the correction
    assert not record.has_open(FlagType.FACT_CONFLICT)
    review_action(record, Action.VALIDATE, USERS["p.lee"], index=INDEX, audit=audit,
                  expected_version=record.version)
    assert record.review_status == ReviewStatus.APPROVED


# --- duplicates ---------------------------------------------------------------
BILL = ("Bayview Orthopedics\nSTATEMENT OF CHARGES\nDate: 07/20/2026\n"
        "Patient: Whitfield, Dana\nDOB: 03/12/1984\nClaim No.: CLM-55821\n\f"
        "99204 New patient visit $250.00\n72040 X-ray $120.00\nTotal due: $370.00\n")
FACTS = lambda: [F("clientName", "Whitfield, Dana"), F("dateOfBirth", "03/12/1984"),
                 F("claimNumber", "CLM-55821"), F("documentDate", "07/20/2026")]


def _record(rid: str, data: bytes, facts, doc_type="medical_bill", minutes=0) -> CandidateRecord:
    import hashlib
    doc = Document(f"D-{rid[2:]}", "email", T0 + timedelta(minutes=minutes), "txt", 2,
                   hashlib.sha256(data).hexdigest())
    return CandidateRecord(rid, doc, document_type=doc_type, facts=facts)


def _prior(text: str, facts, rid="R-0001", doc_type="medical_bill"):
    rec = _record(rid, text.encode(), facts, doc_type)
    return PriorDocument.from_record(rec, prepare_text(rec.document.doc_id, "txt", text.encode()))


def _check(text: str, facts, history, doc_type="medical_bill"):
    rec = _record("R-0002", text.encode(), facts, doc_type, minutes=5)
    return duplicate_findings(rec, prepare_text("D-0002", "txt", text.encode()), history)


def test_exact_duplicate():
    found = _check(BILL, FACTS(), [_prior(BILL, FACTS())])
    assert [(d.kind, d.prior_record_id) for d in found] == [(FlagType.EXACT_DUPLICATE, "R-0001")]
    assert found[0].to_flag().blocking                 # report §7.5: exact duplicate blocks


def test_resent_fax_is_a_possible_duplicate():
    resent = "FAX RECEIVED 2026-10-12 09:14  PAGE 1/2\n" + BILL.replace(": ", ":  ")
    found = _check(resent, FACTS(), [_prior(BILL, FACTS())])
    assert [d.kind for d in found] == [FlagType.POSSIBLE_DUPLICATE]
    assert not found[0].to_flag().blocking             # warning only


def test_revised_statement_is_a_possible_new_version():
    revised = BILL.replace("STATEMENT", "REVISED STATEMENT").replace("$370.00", "$410.00")
    found = _check(revised, FACTS(), [_prior(BILL, FACTS())])
    assert [d.kind for d in found] == [FlagType.POSSIBLE_NEW_VERSION]
    assert not found[0].to_flag().blocking             # warning only


def test_another_statement_for_the_same_patient_is_not_a_duplicate():
    later = BILL.replace("07/20/2026", "08/19/2026")
    facts = FACTS()[:3] + [F("documentDate", "08/19/2026")]
    assert _check(later, facts, [_prior(BILL, FACTS())]) == []


def test_same_template_for_another_client_is_not_a_duplicate():
    other = (BILL.replace("Whitfield, Dana", "Bell, Marcus")
                 .replace("03/12/1984", "11/02/1979").replace("CLM-55821", "CLM-60310"))
    facts = [F("clientName", "Bell, Marcus"), F("dateOfBirth", "11/02/1979"),
             F("claimNumber", "CLM-60310"), F("documentDate", "07/20/2026")]
    assert _check(other, facts, [_prior(BILL, FACTS())]) == []


def test_different_document_type_is_not_a_duplicate():
    assert _check(BILL + " ", FACTS(), [_prior(BILL, FACTS(), doc_type="medical_record")]) == []


def test_a_record_is_not_its_own_duplicate():
    rec = _record("R-0001", BILL.encode(), FACTS())
    assert duplicate_findings(rec, None, [PriorDocument.from_record(rec, None)]) == []


# --- the synthetic set in arrival order ---------------------------------------
CONFLICTING = {"A01", "A02", "A05", "A06"}   # another client's identifiers on page 1


@pytest.fixture(scope="module")
def checked(tmp_path_factory):
    audit = AuditLog()
    intake = Intake(DocumentStore(tmp_path_factory.mktemp("store")), audit)
    history, out = [], {}
    for e in load_manifest(DATA):
        data = (DATA / "documents" / e["file"]).read_bytes()
        got = intake.receive(data, e["file"], e["channel"], datetime.fromisoformat(e["received_at"]))
        if got.text is None:
            continue
        got.record.facts = gold_facts(e)
        got.record.document_type = e["document_type"]
        out[e["key"]] = (e, got.record, run_checks(got.record, got.text, INDEX, history, audit))
        history.append(PriorDocument.from_record(got.record, got.text))
    return out


def test_set_cross_matter_flags(checked):
    for key, (e, record, result) in checked.items():
        types = {f.type for f in result.flags}
        if e["cross_matter"]:
            want = FlagType.CROSS_MATTER_RISK if e["cross_matter"]["tier"] == 2 else FlagType.CROSS_MATTER_NAME
            assert types & {FlagType.CROSS_MATTER_RISK, FlagType.CROSS_MATTER_NAME} == {want}, key
            assert [(c.other_matter_id, c.page) for c in result.cross_matter] == \
                [(e["cross_matter"]["matter"], e["cross_matter"]["page"])], key
        elif key in CONFLICTING:
            assert FlagType.CROSS_MATTER_RISK in types, key
        else:
            assert not types & {FlagType.CROSS_MATTER_RISK, FlagType.CROSS_MATTER_NAME}, key


def test_set_fact_conflict_flags(checked):
    for key, (e, record, result) in checked.items():
        found = [(c.field, c.page) for c in result.conflicts]
        if e["fact_conflict"]:
            assert found == [(e["fact_conflict"]["field"], e["fact_conflict"]["page"])], key
        else:
            assert found == [], key


def test_set_duplicate_flags(checked):
    kind_to_flag = {"exact": FlagType.EXACT_DUPLICATE, "probable": FlagType.POSSIBLE_DUPLICATE,
                    "newer_version": FlagType.POSSIBLE_NEW_VERSION}
    record_of = {e["file"]: rec.record_id for e, rec, _ in checked.values()}
    for key, (e, record, result) in checked.items():
        found = [(d.kind, d.prior_record_id) for d in result.duplicates]
        if e["duplicate"]:
            assert found == [(kind_to_flag[e["duplicate"]["kind"]], record_of[e["duplicate"]["of"]])], key
        else:
            assert found == [], key
