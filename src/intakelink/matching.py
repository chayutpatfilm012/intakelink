"""Evidence Matcher (C6): deterministic matter proposals with evidence.

The matcher compares extracted facts with the Matter Index. The AI never sees
the index and never picks a matter; every point of every score comes from a
rule below and is listed as Evidence with the fact (and so the page) behind it.

Rules (Solution Design Report §7.5; initial values, not validated):

    rule              weight   fires when
    matter_number     1.0      a matterNumber fact equals the matter ID
    claim_number      0.6      a claimNumber fact is one of the matter's claims
    name_dob          0.5      a clientName fact and a dateOfBirth fact both match
    name_only         0.2      the name matches but no DOB fact confirms it
    incident_date     0.2      an incidentDate fact equals the matter's date
    insurer_provider  0.1      an insurer or provider fact names one of the matter's

Each rule counts at most once per matter. Scores are capped at 1.0.

Outcome:
  * top score < 0.6                 -> no confident match, LOW_CONFIDENCE flag,
                                       no proposal attached to the record
  * top two scores within 0.15      -> AMBIGUOUS_MATCH (blocking)
    (checked only when the top score is confident; a low-confidence result
    already has no proposal to approve)

Comparison is exact after normalization (case, punctuation, date format,
"Last, First" order, middle initials). There is no fuzzy matching: a
misspelled name earns nothing, and a person sees the low score.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .access import MatterIndex
from .fields import (CLAIM_NUMBER, CLIENT_NAME, DATE_OF_BIRTH, INCIDENT_DATE,
                     INSURER, MATTER_NUMBER, PROVIDER)
from .models import (CandidateRecord, Evidence, Fact, Flag, FlagType, Matter,
                     ProcessingStatus, Proposal)

WEIGHTS = {
    "matter_number": 1.0,
    "claim_number": 0.6,
    "name_dob": 0.5,
    "name_only": 0.2,
    "incident_date": 0.2,
    "insurer_provider": 0.1,
}
SCORE_CAP = 1.0
CONFIDENT_THRESHOLD = 0.6
AMBIGUITY_MARGIN = 0.15


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
_ORG_NOISE = {"the", "inc", "llc", "llp", "co", "corp", "company", "pc", "pa"}
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y",
                 "%d %B %Y", "%b. %d, %Y")


def _ascii_fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def norm_id(value: str) -> str:
    """'clm 55-821' -> 'CLM55821'."""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def norm_date(value: str) -> Optional[str]:
    """Return ISO 'YYYY-MM-DD', or None when the text is not a known date form."""
    text = re.sub(r"\s+", " ", value.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def norm_name(value: str) -> tuple[str, ...]:
    """'Whitfield, Dana M.' -> ('dana', 'whitfield'). Suffixes such as Jr. are kept."""
    text = _ascii_fold(value).replace("'", "").replace("’", "")
    if "," in text:
        head, tail = text.split(",", 1)
        tail_tokens = re.findall(r"[a-z0-9]+", tail)
        if tail_tokens and not all(t in _NAME_SUFFIXES for t in tail_tokens):
            text = f"{tail} {head}"       # "Last, First" -> "First Last"
    tokens = [t for t in re.findall(r"[a-z0-9]+", text) if len(t) > 1]
    # Suffixes go last so "Carter, James, Jr." equals "James Carter Jr.".
    return tuple([t for t in tokens if t not in _NAME_SUFFIXES]
                 + [t for t in tokens if t in _NAME_SUFFIXES])


def norm_org(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", _ascii_fold(value).replace("'", ""))
    return " ".join(t for t in tokens if t not in _ORG_NOISE)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
@dataclass
class MatchResult:
    candidates: list[Proposal]                 # every matter with score > 0, best first
    flags: list[Flag] = field(default_factory=list)
    threshold: float = CONFIDENT_THRESHOLD

    @property
    def top(self) -> Optional[Proposal]:
        return self.candidates[0] if self.candidates else None

    @property
    def confident(self) -> bool:
        return self.top is not None and self.top.score >= self.threshold

    @property
    def matter_id(self) -> Optional[str]:
        """The proposed matter, or None when there is no confident match."""
        return self.top.matter_id if self.confident else None

    def has(self, flag_type: FlagType) -> bool:
        return any(f.type == flag_type for f in self.flags)


def _ordered(facts: list[Fact]) -> list[Fact]:
    return sorted(facts, key=lambda f: (f.page if f.page is not None else 0, f.fact_id))


def _first(facts: list[Fact], field_name: str, test) -> Optional[Fact]:
    for fact in facts:
        if fact.field == field_name and fact.value and test(fact.value):
            return fact
    return None


def score_matter(facts: list[Fact], matter: Matter) -> Proposal:
    """Apply every rule to one matter and return its score with evidence."""
    facts = _ordered(facts)
    evidence: list[Evidence] = []

    def add(fact: Fact, matter_field: str, rule: str, weight: Optional[float] = None) -> None:
        evidence.append(Evidence(fact.fact_id, matter_field, rule,
                                 WEIGHTS[rule] if weight is None else weight))

    m_id = norm_id(matter.matter_id)
    hit = _first(facts, MATTER_NUMBER, lambda v: norm_id(v) == m_id)
    if hit:
        add(hit, "matter_id", "matter_number")

    claims = {norm_id(c) for c in matter.claim_numbers}
    hit = _first(facts, CLAIM_NUMBER, lambda v: norm_id(v) in claims)
    if hit:
        add(hit, "claim_numbers", "claim_number")

    client = norm_name(matter.client_name)
    name_hit = _first(facts, CLIENT_NAME, lambda v: norm_name(v) == client)
    if name_hit:
        dob = norm_date(matter.date_of_birth)
        dob_hit = _first(facts, DATE_OF_BIRTH, lambda v: norm_date(v) == dob)
        if dob_hit:
            add(name_hit, "client_name", "name_dob")
            add(dob_hit, "date_of_birth", "name_dob", weight=0.0)  # weight counted once
        else:
            add(name_hit, "client_name", "name_only")

    incident = norm_date(matter.incident_date)
    hit = _first(facts, INCIDENT_DATE, lambda v: norm_date(v) == incident)
    if hit:
        add(hit, "incident_date", "incident_date")

    insurers = {norm_org(x) for x in matter.insurers}
    providers = {norm_org(x) for x in matter.providers}
    hit = (_first(facts, INSURER, lambda v: norm_org(v) in insurers)
           or _first(facts, PROVIDER, lambda v: norm_org(v) in providers))
    if hit:
        add(hit, "insurers" if hit.field == INSURER else "providers", "insurer_provider")

    raw = sum(e.weight for e in evidence)
    return Proposal(matter.matter_id, round(min(SCORE_CAP, raw), 2), evidence)


def _summary(candidates: list[Proposal], n: int = 3) -> str:
    return ", ".join(f"{p.matter_id} {p.score:.2f}" for p in candidates[:n]) or "no matter scored"


def match(facts: list[Fact], index: MatterIndex, *,
          threshold: float = CONFIDENT_THRESHOLD,
          margin: float = AMBIGUITY_MARGIN) -> MatchResult:
    """Score every matter and decide the outcome. Pure: changes nothing.

    ``threshold`` and ``margin`` exist for the sensitivity analysis
    (``intakelink.evaluate``); the system always uses the defaults.
    """
    scored = [score_matter(facts, m) for m in index.all()]
    candidates = sorted((p for p in scored if p.score > 0),
                        key=lambda p: (-p.score, p.matter_id))
    result = MatchResult(candidates, threshold=threshold)

    if not result.confident:
        result.flags.append(Flag(FlagType.LOW_CONFIDENCE, False,
                                 f"no confident match (best: {_summary(candidates)})"))
        return result

    if len(candidates) > 1:
        gap = round(candidates[0].score - candidates[1].score, 2)
        if gap <= margin:
            result.flags.append(Flag(
                FlagType.AMBIGUOUS_MATCH, True,
                f"top matters within {margin:.2f}: {_summary(candidates, 2)}"))
    return result


def run_matching(record: CandidateRecord, index: MatterIndex) -> list[Flag]:
    """Attach proposals to a record and return the matcher's flags.

    The caller combines these with the guard's flags and passes them to
    ``review.write_flags``, which moves the record to READY_FOR_REVIEW.
    Only processing status and proposals change here; review status never does.
    """
    record.processing_status = ProcessingStatus.MATCHING
    result = match(record.facts, index)
    # With no confident match nothing is proposed, so approval needs a person
    # to choose the matter (validate is refused with "no matter selected").
    record.proposals = result.candidates if result.confident else []
    return result.flags


__all__ = ["AMBIGUITY_MARGIN", "CONFIDENT_THRESHOLD", "MatchResult", "WEIGHTS",
           "match", "norm_date", "norm_id", "norm_name", "norm_org",
           "run_matching", "score_matter"]
