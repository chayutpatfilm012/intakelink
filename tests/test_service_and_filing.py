"""Slice 6: filing (C10), notifications, the service pipeline and the demo.

Tier-2 release and tier-1 override rules are FR-11 (author-written tests);
here they appear only inside the demo script, which must run end to end.
"""
from __future__ import annotations

import json

import pytest

from conftest import ATTORNEY, PARALEGAL, make_record
from intakelink import demo
from intakelink.audit import AuditLog
from intakelink.filing import INTAKE_QUEUE, FilingError, MockCaseManagementSystem
from intakelink.models import Action, ProcessingStatus, ReviewStatus
from intakelink.review import ReviewError, review_action
from intakelink.service import IntakeLinkService
from intakelink.synthetic import USERS, matter_index


# --- mock case-management system ---------------------------------------------
def test_only_approved_records_are_filed(index, audit):
    cms = MockCaseManagementSystem()
    record = make_record(audit=audit)
    with pytest.raises(FilingError) as err:
        cms.file(record, audit)
    assert err.value.code == "NOT_APPROVED"
    assert audit.denials()[-1].event == "file"


def test_filing_records_matter_approver_and_happens_once(index, audit):
    cms = MockCaseManagementSystem()
    record = make_record(audit=audit)
    review_action(record, Action.VALIDATE, PARALEGAL, index=index, audit=audit,
                  expected_version=record.version)
    receipt = cms.file(record, audit)
    assert (receipt.matter_id, receipt.approved_by) == ("M-1042", PARALEGAL.user_id)
    assert cms.folder("M-1042") == [receipt]
    assert record.review_status == ReviewStatus.APPROVED          # filing changes nothing
    with pytest.raises(FilingError) as err:
        cms.file(record, audit)
    assert err.value.code == "ALREADY_FILED"


# --- service pipeline ----------------------------------------------------------
@pytest.fixture
def svc(tmp_path):
    service = IntakeLinkService(tmp_path, matter_index(), audit_path=tmp_path / "audit.jsonl")
    ids = demo.load_set(service)
    return service, ids


def test_pipeline_matches_ground_truth(svc):
    service, ids = svc
    assert demo.check_against_manifest(service, ids) == []
    assert all(r.review_status == ReviewStatus.CANDIDATE for r in service.records.values())


def test_corrupted_files_notify_the_intake_queue(svc):
    service, ids = svc
    notes = service.notifier.for_user(INTAKE_QUEUE)
    assert sorted(n.record_id for n in notes) == sorted([ids["C01"], ids["C02"]])


def test_hold_notifies_only_the_responsible_attorney(svc):
    service, ids = svc
    held = [n for n in service.notifier.all() if n.record_id == ids["X06"]]
    assert [(n.to, n.subject) for n in held] == [("a.reyes", "Document held for your review")]
    assert "Achterberg" not in held[0].body and "M-1203" not in held[0].body


def test_approval_files_and_notifies_the_matter_team(svc):
    service, ids = svc
    rid = ids["S01"]
    service.act(rid, Action.VALIDATE, USERS["p.lee"], expected_version=service.records[rid].version)
    receipt = service.cms.receipt_for(rid)
    assert receipt.matter_id == "M-1042"
    told = sorted(n.to for n in service.notifier.all()
                  if n.record_id == rid and n.subject == "Document filed")
    assert told == ["a.reyes", "p.lee", "s.ortiz"]


def test_correction_reruns_checks(svc):
    service, ids = svc
    rid = ids["L02"]                                   # name only: no matter proposed
    rec = service.records[rid]
    service.act(rid, Action.CORRECT, USERS["p.garcia"], expected_version=rec.version,
                change={"selected_matter_id": "M-1219"}, reason="confirmed by phone")
    assert rec.processing_status == ProcessingStatus.READY_FOR_REVIEW
    assert [e.event for e in service.audit.for_record(rid)][-2:] == ["correct", "flags_written"]


def test_refused_action_is_not_filed(svc):
    service, ids = svc
    rid = ids["A03"]                                   # ambiguous: blocked
    with pytest.raises(ReviewError):
        service.act(rid, Action.VALIDATE, USERS["p.lee"], expected_version=service.records[rid].version)
    assert service.cms.receipt_for(rid) is None


def test_queue_shows_only_own_matters_and_intake_queue(svc):
    service, ids = svc
    baker = USERS["s.baker"]
    mine = set(service.matters_for(baker))
    for rec in service.visible_records(baker):
        assert rec.target_matter_id() is None or rec.target_matter_id() in mine
    assert service.records[ids["S01"]] not in service.visible_records(baker)


def test_off_matter_view_is_refused_and_logged(svc):
    service, ids = svc
    with pytest.raises(ReviewError) as err:
        service.view(ids["S01"], USERS["s.baker"])
    assert err.value.code == "NOT_AUTHORIZED"
    assert service.audit.denials()[-1].event == "view"
    assert service.view(ids["S01"], USERS["p.lee"]).text.page_count >= 1


def test_audit_log_is_mirrored_to_disk(svc, tmp_path):
    service, _ = svc
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(service.audit)
    assert json.loads(lines[0])["event"] == "document_received"


# --- demo ------------------------------------------------------------------------
def test_demo_runs_end_to_end(tmp_path, capsys):
    assert demo.main(["--workdir", str(tmp_path / "demo")]) == 0
    out = capsys.readouterr().out
    assert "Matches ground truth: 64/64" in out
    assert "All outcomes and scripted steps behaved as expected." in out


def test_demo_refuses_a_used_workdir(tmp_path):
    assert demo.main(["--workdir", str(tmp_path / "demo")]) == 0
    assert demo.main(["--workdir", str(tmp_path / "demo")]) == 2
