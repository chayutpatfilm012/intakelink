"""Slice 5: AI Extraction Service (C4), offline rule-based and optional LLM mode."""
from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import intakelink.extraction as extraction
from intakelink.audit import AuditLog
from intakelink.extraction import (ExtractionError, LLMExtractor,
                                   RuleBasedExtractor, extract_into, render_pages)
from intakelink.guard import PriorDocument, run_checks
from intakelink.intake import DocumentStore, Intake, prepare_text
from intakelink.models import FlagType, Origin, ProcessingStatus
from intakelink.synthetic import load_manifest, matter_index

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
MANIFEST = load_manifest(DATA)
READABLE = [e for e in MANIFEST if e["category"] != "corrupted"]


def _text(entry):
    data = (DATA / "documents" / entry["file"]).read_bytes()
    return prepare_text("D-0001", entry["file"].rsplit(".", 1)[1], data)


# --- boundary: the extractor never sees the Matter Index -----------------------
def test_extraction_module_imports_nothing_that_reads_the_index():
    tree = ast.parse(inspect.getsource(extraction))
    imported = {node.module for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}
    assert not imported & {"access", "matching", "guard", "review", "synthetic"}
    assert "MatterIndex" not in inspect.getsource(extraction)


def test_extract_takes_only_the_document_text():
    for cls in (RuleBasedExtractor, LLMExtractor):
        assert list(inspect.signature(cls.extract).parameters) == ["self", "text"]


# --- rule-based extractor on the synthetic set ----------------------------------
@pytest.mark.parametrize("entry", READABLE, ids=[e["key"] for e in READABLE])
def test_rule_based_matches_ground_truth(entry):
    result = RuleBasedExtractor().extract(_text(entry))
    assert result.document_type == entry["document_type"]
    assert sorted((f.field, f.value, f.page) for f in result.facts) == \
        sorted((f["field"], f["value"], f["page"]) for f in entry["facts"])


def test_every_fact_has_a_page_and_is_marked_ai():
    for entry in READABLE[:10]:
        text = _text(entry)
        for f in RuleBasedExtractor().extract(text).facts:
            assert f.page is not None and f.origin == Origin.AI
            assert f.value in text.page(f.page).text


def test_unlabeled_claim_number_is_found_with_lower_confidence():
    text = prepare_text("D-1", "txt", b"Letter\n\fPlease also see claim CLM-60310 for this payment.")
    facts = RuleBasedExtractor().extract(text).facts
    assert [(f.field, f.value, f.page) for f in facts] == [("claimNumber", "CLM-60310", 2)]
    assert facts[0].confidence < 0.95


def test_unknown_document_is_other_with_no_facts():
    result = RuleBasedExtractor().extract(prepare_text("D-1", "txt", b"Hello there.\nNothing here."))
    assert result.document_type == "other" and result.facts == []


# --- end to end without ground truth: intake -> extraction -> checks -------------
@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    audit = AuditLog()
    intake = Intake(DocumentStore(tmp_path_factory.mktemp("store")), audit)
    index, history, out = matter_index(), [], {}
    for e in MANIFEST:
        got = intake.receive((DATA / "documents" / e["file"]).read_bytes(), e["file"],
                             e["channel"], datetime.fromisoformat(e["received_at"]))
        extract_into(got.record, got.text, RuleBasedExtractor(), audit)
        if got.text is not None:
            run_checks(got.record, got.text, index, history, audit)
            history.append(PriorDocument.from_record(got.record, got.text))
        out[e["key"]] = (e, got.record)
    return out


def test_end_to_end_outcomes_match_the_manifest(pipeline):
    for key, (e, record) in pipeline.items():
        types = {f.type for f in record.flags}
        if e["expected_outcome"] == "EXTRACTION_FAILED":
            assert record.processing_status == ProcessingStatus.EXTRACTION_FAILED, key
            continue
        assert record.processing_status == ProcessingStatus.READY_FOR_REVIEW, key
        if e["expected_outcome"] == "CONFIDENT":
            assert record.target_matter_id() == e["truth_matter"], key
            assert FlagType.AMBIGUOUS_MATCH not in types and FlagType.LOW_CONFIDENCE not in types, key
        else:
            assert FlagType[e["expected_outcome"]] in types, key
        if e["cross_matter"]:
            want = "CROSS_MATTER_RISK" if e["cross_matter"]["tier"] == 2 else "CROSS_MATTER_NAME"
            assert FlagType[want] in types, key


# --- LLM mode with a fake client (no network, no key) ----------------------------
class FakeMessages:
    def __init__(self, payload=None, stop_reason="end_turn", error=None):
        self.payload, self.stop_reason, self.error = payload, stop_reason, error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def fake_client(**kw):
    messages = FakeMessages(**kw)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


TWO_PAGES = prepare_text("D-1", "txt", "Bayview Orthopedics\nPatient: Whitfield, Dana\n"
                                       "\fClaim No.: CLM-55821\n".encode())


def test_llm_output_is_validated_against_the_pages():
    client, _ = fake_client(payload={"document_type": "medical_bill", "facts": [
        {"field": "clientName", "value": "Whitfield, Dana", "page": 1},       # kept
        {"field": "claimNumber", "value": "CLM-55821", "page": 2},            # kept
        {"field": "claimNumber", "value": "CLM-55821", "page": 1},            # wrong page
        {"field": "dateOfBirth", "value": "03/12/1984", "page": 1},           # not in text
        {"field": "matterId", "value": "M-1042", "page": 1},                  # unknown field
        {"field": "provider", "value": "Bayview Orthopedics", "page": 9},     # no such page
    ]})
    result = LLMExtractor(client=client).extract(TWO_PAGES)
    assert [(f.field, f.page) for f in result.facts] == [("clientName", 1), ("claimNumber", 2)]
    assert result.rejected == 4


def test_llm_request_carries_only_the_document():
    client, messages = fake_client(payload={"document_type": "other", "facts": []})
    LLMExtractor(client=client).extract(TWO_PAGES)
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["messages"] == [{"role": "user", "content": render_pages(TWO_PAGES)}]
    assert call["output_config"]["format"]["type"] == "json_schema"
    sent = json.dumps(call)
    for matter in matter_index().all():          # no index data in the request
        assert matter.matter_id not in sent


def test_unknown_document_type_from_llm_becomes_other():
    client, _ = fake_client(payload={"document_type": "spaceship_manual", "facts": []})
    assert LLMExtractor(client=client).extract(TWO_PAGES).document_type == "other"


@pytest.mark.parametrize("kw, why", [
    ({"payload": {}, "stop_reason": "refusal"}, "declined"),
    ({"payload": {}, "stop_reason": "max_tokens"}, "cut off"),
    ({"error": RuntimeError("connection reset")}, "request failed"),
])
def test_llm_failures_mark_the_record_failed(kw, why):
    from conftest import make_record
    client, _ = fake_client(**kw)
    audit = AuditLog()
    record = make_record(audit=audit)
    with pytest.raises(ExtractionError, match=why):
        LLMExtractor(client=client).extract(TWO_PAGES)
    assert extract_into(record, TWO_PAGES, LLMExtractor(client=client), audit) is None
    assert record.processing_status == ProcessingStatus.EXTRACTION_FAILED
    assert audit.entries()[-1].event == "extraction_failed"


def test_llm_mode_without_the_sdk_fails_clearly():
    try:
        import anthropic  # noqa: F401
        pytest.skip("anthropic is installed")
    except ImportError:
        with pytest.raises(ExtractionError, match="llm"):
            LLMExtractor()
