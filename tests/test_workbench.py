"""Slice 6: Workbench (C9) smoke tests with Streamlit's headless AppTest."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from intakelink.models import Action, FlagType  # noqa: E402
from intakelink.synthetic import USERS  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "app" / "workbench.py")


def _by_label(widgets, label):
    return next(w for w in widgets if w.label == label)


def _loaded(user: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    _by_label(at.selectbox, "Signed in as (simulated)").set_value(user).run()
    _by_label(at.button, "Load synthetic test set (64 documents)").click().run()
    assert not at.exception, at.exception
    return at


def test_start_page_asks_to_load_data():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    assert "Load the synthetic test set" in at.info[0].value


def test_queue_shows_only_the_users_open_records():
    at = _loaded("s.baker")
    svc = at.session_state["service"]
    shown = set(at.dataframe[0].value["record"])
    allowed = {r.record_id for r in svc.visible_records(USERS["s.baker"]) if not r.is_final}
    assert shown and shown == allowed


def test_refused_action_is_shown_and_changes_nothing():
    at = _loaded("p.lee")
    svc = at.session_state["service"]
    blocked = next(r.record_id for r in svc.visible_records(USERS["p.lee"])
                   if r.open_blocking_flags())
    _by_label(at.selectbox, "Open record").set_value(blocked).run()
    version = svc.records[blocked].version
    _by_label(at.button, "Submit").click().run()      # default action: validate
    assert not at.exception, at.exception
    assert any("Refused" in e.value for e in at.error)
    assert svc.records[blocked].version == version


def test_fact_correction_through_the_page_clears_a_fact_conflict():
    at = _loaded("p.lee")
    svc = at.session_state["service"]
    rid = next(r.record_id for r in svc.visible_records(USERS["p.lee"])
               if r.has_open(FlagType.FACT_CONFLICT))
    rec = svc.records[rid]
    dob = next(f for f in rec.facts if f.field == "dateOfBirth")
    _by_label(at.selectbox, "Open record").set_value(rid).run()
    _by_label(at.selectbox, "Action").set_value(Action.CORRECT)
    _by_label(at.selectbox, "Correct: fact").set_value(dob.fact_id)
    _by_label(at.text_input, "Correct: new value for that fact").input(
        svc.index.get(rec.target_matter_id()).date_of_birth)
    _by_label(at.text_area, "Reason (required for reject, escalate, override and release)").input(
        "DOB checked against the intake form")
    _by_label(at.button, "Submit").click().run()
    assert not at.exception, at.exception
    assert not rec.open_blocking_flags()


def test_approval_through_the_page_files_the_record():
    at = _loaded("p.lee")
    svc = at.session_state["service"]
    clean = next(r.record_id for r in svc.visible_records(USERS["p.lee"])
                 if r.proposals and not r.flags)
    _by_label(at.selectbox, "Open record").set_value(clean).run()
    _by_label(at.button, "Submit").click().run()
    assert not at.exception, at.exception
    assert svc.cms.receipt_for(clean) is not None
    assert any("validate recorded" in s.value for s in at.success)
