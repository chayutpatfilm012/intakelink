"""Slice 3: the synthetic matter index and 64-document test set (report §8.3).

These tests check the data itself (composition, ground truth on the right
page, files in sync with the generator) and run the Evidence Matcher on the
ground-truth facts of every readable document.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from intakelink.audit import AuditLog
from intakelink.intake import DocumentStore, Intake
from intakelink.matching import match
from intakelink.models import FlagType, ProcessingStatus
from intakelink.synthetic import (MATTERS, build_dataset, gold_facts,
                                  load_manifest, load_matter_index, matters_json)

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"


@pytest.fixture(scope="module")
def manifest() -> list[dict]:
    return load_manifest(DATA)


@pytest.fixture(scope="module")
def index():
    return load_matter_index(DATA / "matters.json")


def _readable(manifest):
    return [e for e in manifest if e["category"] != "corrupted"]


# --- composition ---------------------------------------------------------------
def test_twenty_matters_with_unique_ids_and_claims():
    assert len(MATTERS) == 20
    assert len({m.matter_id for m in MATTERS}) == 20
    claims = [c for m in MATTERS for c in m.claim_numbers]
    assert len(claims) == len(set(claims))


def test_sixty_four_documents_in_the_planned_mix(manifest):
    counts = Counter(e["category"] for e in manifest)
    assert len(manifest) == 64
    assert counts["standard"] == 30
    assert counts["ambiguous"] == 10
    assert counts["cross_matter_tier1"] == 5
    assert counts["cross_matter_tier2"] == 5
    assert counts["corrupted"] == 2
    kinds = Counter(e["duplicate"]["kind"] for e in manifest if e["duplicate"])
    assert kinds == {"exact": 2, "probable": 2, "newer_version": 2}
    assert counts["duplicate_original"] == 6


def test_expected_outcomes_mix(manifest):
    ambiguous = Counter(e["expected_outcome"] for e in manifest if e["category"] == "ambiguous")
    assert ambiguous == {"AMBIGUOUS_MATCH": 5, "LOW_CONFIDENCE": 3, "CONFIDENT": 2}
    conflicts = [e for e in manifest if e["fact_conflict"]]
    assert len(conflicts) == 2 and all(e["category"] == "ambiguous" for e in conflicts)


# --- files match the generator ---------------------------------------------------
def test_committed_files_match_the_generator(manifest):
    expected_manifest, files = build_dataset()
    assert manifest == expected_manifest, "run: python -m intakelink.synthetic data/synthetic"
    for name, data in files.items():
        assert (DATA / "documents" / name).read_bytes() == data, name
    assert load_matter_index(DATA / "matters.json").all() == MATTERS
    assert len(matters_json()) == 20


def test_manifest_hashes_match_files(manifest):
    for e in manifest:
        data = (DATA / "documents" / e["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == e["sha256"]


# --- documents through intake ---------------------------------------------------
@pytest.fixture(scope="module")
def received(manifest, tmp_path_factory):
    intake = Intake(DocumentStore(tmp_path_factory.mktemp("store")), AuditLog())
    out = {}
    for e in manifest:
        data = (DATA / "documents" / e["file"]).read_bytes()
        out[e["file"]] = intake.receive(data, e["file"], e["channel"],
                                        datetime.fromisoformat(e["received_at"]))
    return out


def test_corrupted_files_fail_text_preparation(manifest, received):
    for e in manifest:
        failed = received[e["file"]].record.processing_status == ProcessingStatus.EXTRACTION_FAILED
        assert failed is (e["category"] == "corrupted"), e["file"]


def test_every_ground_truth_fact_is_on_its_stated_page(manifest, received):
    for e in _readable(manifest):
        text = received[e["file"]].text
        assert e["facts"], e["file"]
        for f in e["facts"]:
            assert f["value"] in text.page(f["page"]).text, (e["file"], f)


def test_cross_matter_identifiers_are_not_on_page_one(manifest):
    other = {m.matter_id: m for m in MATTERS}
    for e in manifest:
        if not e["cross_matter"]:
            continue
        foreign = other[e["cross_matter"]["matter"]]
        page1 = [f["value"] for f in e["facts"] if f["page"] == 1]
        assert foreign.client_name not in " ".join(page1), e["file"]


def test_duplicates_follow_their_original(manifest):
    order = {e["file"]: i for i, e in enumerate(manifest)}
    by_file = {e["file"]: e for e in manifest}
    for e in manifest:
        if not e["duplicate"]:
            continue
        original = by_file[e["duplicate"]["of"]]
        assert order[e["file"]] > order[original["file"]]
        assert e["truth_matter"] == original["truth_matter"]
        same_bytes = e["sha256"] == original["sha256"]
        assert same_bytes is (e["duplicate"]["kind"] == "exact")


def test_no_document_names_a_person_outside_the_index(manifest):
    known = {m.client_name for m in MATTERS} | {"Desmon Okafor"}   # L03: deliberate typo
    for e in _readable(manifest):
        for f in e["facts"]:
            if f["field"] == "clientName":
                first_last = " ".join(reversed(f["value"].split(", "))) if ", " in f["value"] else f["value"]
                assert first_last in known, (e["file"], f["value"])


# --- Evidence Matcher on the ground-truth facts ---------------------------------
def _outcome(result) -> str:
    if result.has(FlagType.AMBIGUOUS_MATCH):
        return "AMBIGUOUS_MATCH"
    if result.has(FlagType.LOW_CONFIDENCE):
        return "LOW_CONFIDENCE"
    return "CONFIDENT"


@pytest.mark.parametrize("key", [e["key"] for e in load_manifest(DATA) if e["category"] != "corrupted"])
def test_matcher_outcome_on_each_document(key, manifest, index):
    e = next(x for x in manifest if x["key"] == key)
    result = match(gold_facts(e), index)
    assert _outcome(result) == e["expected_outcome"], [(p.matter_id, p.score) for p in result.candidates]
    if e["expected_outcome"] == "CONFIDENT":
        assert result.matter_id == e["truth_matter"]
    if e["expected_outcome"] == "AMBIGUOUS_MATCH":
        assert {p.matter_id for p in result.candidates[:2]} == set(e["candidate_matters"])
    if e["cross_matter"]:
        # The other client's matter scores, but below the true matter.
        scores = {p.matter_id: p.score for p in result.candidates}
        assert 0 < scores[e["cross_matter"]["matter"]] < scores[e["truth_matter"]]


def test_matcher_summary_on_the_set(manifest, index):
    """Never-wrong check: no confident proposal names the wrong matter."""
    wrong = [e["file"] for e in _readable(manifest)
             if (r := match(gold_facts(e), index)).confident
             and not r.has(FlagType.AMBIGUOUS_MATCH)
             and r.matter_id != e["truth_matter"]]
    assert wrong == []
