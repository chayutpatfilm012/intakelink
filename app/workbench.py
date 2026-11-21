"""Human Review Workbench (C9): ``streamlit run app/workbench.py``.

A reviewer picks who they are (simulated sign-in; synthetic staff only), loads
the synthetic test set, and works the queue. Every action goes through
``IntakeLinkService.act`` and so through ``review.review_action``; the page
itself never changes a record.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

try:
    import intakelink  # noqa: F401  (installed with pip install -e .)
except ImportError:      # running from a source checkout without install
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import streamlit as st

from intakelink.demo import load_set
from intakelink.fields import DOCUMENT_TYPES
from intakelink.models import Action, HoldStatus, ReviewStatus
from intakelink.review import ReviewError
from intakelink.service import IntakeLinkService
from intakelink.synthetic import USERS, matter_index

st.set_page_config(page_title="IntakeLink Workbench", layout="wide")


def service() -> IntakeLinkService | None:
    return st.session_state.get("service")


def load() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="intakelink-workbench-"))
    svc = IntakeLinkService(workdir, matter_index(), audit_path=workdir / "audit.jsonl")
    load_set(svc)
    st.session_state["service"] = svc
    st.session_state.pop("selected", None)


def status_of(svc: IntakeLinkService, rec) -> str:
    if svc.cms.receipt_for(rec.record_id):
        return "filed"
    if rec.hold_status == HoldStatus.HELD_FOR_ATTORNEY:
        return "held for attorney"
    if rec.processing_status.value != "READY_FOR_REVIEW":
        return rec.processing_status.value.lower()
    return rec.review_status.value


# --- sidebar: who am I ----------------------------------------------------------
st.sidebar.title("IntakeLink")
st.sidebar.caption("Synthetic data only. Do not load real client documents.")
user_id = st.sidebar.selectbox("Signed in as (simulated)", list(USERS),
                               format_func=lambda u: f"{u} ({USERS[u].role.value})")
actor = USERS[user_id]
if st.sidebar.button("Load synthetic test set (64 documents)", type="primary"):
    load()
svc = service()
if svc is None:
    st.title("Human Review Workbench")
    st.info("Load the synthetic test set from the sidebar to start.")
    st.stop()
st.sidebar.write("Your matters: " + ", ".join(svc.matters_for(actor)))

queue_tab, notes_tab, filed_tab, audit_tab = st.tabs(
    ["Review queue", "Notifications", "Filed", "Audit log"])

# --- queue -----------------------------------------------------------------------
with queue_tab:
    records = svc.visible_records(actor)
    show = st.radio("Show", ["Needs action", "Held for attorney", "All"], horizontal=True)
    if show == "Needs action":
        records = [r for r in records if not r.is_final]
    elif show == "Held for attorney":
        records = [r for r in records if r.hold_status == HoldStatus.HELD_FOR_ATTORNEY]
    rows = [{"record": r.record_id, "matter": r.target_matter_id() or "(intake queue)",
             "type": r.document_type or "-", "status": status_of(svc, r),
             "flags": ", ".join(f.type.value for f in r.flags if not f.cleared) or "-"}
            for r in records]
    st.caption(f"{len(rows)} record(s) you can see. Records on other matters are hidden.")
    st.dataframe(rows, hide_index=True, width="stretch")

    if records:
        ids = [r.record_id for r in records]
        current = st.session_state.get("selected")
        choice = st.selectbox("Open record", ids, index=ids.index(current) if current in ids else 0)
        first_open = choice != st.session_state.get("selected")
        st.session_state["selected"] = choice
        try:
            view = svc.view(choice, actor, log=first_open)
        except ReviewError as err:
            st.error(f"{err.code}: {err.message}")
            st.stop()
        rec = view.record

        st.subheader(f"{rec.record_id} - {rec.document_type or 'unknown type'}")
        st.write(f"**Status:** {status_of(svc, rec)} - review status `{rec.review_status.value}`, "
                 f"version {rec.version}")
        if view.receipt:
            st.success(f"Filed to {view.receipt.matter_id} as {view.receipt.cms_ref}")

        left, right = st.columns([3, 2])
        with left:
            if view.text is None:
                st.warning("This file could not be read. Ask the sender for a new copy.")
            else:
                page_tabs = st.tabs([f"Page {p.number}" for p in view.text.pages])
                for tab, page in zip(page_tabs, view.text.pages):
                    with tab:
                        st.code(page.text or "(blank page)", language=None)
        with right:
            st.markdown("**Flags**")
            for f in rec.flags or []:
                state = "cleared" if f.cleared else ("blocks approval" if f.blocking else "warning")
                st.write(f"- `{f.type.value}` ({state}): {f.detail}")
            if not rec.flags:
                st.write("None.")

            st.markdown("**Proposed matter and evidence**")
            facts = {f.fact_id: f for f in rec.facts}
            if rec.selected_matter_id:
                st.write(f"Selected by a person: **{rec.selected_matter_id}**")
            for p in rec.proposals[:3]:
                with st.expander(f"{p.matter_id}: score {p.score:.2f}", expanded=p is rec.proposals[0]):
                    for e in p.evidence:
                        fact = facts.get(e.fact_id)
                        where = f"page {fact.page}" if fact and fact.page else "staff entry"
                        st.write(f"- {e.rule} (+{e.weight:.1f}): {fact.value if fact else e.fact_id} ({where})")
            if not rec.proposals:
                st.write("No confident match. Choose the matter with **correct**.")

            st.markdown("**Facts read from the document**")
            st.dataframe([{"field": f.field, "value": f.value, "page": f.page,
                           "origin": f.origin.value} for f in rec.facts],
                         hide_index=True, width="stretch")

        if not rec.is_final:
            st.markdown("### Decide")
            with st.form(f"act-{rec.record_id}-{rec.version}"):
                action = st.selectbox("Action", list(Action), format_func=lambda a: a.value)
                reason = st.text_area("Reason (required for reject, escalate, override and release)")
                matter = st.selectbox("Correct: matter", ["(no change)"] + svc.matters_for(actor))
                doc_type = st.selectbox("Correct: document type", ["(no change)", *DOCUMENT_TYPES])
                if st.form_submit_button("Submit"):
                    change = {}
                    if matter != "(no change)":
                        change["selected_matter_id"] = matter
                    if doc_type != "(no change)":
                        change["document_type"] = doc_type
                    try:
                        svc.act(rec.record_id, action, actor, expected_version=rec.version,
                                reason=reason or None, change=change or None)
                        st.session_state["flash"] = ("ok", f"{action.value} recorded for {rec.record_id}")
                    except ReviewError as err:
                        st.session_state["flash"] = ("error", f"Refused - {err.code}: {err.message}")
                    st.rerun()
        flash = st.session_state.pop("flash", None)
        if flash:
            (st.success if flash[0] == "ok" else st.error)(flash[1])

        st.markdown("**Decision history**")
        st.dataframe([{"by": d.actor_id, "action": d.action.value, "reason": d.reason or "",
                       "at": d.at.strftime("%Y-%m-%d %H:%M:%S")} for d in rec.decisions],
                     hide_index=True, width="stretch")

# --- notifications / filed / audit ---------------------------------------------------
with notes_tab:
    notes = svc.notifier.for_user(actor.user_id)
    st.dataframe([{"subject": n.subject, "message": n.body} for n in notes],
                 hide_index=True, width="stretch")
    if not notes:
        st.write("No notifications for you.")

with filed_tab:
    mine = set(svc.matters_for(actor))
    st.dataframe([{"cms_ref": r.cms_ref, "record": r.record_id, "matter": r.matter_id,
                   "type": r.document_type, "approved_by": r.approved_by}
                  for r in svc.cms.all() if r.matter_id in mine],
                 hide_index=True, width="stretch")

with audit_tab:
    visible = {r.record_id for r in svc.visible_records(actor)}
    entries = [e for e in svc.audit.entries() if e.record_id in visible or e.actor_id == actor.user_id]
    st.caption("Entries for records you can see, plus your own actions. The log is append-only.")
    st.dataframe([{"at": e.at.strftime("%H:%M:%S"), "actor": e.actor_id, "event": e.event,
                   "record": e.record_id or "", "outcome": e.outcome, "detail": e.detail}
                  for e in reversed(entries)], hide_index=True, width="stretch")
