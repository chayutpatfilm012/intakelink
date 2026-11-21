"""Evaluation and threshold sensitivity: ``python -m intakelink.evaluate``.

Measures the offline pipeline (rule-based extraction -> Evidence Matcher ->
guard) on two sets:
  * dev:      data/synthetic (the format the extractor was written for)
  * held-out: data/heldout   (formats it was not written for)

Reports:
  1. Extraction precision / recall per field (value compared after the
     matcher's normalization; the page must match too).
  2. Matching outcome per document, with the system's default settings:
       correct proposal   confident, not ambiguous, the true matter
       WRONG proposal     confident, not ambiguous, another matter (a reviewer
                          must catch it; nothing is filed without approval)
       sent to a person   low confidence or ambiguous
  3. Guard detection (cross-matter tiers, fact conflicts) and false alarms.
  4. Sensitivity: the same matching outcome for a grid of thresholds and
     ambiguity margins. Weights are not varied.

``--out FILE`` also writes the report as Markdown. All data is synthetic.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .access import MatterIndex
from .extraction import RuleBasedExtractor
from .fields import DOCUMENT_DATE
from .guard import cross_matter_findings, fact_conflict_findings
from .intake import TextPrepError, prepare_text
from .matching import (AMBIGUITY_MARGIN, CONFIDENT_THRESHOLD, match, norm_date,
                       norm_id, norm_name, norm_org)
from .models import Fact, FlagType
from .synthetic import gold_facts, matter_index

ROOT = Path(__file__).resolve().parents[2] / "data"
SETS = {"dev": ROOT / "synthetic", "held-out": ROOT / "heldout"}
THRESHOLDS = (0.4, 0.5, 0.6, 0.7, 0.8)
MARGINS = (0.0, 0.1, 0.2, 0.3)
EVAL_FIELDS = ("matterNumber", "claimNumber", "clientName", "dateOfBirth",
               "incidentDate", "insurer", "provider")
_NORM = {"matterNumber": norm_id, "claimNumber": norm_id, "clientName": norm_name,
         "dateOfBirth": norm_date, "incidentDate": norm_date,
         "insurer": norm_org, "provider": norm_org}


def _key(fld: str, value: str):
    normalized = _NORM[fld](value)
    return normalized if normalized else " ".join(value.casefold().split())


@dataclass
class Doc:
    entry: dict
    gold: list[Fact]
    extracted: list[Fact] = field(default_factory=list)
    doc_type: Optional[str] = None


def load(name: str) -> list[Doc]:
    root = SETS[name]
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    docs = []
    for e in manifest:
        if e["category"] == "corrupted":
            continue
        data = (root / "documents" / e["file"]).read_bytes()
        try:
            text = prepare_text("D-0000", e["file"].rsplit(".", 1)[1], data)
        except TextPrepError:
            continue
        result = RuleBasedExtractor().extract(text)
        docs.append(Doc(e, gold_facts(e), result.facts, result.document_type))
    return docs


# --- 1. extraction ---------------------------------------------------------------
def extraction_scores(docs: list[Doc]) -> dict[str, tuple[int, int, int]]:
    """{field: (true positives, extracted, gold)}"""
    out = {f: [0, 0, 0] for f in EVAL_FIELDS}
    for d in docs:
        gold = Counter((f.field, f.page, _key(f.field, f.value)) for f in d.gold
                       if f.field != DOCUMENT_DATE)
        got = Counter((f.field, f.page, _key(f.field, f.value)) for f in d.extracted
                      if f.field != DOCUMENT_DATE)
        for (fld, _, _), n in gold.items():
            out[fld][2] += n
        for (fld, _, _), n in got.items():
            out[fld][1] += n
        for k, n in (gold & got).items():
            out[k[0]][0] += n
    return {f: tuple(v) for f, v in out.items()}


# --- 2 + 4. matching -----------------------------------------------------------------
def outcome(facts: list[Fact], truth: Optional[str], index: MatterIndex,
            threshold: float = CONFIDENT_THRESHOLD, margin: float = AMBIGUITY_MARGIN) -> str:
    r = match(facts, index, threshold=threshold, margin=margin)
    if not r.confident or r.has(FlagType.AMBIGUOUS_MATCH):
        return "to_person"
    return "correct" if r.matter_id == truth else "wrong"


def outcome_counts(docs, index, source="extracted", **kw) -> Counter:
    return Counter(outcome(d.extracted if source == "extracted" else d.gold,
                           d.entry.get("truth_matter"), index, **kw) for d in docs)


# --- 3. guard ------------------------------------------------------------------------
def guard_scores(docs: list[Doc], index: MatterIndex) -> dict[str, tuple[int, int]]:
    """{metric: (hits, total)} with the system's default settings."""
    s = {"cross-matter found": [0, 0], "cross-matter right tier": [0, 0],
         "cross-matter doc stopped (any flag, or sent to a person)": [0, 0],
         "fact conflict found": [0, 0],
         "fact-conflict doc stopped (any blocking flag, or sent to a person)": [0, 0],
         "false alarm on standard docs": [0, 0]}
    for d in docs:
        r = match(d.extracted, index)
        # Same target as run_checks: the top proposal once there is a confident match.
        target = index.get(r.top.matter_id) if r.confident else None
        cross = cross_matter_findings(d.extracted, target, index) if target else []
        conflicts = fact_conflict_findings(d.extracted, target, index) if target else []
        to_person = not r.confident or r.has(FlagType.AMBIGUOUS_MATCH)
        cm = d.entry.get("cross_matter")
        if cm:
            for k in ("cross-matter found", "cross-matter right tier",
                      "cross-matter doc stopped (any flag, or sent to a person)"):
                s[k][1] += 1
            mine = [c for c in cross if c.other_matter_id == cm["matter"]]
            s["cross-matter found"][0] += bool(mine)
            s["cross-matter right tier"][0] += any(c.tier == cm["tier"] for c in mine)
            s["cross-matter doc stopped (any flag, or sent to a person)"][0] += bool(cross) or to_person
        if d.entry.get("fact_conflict"):
            s["fact conflict found"][1] += 1
            s["fact-conflict doc stopped (any blocking flag, or sent to a person)"][1] += 1
            s["fact conflict found"][0] += bool(conflicts)
            s["fact-conflict doc stopped (any blocking flag, or sent to a person)"][0] += \
                bool(conflicts or cross) or to_person
        if d.entry["category"] == "standard":
            s["false alarm on standard docs"][1] += 1
            s["false alarm on standard docs"][0] += bool(cross or conflicts)
    return {k: tuple(v) for k, v in s.items()}


