"""End-to-end demo on the synthetic set: ``python -m intakelink.demo``.

1. Receives all 64 synthetic documents and compares each outcome with the
   hand-written ground truth in data/synthetic/manifest.json.
2. Plays a scripted review session through the real rules: a clean approval,
   a tier-1 override, a tier-2 attorney release, an ambiguous match resolved,
   a low-confidence document assigned, a duplicate rejected, and an
   off-matter view refused.
3. Prints filings, notifications and audit-log totals.

Files go to a fresh work directory (default: a temp folder); the audit log is
written there as audit.jsonl. All data is synthetic.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .models import Action, FlagType, ProcessingStatus
from .review import ReviewError
from .service import IntakeLinkService
from .synthetic import USERS, load_manifest, matter_index

DATA = Path(__file__).resolve().parents[2] / "data" / "synthetic"


def outcome_of(record) -> str:
    if record.processing_status == ProcessingStatus.EXTRACTION_FAILED:
        return "EXTRACTION_FAILED"
    types = {f.type for f in record.flags}
    if FlagType.AMBIGUOUS_MATCH in types:
        return "AMBIGUOUS_MATCH"
    if FlagType.LOW_CONFIDENCE in types:
        return "LOW_CONFIDENCE"
    return "CONFIDENT"


def load_set(service: IntakeLinkService, data_dir: Path = DATA) -> dict[str, str]:
    """Receive every synthetic document; return {manifest key: record_id}."""
    ids = {}
    for e in load_manifest(data_dir):
        rec = service.receive((data_dir / "documents" / e["file"]).read_bytes(), e["file"],
                              e["channel"], datetime.fromisoformat(e["received_at"]))
        ids[e["key"]] = rec.record_id
    return ids


def check_against_manifest(service, ids, data_dir: Path = DATA) -> list[str]:
    problems = []
    for e in load_manifest(data_dir):
        rec = service.records[ids[e["key"]]]
        got = outcome_of(rec)
        if got != e["expected_outcome"]:
            problems.append(f"{e['file']}: expected {e['expected_outcome']}, got {got}")
        elif got == "CONFIDENT" and rec.target_matter_id() != e["truth_matter"]:
            problems.append(f"{e['file']}: proposed {rec.target_matter_id()}, truth {e['truth_matter']}")
    return problems


def run_script(service: IntakeLinkService, ids: dict[str, str],
               say: Callable[[str], None]) -> list[tuple[str, bool]]:
    """The scripted session. Returns (step, behaved as expected) pairs."""
    steps: list[tuple[str, bool]] = []

    def act(key, action, user, *, reason=None, change=None, expect_error: Optional[str] = None):
        rid = ids[key]
        rec = service.records[rid]
        label = f"{user} {action.value} {rid} ({key})"
        try:
            service.act(rid, action, USERS[user], expected_version=rec.version,
                        reason=reason, change=change)
            ok = expect_error is None
            say(f"  {'ok ' if ok else 'BAD'} {label} -> {rec.review_status.value}")
        except ReviewError as err:
            ok = err.code == expect_error
            say(f"  {'ok ' if ok else 'BAD'} {label} refused: {err.code}")
        steps.append((label, ok))

    say("1. Clean document: paralegal approves, system files it")
    act("S01", Action.VALIDATE, "p.lee")

    say("2. Tier 1 (another client's name on page 2)")
    act("X01", Action.VALIDATE, "p.lee", expect_error="GUARD_BLOCKED")
    act("X01", Action.OVERRIDE_GUARD, "p.lee",
        reason="page 2 name is a waiting-room sign-in, no records of that person")
    act("X01", Action.VALIDATE, "p.lee")

    say("3. Tier 2 (another client's name + DOB): held for the responsible attorney")
    act("X06", Action.VALIDATE, "s.ortiz", expect_error="GUARD_BLOCKED")
    act("X06", Action.RELEASE_HOLD, "a.reyes",
        reason="page 2 removed and returned to the provider; rest belongs here")
    act("X06", Action.VALIDATE, "s.ortiz", expect_error="NOT_AUTHORIZED")
    act("X06", Action.VALIDATE, "a.reyes")

    say("4. Ambiguous: same client, two matters")
    act("A03", Action.VALIDATE, "p.lee", expect_error="GUARD_BLOCKED")
    act("A03", Action.CORRECT, "p.lee", change={"selected_matter_id": "M-1407"},
        reason="letter date is after the July 2026 accident; M-1389 claim is closed")
    act("A03", Action.VALIDATE, "p.lee")

    say("5. Low confidence (name only): assigned by a person")
    act("L02", Action.VALIDATE, "p.garcia", expect_error="GUARD_BLOCKED")
    act("L02", Action.CORRECT, "p.garcia", change={"selected_matter_id": "M-1219"},
        reason="client confirmed by phone that she sent this letter")
    act("L02", Action.VALIDATE, "p.garcia")

    say("6. Exact duplicate: rejected with a reason")
    act("D02", Action.REJECT, "p.lee", reason="exact duplicate of an earlier upload")

    say("7. Off-matter staff cannot open a record")
    try:
        service.view(ids["S01"], USERS["s.baker"])
        steps.append(("s.baker view S01", False))
        say("  BAD s.baker opened S01")
    except ReviewError as err:
        steps.append(("s.baker view S01", err.code == "NOT_AUTHORIZED"))
        say(f"  ok  s.baker view {ids['S01']} (S01) refused: {err.code}")
    return steps


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=None,
                        help="work directory (default: a new temp folder)")
    args = parser.parse_args(argv)
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="intakelink-demo-"))
    if (workdir / "originals").exists() and any((workdir / "originals").iterdir()):
        print(f"{workdir} already has stored documents; choose an empty --workdir", file=sys.stderr)
        return 2

    service = IntakeLinkService(workdir, matter_index(), audit_path=workdir / "audit.jsonl")
    ids = load_set(service)
    counts = Counter(outcome_of(r) for r in service.records.values())
    problems = check_against_manifest(service, ids)
    print(f"Received {len(service.records)} synthetic documents into {workdir}")
    for name in ("CONFIDENT", "AMBIGUOUS_MATCH", "LOW_CONFIDENCE", "EXTRACTION_FAILED"):
        print(f"  {name:<18} {counts[name]}")
    held = sum(r.hold_status.value == "HELD_FOR_ATTORNEY" for r in service.records.values())
    print(f"  held for attorney  {held}")
    print(f"Matches ground truth: {len(ids) - len(problems)}/{len(ids)}")
    for p in problems:
        print(f"  MISMATCH {p}")

    print("\nScripted review session")
    steps = run_script(service, ids, print)

    print(f"\nFiled to mock case-management system: {len(service.cms.all())}")
    for r in service.cms.all():
        print(f"  {r.cms_ref}  {r.record_id}  {r.document_type}  approved by {r.approved_by}")
    print(f"Notifications sent: {len(service.notifier.all())}")
    print(f"Audit log: {len(service.audit)} entries, {len(service.audit.denials())} refusals "
          f"-> {workdir / 'audit.jsonl'}")

    failed = [s for s, ok in steps if not ok]
    if problems or failed:
        print(f"\nDEMO FAILED: {len(problems)} mismatches, {len(failed)} unexpected steps")
        return 1
    print("\nAll outcomes and scripted steps behaved as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
