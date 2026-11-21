"""AI Extraction Service (C4): document type and facts, each with its page.

Boundary rules (Solution Design Report §7.2):
  * The extractor sees only the document's page-numbered text. It never sees
    the Matter Index and never picks a matter; this module imports nothing
    that reads the index (a test checks the imports).
  * Every fact carries the page it came from. Facts that cannot be tied to a
    page are dropped.
  * System code here sets processing status, document type and AI facts. It
    never sets review status.

Two modes:
  * ``RuleBasedExtractor`` (default, offline): labeled lines ("Claim No.: ..."),
    letterhead organizations, and claim / matter number patterns. Tuned to the
    synthetic documents in ``data/synthetic``; real office documents would need
    more rules or the LLM mode.
  * ``LLMExtractor`` (optional): Claude via the Anthropic API. Needs the ``llm``
    extra and an API key. Sends document text to an external service, so use
    it with synthetic data only (NFR-12). Its output is re-checked: unknown
    fields, invalid pages, and values not found on the stated page are dropped.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .audit import AuditLog
from .fields import (ALL_FIELDS, CLAIM_NUMBER, CLIENT_NAME, DATE_OF_BIRTH,
                     DOCUMENT_DATE, DOCUMENT_TYPES, INCIDENT_DATE, INSURER,
                     MATTER_NUMBER, PROVIDER)
from .intake import PreparedText
from .models import CandidateRecord, Fact, Origin, ProcessingStatus


class ExtractionError(Exception):
    """The extractor could not produce a result for this document."""


@dataclass
class Extraction:
    document_type: str
    facts: list[Fact]
    rejected: int = 0          # facts dropped by validation (LLM mode)
    extractor: str = ""


class Extractor(Protocol):
    name: str

    def extract(self, text: PreparedText) -> Extraction: ...


# ---------------------------------------------------------------------------
# Shared validation
# ---------------------------------------------------------------------------
def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def on_page(text: PreparedText, page: int, value: str) -> bool:
    """True when ``value`` appears on ``page`` (ignoring case and spacing)."""
    if not 1 <= page <= text.page_count:
        return False
    return _squash(value) in _squash(text.page(page).text)


def _number(facts: list[tuple[str, str, int, float]]) -> list[Fact]:
    """Sort by page and number fact IDs F-1, F-2, ... ; drop exact repeats."""
    seen, out = set(), []
    for fld, value, page, conf in sorted(facts, key=lambda f: f[2]):
        key = (fld, _squash(value), page)
        if key in seen:
            continue
        seen.add(key)
        out.append(Fact(f"F-{len(out) + 1}", fld, value, page=page,
                        confidence=conf, origin=Origin.AI))
    return out


# ---------------------------------------------------------------------------
# Offline rule-based extractor
# ---------------------------------------------------------------------------
LABELS: dict[str, str] = {
    "attorney file": MATTER_NUMBER, "your file": MATTER_NUMBER, "your file no.": MATTER_NUMBER,
    "your file no": MATTER_NUMBER, "our file": MATTER_NUMBER, "our file no.": MATTER_NUMBER,
    "requesting file": MATTER_NUMBER, "matter no.": MATTER_NUMBER, "matter number": MATTER_NUMBER,
    "claim no.": CLAIM_NUMBER, "claim no": CLAIM_NUMBER, "claim #": CLAIM_NUMBER,
    "claim number": CLAIM_NUMBER, "insurance claim": CLAIM_NUMBER,
    "patient": CLIENT_NAME, "patient name": CLIENT_NAME, "party 1": CLIENT_NAME,
    "party 2": CLIENT_NAME, "claimant": CLIENT_NAME, "employee": CLIENT_NAME,
    "re": CLIENT_NAME, "records for": CLIENT_NAME, "client": CLIENT_NAME, "insured": CLIENT_NAME,
    "dob": DATE_OF_BIRTH, "date of birth": DATE_OF_BIRTH, "claimant dob": DATE_OF_BIRTH,
    "date of injury": INCIDENT_DATE, "date of loss": INCIDENT_DATE,
    "date of accident": INCIDENT_DATE, "date of collision": INCIDENT_DATE,
    "date of incident": INCIDENT_DATE,
    "date": DOCUMENT_DATE, "statement date": DOCUMENT_DATE,
    "payer": INSURER, "insurance": INSURER, "insurance carrier": INSURER, "carrier": INSURER,
    "provider": PROVIDER, "transported to": PROVIDER,
}
_LABELED = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 .#/]*?)\s*:\s*(\S.*?)\s*$")
_CLAIM_PATTERN = re.compile(r"\bCLM-\d{5}\b")
_MATTER_PATTERN = re.compile(r"\bM-\d{4}\b")
_PROVIDER_WORDS = re.compile(r"\b(medical|orthopedics|imaging|therapy|chiropractic|"
                             r"rehabilitation|clinic|hospital|health|center)\b", re.I)
_INSURER_WORDS = re.compile(r"\b(insurance|mutual|casualty|assurance|fidelity|indemnity)\b", re.I)

TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "medical_bill": ("statement of charges", "total due", "itemized charges"),
    "medical_record": ("visit note", "subjective:", "assessment:", "progress note"),
    "police_report": ("police", "collision report", "narrative:"),
    "insurer_letter": ("claim correspondence", "date of loss", "evaluation amount"),
    "wage_statement": ("lost wages", "pay period", "gross wages"),
    "correspondence": ("records delivery", "enclosed are", "enclosures:"),
}


def classify(text: PreparedText) -> str:
    body = "\n".join(p.text for p in text.pages).casefold()
    scores = {t: sum(k in body for k in kws) for t, kws in TYPE_KEYWORDS.items()}
    best = max(scores, key=lambda t: scores[t])
    return best if scores[best] else "other"


class RuleBasedExtractor:
    name = "rule-based"

    def extract(self, text: PreparedText) -> Extraction:
        found: list[tuple[str, str, int, float]] = []
        for page in text.pages:
            lines = page.text.split("\n")
            if page.number == 1:
                # Letterhead: the first of the top three lines that names an
                # organization (a fax stamp may sit above it).
                for head in [ln.strip() for ln in lines if ln.strip()][:3]:
                    if ":" in head:
                        continue
                    org = head.split("  ")[0].strip()
                    if _INSURER_WORDS.search(org):
                        found.append((INSURER, org, 1, 0.85))
                        break
                    if _PROVIDER_WORDS.search(org):
                        found.append((PROVIDER, org, 1, 0.85))
                        break
            for line in lines:
                m = _LABELED.match(line)
                fld = LABELS.get(m.group(1).strip().casefold()) if m else None
                if fld:
                    found.append((fld, m.group(2), page.number, 0.95))
                    continue
                for c in _CLAIM_PATTERN.findall(line):
                    found.append((CLAIM_NUMBER, c, page.number, 0.7))
                for c in _MATTER_PATTERN.findall(line):
                    found.append((MATTER_NUMBER, c, page.number, 0.7))
        return Extraction(classify(text), _number(found), 0, self.name)


# ---------------------------------------------------------------------------
# Optional LLM extractor (Claude)
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You read one legal-intake document and report what it says.

Return the document type and every identifying fact printed in it, each with
the page number it appears on. Copy each value exactly as printed (same
spelling, same date format). Report a fact once per page it appears on.

Fields:
- matterNumber: a law-firm file or matter number
- claimNumber: an insurance claim number
- clientName: the name of a person the document is about (patient, claimant,
  employee, party to a collision, person whose records are enclosed)
- dateOfBirth: a person's date of birth
- incidentDate: the date of the injury, loss, accident or collision
- insurer: an insurance company named in the document
- provider: a medical provider named in the document
- documentDate: the date the document was written or issued

Do not guess, infer or correct values, and do not decide which client or file
the document belongs to. If a field is absent, leave it out."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "enum": list(DOCUMENT_TYPES)},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": list(ALL_FIELDS)},
                    "value": {"type": "string"},
                    "page": {"type": "integer"},
                },
                "required": ["field", "value", "page"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["document_type", "facts"],
    "additionalProperties": False,
}


def render_pages(text: PreparedText) -> str:
    return "\n\n".join(f"=== PAGE {p.number} ===\n{p.text}" for p in text.pages)


class LLMExtractor:
    """Claude-based extraction. Pass ``client`` to inject a test double."""

    name = "llm"

    def __init__(self, client: Any = None, model: str = DEFAULT_MODEL,
                 effort: str = "low") -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as err:
                raise ExtractionError("LLM mode needs the optional 'llm' extra "
                                      "(pip install -e .[llm])") from err
            client = anthropic.Anthropic()   # credentials from the environment
        self.client = client
        self.model = model
        self.effort = effort

    def _call(self, text: PreparedText) -> dict:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",                       # re-run on refusal
                system=SYSTEM_PROMPT,
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": render_pages(text)}],
            )
        except Exception as err:   # network, auth, rate limit: fail visibly
            raise ExtractionError(f"LLM request failed: {type(err).__name__}: {err}") from err
        if response.stop_reason == "refusal":
            raise ExtractionError("LLM declined to process this document")
        if response.stop_reason == "max_tokens":
            raise ExtractionError("LLM output was cut off")
        raw = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as err:
            raise ExtractionError("LLM returned invalid JSON") from err

    def extract(self, text: PreparedText) -> Extraction:
        data = self._call(text)
        doc_type = data.get("document_type")
        if doc_type not in DOCUMENT_TYPES:
            doc_type = "other"
        kept: list[tuple[str, str, int, float]] = []
        rejected = 0
        for item in data.get("facts") or []:
            fld, value, page = item.get("field"), str(item.get("value", "")).strip(), item.get("page")
            if (fld in ALL_FIELDS and value and isinstance(page, int)
                    and on_page(text, page, value)):
                kept.append((fld, value, page, 0.9))
            else:
                rejected += 1
        return Extraction(doc_type, _number(kept), rejected, self.name)


# ---------------------------------------------------------------------------
# Record integration
# ---------------------------------------------------------------------------
def extract_into(record: CandidateRecord, text: Optional[PreparedText], extractor: Extractor,
                 audit: AuditLog, system_id: str = "system") -> Optional[Extraction]:
    """Run extraction for a record. On failure the record goes to EXTRACTION_FAILED."""
    if text is None:
        return None
    record.processing_status = ProcessingStatus.EXTRACTING
    try:
        result = extractor.extract(text)
    except ExtractionError as err:
        record.processing_status = ProcessingStatus.EXTRACTION_FAILED
        audit.append(system_id, "extraction_failed", record.record_id, detail=str(err))
        return None
    record.document_type = result.document_type
    record.facts = result.facts
    audit.append(system_id, "extracted", record.record_id,
                 detail=f"{extractor.name}: {result.document_type}, {len(result.facts)} fact(s)"
                        + (f", {result.rejected} rejected" if result.rejected else ""))
    return result


__all__ = ["Extraction", "ExtractionError", "Extractor", "LLMExtractor",
           "RuleBasedExtractor", "classify", "extract_into", "on_page"]