# --- report --------------------------------------------------------------------------
def _pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "n/a"


def report() -> str:
    index = matter_index()
    data = {name: load(name) for name in SETS}
    L = ["# IntakeLink evaluation",
         "",
         "Generated by `python -m intakelink.evaluate --out docs/EVALUATION.md`. "
         "Do not edit by hand. All data is synthetic.",
         "",
         "- **dev**: `data/synthetic`, the format the rule-based extractor was written for.",
         "- **held-out**: `data/heldout`, six formats the extractor was not written for. "
         "The extractor was not changed after these results.",
         "",
         f"System settings: threshold {CONFIDENT_THRESHOLD}, ambiguity margin {AMBIGUITY_MARGIN}. "
         "Corrupted files are left out.",
         "", "## 1. Extraction (rule-based, offline)", "",
         "| Field | dev precision | dev recall | held-out precision | held-out recall |",
         "| --- | --- | --- | --- | --- |"]
    scores = {n: extraction_scores(d) for n, d in data.items()}
    for f in EVAL_FIELDS:
        cells = []
        for n in SETS:
            tp, got, gold = scores[n][f]
            cells += [_pct(tp, got), _pct(tp, gold)]
        L.append(f"| {f} | " + " | ".join(cells) + " |")
    for n, docs in data.items():
        right = sum(d.doc_type == d.entry["document_type"] for d in docs)
        L.append(f"\nDocument type accuracy, {n}: {_pct(right, len(docs))}.")

    L += ["", "## 2. Matching outcome (system settings)", "",
          "A **wrong proposal** is still not filed: a reviewer must approve every record. "
          "It is the case the reviewer has to catch.", "",
          "| Set | Facts from | Documents | Correct proposal | Wrong proposal | Sent to a person |",
          "| --- | --- | --- | --- | --- | --- |"]
    for n, docs in data.items():
        for source in ("extracted", "ground truth"):
            c = outcome_counts(docs, index, "extracted" if source == "extracted" else "gold")
            L.append(f"| {n} | {source} | {len(docs)} | {c['correct']} | {c['wrong']} | {c['to_person']} |")
    L.append("\nThe ground-truth rows show the matcher with perfect extraction; "
             "the gap to the extracted rows is caused by extraction.")

    by_template: dict[str, Counter] = {}
    for d in data["held-out"]:
        by_template.setdefault(d.entry["template"], Counter())[
            outcome(d.extracted, d.entry["truth_matter"], index)] += 1
    L += ["", "Held-out, by template (extracted facts):", "",
          "| Template | Correct | Wrong | Sent to a person |", "| --- | --- | --- | --- |"]
    for t, c in by_template.items():
        L.append(f"| {t} | {c['correct']} | {c['wrong']} | {c['to_person']} |")

    L += ["", "## 3. Guard detection (extracted facts, system settings)", "",
          "| Metric | dev | held-out |", "| --- | --- | --- |"]
    guards = {n: guard_scores(d, index) for n, d in data.items()}
    for metric in guards["dev"]:
        L.append(f"| {metric} | " + " | ".join(_pct(*guards[n][metric]) for n in SETS) + " |")
    L.append("\nThe guard runs only when the matcher has a confident target. "
             "A document sent to a person is checked again after the person picks the matter.")

    L += ["", "## 4. Sensitivity: threshold and ambiguity margin", "",
          "Weights are all multiples of 0.1, so scores and gaps are multiples of 0.1 too. "
          "Thresholds between steps behave like the next step up, and the system margin "
          "0.15 behaves exactly like 0.1.", "",
          "Each cell: correct / **wrong** / sent to a person (extracted facts). "
          "In the dev set, \"wrong\" also counts a conflicting document that has no single "
          "correct matter but was proposed for one anyway.", "",
          "| Set | Threshold | " + " | ".join(f"margin {m}" for m in MARGINS) + " |",
          "| --- | --- | " + " | ".join("---" for _ in MARGINS) + " |"]
    for n, docs in data.items():
        for t in THRESHOLDS:
            cells = []
            for m in MARGINS:
                c = outcome_counts(docs, index, threshold=t, margin=m)
                cell = f"{c['correct']} / **{c['wrong']}** / {c['to_person']}"
                if t == CONFIDENT_THRESHOLD and m == 0.1:
                    cell += " (system)"
                cells.append(cell)
            L.append(f"| {n} | {t} | " + " | ".join(cells) + " |")
    return "\n".join(L) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate IntakeLink on the synthetic sets")
    parser.add_argument("--out", type=Path, help="also write the Markdown report here")
    args = parser.parse_args(argv)
    text = report()
    print(text)
    if args.out:
        args.out.write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
