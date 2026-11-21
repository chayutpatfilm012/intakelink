"""Held-out set integrity and the evaluation / sensitivity report.

The held-out numbers themselves are NOT asserted (they are measurements, not
requirements), except one safety property: with the system settings, no
document gets a confident proposal for the wrong matter.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from intakelink import evaluate, heldout
from intakelink.intake import prepare_text
from intakelink.matching import match
from intakelink.models import Fact, FlagType
from intakelink.synthetic import MATTERS, matter_index

ROOT = Path(__file__).resolve().parents[1]
HELD = ROOT / "data" / "heldout"


@pytest.fixture(scope="module")
def manifest():
    return json.loads((HELD / "manifest.json").read_text(encoding="utf-8"))


def test_heldout_composition(manifest):
    assert len(manifest) == 40
    assert Counter(e["category"] for e in manifest) == {
        "standard": 30, "cross_matter_tier1": 3, "cross_matter_tier2": 3, "fact_conflict": 4}
    assert Counter(e["template"] for e in manifest if e["category"] == "standard") == \
        {t: 5 for t in heldout.TEMPLATES}
    assert {e["truth_matter"] for e in manifest} <= {m.matter_id for m in MATTERS}


def test_heldout_files_match_the_generator(manifest):
    expected, files = heldout.build()
    assert manifest == expected, "run: python -m intakelink.heldout data/heldout"
    for name, data in files.items():
        assert (HELD / "documents" / name).read_bytes() == data, name


def test_heldout_ground_truth_is_on_its_page(manifest):
    for e in manifest:
        text = prepare_text("D-1", "txt", (HELD / "documents" / e["file"]).read_bytes())
        for f in e["facts"]:
            assert f["value"] in text.page(f["page"]).text, (e["key"], f)


def test_heldout_formats_differ_from_dev():
    # Labels the dev set uses must not be the only way identifiers appear.
    text = (HELD / "documents" / "held-02.txt").read_text(encoding="utf-8")   # discharge
    assert "PATIENT NAME\n" in text and "Patient:" not in text


def test_evaluation_report_is_current():
    committed = (ROOT / "docs" / "EVALUATION.md").read_text(encoding="utf-8")
    assert committed == evaluate.report(), \
        "run: python -m intakelink.evaluate --out docs/EVALUATION.md"


def test_no_wrong_proposals_with_system_settings():
    index = matter_index()
    for name in evaluate.SETS:
        counts = evaluate.outcome_counts(evaluate.load(name), index)
        assert counts["wrong"] == 0, name


def test_dev_extraction_is_perfect_and_heldout_is_not():
    dev = evaluate.extraction_scores(evaluate.load("dev"))
    assert all(tp == got == gold for tp, got, gold in dev.values())
    held = evaluate.extraction_scores(evaluate.load("held-out"))
    assert sum(v[0] for v in held.values()) < sum(v[2] for v in held.values())


def test_match_accepts_threshold_and_margin_for_the_sweep():
    facts = [Fact("F-1", "clientName", "Dana Whitfield", 1, 0.9),
             Fact("F-2", "incidentDate", "2026-06-14", 1, 0.9)]          # 0.4
    index = matter_index()
    assert not match(facts, index).confident
    assert match(facts, index, threshold=0.4).confident
    two = facts + [Fact("F-3", "claimNumber", "CLM-60310", 1, 0.9)]      # 0.6 vs 0.4
    assert not match(two, index).has(FlagType.AMBIGUOUS_MATCH)
    assert match(two, index, margin=0.2).has(FlagType.AMBIGUOUS_MATCH)
