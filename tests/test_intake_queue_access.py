"""Intake-queue access (NFR-01): a record with no matter yet may hold any
client's data, so only intake staff (senior paralegals) may open or work it.
"""
from __future__ import annotations

import pytest

from intakelink import demo
from intakelink.models import Action, ReviewStatus
from intakelink.review import ReviewError
from intakelink.service import IntakeLinkService
from intakelink.synthetic import USERS, matter_index


@pytest.fixture
def svc(tmp_path):
    service = IntakeLinkService(tmp_path, matter_index())
    return service, demo.load_set(service)


def _intake_records(service):
    return [r for r in service.records.values() if r.target_matter_id() is None]


def test_intake_queue_exists_in_the_set(svc):
    service, ids = svc
    # 3 low confidence + 2 unreadable files have no matter.
    assert {r.record_id for r in _intake_records(service)} == \
        {ids[k] for k in ("L02", "L03", "L04", "C01", "C02")}


@pytest.mark.parametrize("user", ["p.lee", "p.garcia", "a.reyes", "a.novak"])
def test_non_intake_staff_cannot_see_or_open_intake_records(svc, user):
    service, ids = svc
    intake = {r.record_id for r in _intake_records(service)}
    assert not intake & {r.record_id for r in service.visible_records(USERS[user])}
    before = len(service.audit.denials())
    with pytest.raises(ReviewError) as err:
        service.view(ids["L02"], USERS[user])
    assert err.value.code == "NOT_AUTHORIZED"
    assert len(service.audit.denials()) == before + 1


def test_non_intake_staff_cannot_act_on_intake_records(svc):
    service, ids = svc
    rec = service.records[ids["L02"]]
    with pytest.raises(ReviewError) as err:
        service.act(rec.record_id, Action.CORRECT, USERS["p.garcia"],   # on M-1219 itself
                    expected_version=rec.version, change={"selected_matter_id": "M-1219"},
                    reason="looks like Hana's letter")
    assert err.value.code == "NOT_AUTHORIZED"
    assert rec.selected_matter_id is None


@pytest.mark.parametrize("user", ["s.ortiz", "s.baker"])
def test_intake_staff_see_the_whole_intake_queue(svc, user):
    service, _ = svc
    visible = {r.record_id for r in service.visible_records(USERS[user])}
    assert {r.record_id for r in _intake_records(service)} <= visible


def test_routing_needs_a_reason(svc):
    service, ids = svc
    rec = service.records[ids["L02"]]
    with pytest.raises(ReviewError) as err:
        service.act(rec.record_id, Action.CORRECT, USERS["s.ortiz"],
                    expected_version=rec.version, change={"selected_matter_id": "M-1219"})
    assert err.value.code == "REASON_REQUIRED"


def test_routed_record_moves_to_the_matter_team(svc):
    service, ids = svc
    rid = ids["L02"]
    rec = service.records[rid]
    # s.ortiz is not on M-1219 but may route to it from the intake queue.
    service.act(rid, Action.CORRECT, USERS["s.ortiz"], expected_version=rec.version,
                change={"selected_matter_id": "M-1219"}, reason="client confirmed by phone")
    assert rec.target_matter_id() == "M-1219"
    with pytest.raises(ReviewError):
        service.view(rid, USERS["s.ortiz"])               # access moved to the team
    service.act(rid, Action.VALIDATE, USERS["p.garcia"], expected_version=rec.version)
    assert rec.review_status == ReviewStatus.APPROVED


def test_intake_staff_can_reject_an_unreadable_file(svc):
    service, ids = svc
    rec = service.records[ids["C01"]]
    service.act(rec.record_id, Action.REJECT, USERS["s.baker"], expected_version=rec.version,
                reason="file is corrupted; sender asked to resend")
    assert rec.review_status == ReviewStatus.REJECTED


def test_escalated_intake_record_can_still_be_decided_by_intake_staff(svc):
    service, ids = svc
    rec = service.records[ids["C02"]]
    service.act(rec.record_id, Action.ESCALATE, USERS["s.baker"], expected_version=rec.version,
                reason="unreadable; may be a medical record")
    service.act(rec.record_id, Action.REJECT, USERS["s.ortiz"], expected_version=rec.version,
                reason="resend requested")
    assert rec.review_status == ReviewStatus.REJECTED
