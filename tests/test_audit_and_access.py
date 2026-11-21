"""FR-08 (complete audit trail), NFR-09 (append-only) and NFR-01 (matter access)."""
from __future__ import annotations

import json

import pytest

from conftest import OTHER_ATTORNEY, OUTSIDER, PARALEGAL
from intakelink.audit import AuditLog
from intakelink.models import Action
from intakelink.review import ReviewError, review_action


def test_every_action_is_attributed(record, index, audit):
    review_action(record, Action.ESCALATE, PARALEGAL, index=index, audit=audit,
                  expected_version=record.version, reason="Two claim numbers")
    entry = audit.for_record("R-0001")[-1]
    assert (entry.actor_id, entry.event, entry.outcome) == ("p.lee", "escalate", "ok")
    assert entry.detail == "Two claim numbers"
    decision = record.decisions[-1]
    assert (decision.actor_id, decision.prior, decision.new) == ("p.lee", "candidate", "escalated")


def test_audit_log_cannot_be_edited():
    log = AuditLog()
    log.append("p.lee", "validate", "R-1")
    view = log.entries()
    assert isinstance(view, tuple)
    with pytest.raises(AttributeError):
        view[0].actor_id = "someone-else"  # entries are frozen
    assert not hasattr(log, "delete") and not hasattr(log, "remove")


def test_audit_log_mirrors_to_jsonl(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("p.lee", "validate", "R-1")
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert line["actor_id"] == "p.lee" and line["outcome"] == "ok"


@pytest.mark.parametrize("actor", [OUTSIDER, OTHER_ATTORNEY])
def test_off_matter_staff_are_denied_and_logged(record, index, audit, actor):
    with pytest.raises(ReviewError) as err:
        review_action(record, Action.VALIDATE, actor, index=index, audit=audit,
                      expected_version=record.version)
    assert err.value.code == "NOT_AUTHORIZED"
    denied = audit.denials()[-1]
    assert (denied.actor_id, denied.record_id) == (actor.user_id, "R-0001")
