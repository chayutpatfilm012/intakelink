"""Exception & Cross-Matter Guard (C7) and duplicate checks.

Cross-matter check (runs once the record has a target matter):
  * tier 2, CROSS_MATTER_RISK (blocking; ``write_flags`` sets the attorney hold):
    another client's matter number, claim number, or name + date of birth
  * tier 1, CROSS_MATTER_NAME (blocking until a reviewer overrides with a reason):
    another client's name alone
  Another matter of the *same* client (same name and DOB) is not foreign.
  A name shared by two different clients is not foreign on its own; it becomes
  tier 2 only if the other client's DOB also appears.
  Flag details name the page and the kind of identifier, never the other
  matter or client, so a reviewer on this matter learns nothing new about it.

Fact conflict, FACT_CONFLICT (blocking; FR-07, report §7.5):
  A name on the document matches the target matter's client, but a date of
  birth on the same page differs from that matter's record. Not raised when
  that name + DOB belongs to another client's matter (tier 2 / the matcher
  cover it). Only dates that parse are compared. The detail names the field
  and page, never the record's value. The reviewer corrects the fact or the
  matter (checks re-run), or rejects / escalates with a reason.

Duplicate check, against documents received earlier:
  * EXACT_DUPLICATE (blocking, report §7.5): identical bytes (same SHA-256).
    The reviewer rejects it with a reason, or escalates.
  * POSSIBLE_NEW_VERSION (warning): same type and identifiers, similar text,
    and a revision marker ("revised", "amended", ...) the earlier one lacks
  * POSSIBLE_DUPLICATE (warning): same type, identifiers and document date,
    similar text (a re-sent fax or re-scan)

System code here writes flags through ``review.write_flags`` only; it never
sets review status.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .access import MatterIndex
from .audit import AuditLog
from .fields import (CLAIM_NUMBER, CLIENT_NAME, DATE_OF_BIRTH, DOCUMENT_DATE,
                     INCIDENT_DATE, INSURER, MATTER_NUMBER, PROVIDER)
from .intake import PreparedText
from .matching import norm_date, norm_id, norm_name, norm_org, run_matching
from .models import CandidateRecord, Fact, Flag, FlagType, Matter
from .review import write_flags

SIMILARITY_THRESHOLD = 0.8
_REVISION = re.compile(r"\b(revised|amended|corrected|supersedes|superseded|updated)\b", re.I)


# ---------------------------------------------------------------------------
# Cross-matter guard
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CrossMatterFinding:
    tier: int
    other_matter_id: str      # for the system (notification); not shown in flag detail
    page: Optional[int]
    fact_ids: tuple[str, ...]
    evidence: str             # "matter number" | "claim number" | "name and date of birth" | "name"

    def to_flag(self) -> Flag:
        where = f"page {self.page}" if self.page else "staff-entered fact"
        if self.tier == 2:
            return Flag(FlagType.CROSS_MATTER_RISK, True,
                        f"{where}: {self.evidence} belonging to another client's matter")
        return Flag(FlagType.CROSS_MATTER_NAME, True,
                    f"{where}: a name that matches another client's matter")


def _first(facts: list[Fact], field_name: str, test) -> Optional[Fact]:
    ordered = sorted(facts, key=lambda f: (f.page or 0, f.fact_id))
    return next((f for f in ordered if f.field == field_name and f.value and test(f.value)), None)


def cross_matter_findings(facts: list[Fact], target: Matter,
                          index: MatterIndex) -> list[CrossMatterFinding]:
    t_name = norm_name(target.client_name)
    t_dob = norm_date(target.date_of_birth)
    findings: list[CrossMatterFinding] = []
    for other in index.all():
        if other.matter_id == target.matter_id:
            continue
        o_name = norm_name(other.client_name)
        o_dob = norm_date(other.date_of_birth)
        if o_name == t_name and o_dob == t_dob:
            continue                      # same client, another matter: not foreign

        hits: list[tuple[Fact, ...]] = []
        tier, evidence = 0, ""
        mn = _first(facts, MATTER_NUMBER, lambda v: norm_id(v) == norm_id(other.matter_id))
        claims = {norm_id(c) for c in other.claim_numbers}
        cl = _first(facts, CLAIM_NUMBER, lambda v: norm_id(v) in claims)
        name = _first(facts, CLIENT_NAME, lambda v: norm_name(v) == o_name)
        dob = _first(facts, DATE_OF_BIRTH, lambda v: norm_date(v) == o_dob) if o_dob != t_dob else None

        if mn:
            tier, evidence, hits = 2, "matter number", [(mn,)]
        elif cl:
            tier, evidence, hits = 2, "claim number", [(cl,)]
        elif name and dob:
            tier, evidence, hits = 2, "name and date of birth", [(name, dob)]
        elif name and o_name != t_name:
            tier, evidence, hits = 1, "name", [(name,)]
        if not tier:
            continue
        used = hits[0]
        pages = [f.page for f in used if f.page is not None]
        findings.append(CrossMatterFinding(tier, other.matter_id, min(pages) if pages else None,
                                           tuple(f.fact_id for f in used), evidence))
    return findings


# ---------------------------------------------------------------------------
# Fact conflict
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FactConflictFinding:
    field: str
    page: Optional[int]
    fact_ids: tuple[str, ...]

    def to_flag(self) -> Flag:
        where = f"page {self.page}" if self.page else "a staff-entered fact"
        return Flag(FlagType.FACT_CONFLICT, True,
                    f"date of birth on {where} differs from the matter record")


def fact_conflict_findings(facts: list[Fact], target: Matter,
                           index: MatterIndex) -> list[FactConflictFinding]:
    """Target client's name with a different DOB on the same page."""
    t_name = norm_name(target.client_name)
    t_dob = norm_date(target.date_of_birth)
    names_in_doc = {norm_name(f.value) for f in facts if f.field == CLIENT_NAME}
    # DOBs explained by another client whose name is also on the document.
    other_dobs = {norm_date(m.date_of_birth) for m in index.all()
                  if m.matter_id != target.matter_id
                  and not (norm_name(m.client_name) == t_name
                           and norm_date(m.date_of_birth) == t_dob)
                  and norm_name(m.client_name) in names_in_doc}

    findings: list[FactConflictFinding] = []
    name_pages = sorted({f.page for f in facts
                         if f.field == CLIENT_NAME and norm_name(f.value) == t_name},
                        key=lambda p: p or 0)
    for page in name_pages:
        dobs = [f for f in facts if f.field == DATE_OF_BIRTH and f.page == page]
        parsed = [(f, norm_date(f.value)) for f in dobs if norm_date(f.value)]
        if not parsed or any(d == t_dob for _, d in parsed):
            continue
        conflicting = [f for f, d in parsed if d not in other_dobs]
        if conflicting:
            findings.append(FactConflictFinding(DATE_OF_BIRTH, page,
                                                tuple(f.fact_id for f in conflicting)))
    return findings


# ---------------------------------------------------------------------------
# Duplicate checks
# ---------------------------------------------------------------------------
@dataclass
class PriorDocument:
    """What the duplicate check needs to know about an earlier document."""
    record_id: str
    sha256: str
    document_type: Optional[str]
    facts: list[Fact]
    text: Optional[PreparedText]
    received_at: datetime

    @classmethod
    def from_record(cls, record: CandidateRecord, text: Optional[PreparedText]) -> "PriorDocument":
        return cls(record.record_id, record.document.sha256, record.document_type,
                   list(record.facts), text, record.document.received_at)


@dataclass(frozen=True)
class DuplicateFinding:
    kind: FlagType
    prior_record_id: str
    similarity: float

    def to_flag(self) -> Flag:
        words = {FlagType.EXACT_DUPLICATE: "same file as",
                 FlagType.POSSIBLE_NEW_VERSION: "possible newer version of",
                 FlagType.POSSIBLE_DUPLICATE: "possible re-sent copy of"}[self.kind]
        blocking = self.kind == FlagType.EXACT_DUPLICATE   # warnings otherwise
        return Flag(self.kind, blocking, f"{words} {self.prior_record_id} "
                                         f"(text similarity {self.similarity:.2f})")


_NORMALIZERS = {MATTER_NUMBER: norm_id, CLAIM_NUMBER: norm_id, CLIENT_NAME: norm_name,
                DATE_OF_BIRTH: norm_date, INCIDENT_DATE: norm_date, DOCUMENT_DATE: norm_date,
                INSURER: norm_org, PROVIDER: norm_org}


def _identity(facts: list[Fact], *, with_date: bool) -> frozenset:
    out = set()
    for f in facts:
        norm = _NORMALIZERS.get(f.field)
        if norm is None or (f.field == DOCUMENT_DATE and not with_date):
            continue
        out.add((f.field, norm(f.value)))
    return frozenset(out)


def _tokens(text: Optional[PreparedText]) -> list[str]:
    if text is None:
        return []
    return re.findall(r"[a-z0-9]+", "\n".join(p.text for p in text.pages).casefold())


def _revised(text: Optional[PreparedText]) -> bool:
    return text is not None and any(_REVISION.search(p.text) for p in text.pages)


def text_similarity(a: Optional[PreparedText], b: Optional[PreparedText]) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return round(difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio(), 2)


def duplicate_findings(record: CandidateRecord, text: Optional[PreparedText],
                       history: list[PriorDocument]) -> list[DuplicateFinding]:
    """Return at most one finding: the strongest match against an earlier document."""
    earlier = [p for p in history if p.record_id != record.record_id]
    for prior in earlier:
        if prior.sha256 == record.document.sha256:
            return [DuplicateFinding(FlagType.EXACT_DUPLICATE, prior.record_id, 1.0)]

    ids = _identity(record.facts, with_date=False)
    ids_dated = _identity(record.facts, with_date=True)
    best: Optional[DuplicateFinding] = None
    for prior in earlier:
        if not ids or prior.document_type != record.document_type:
            continue
        if _identity(prior.facts, with_date=False) != ids:
            continue
        sim = text_similarity(text, prior.text)
        if sim < SIMILARITY_THRESHOLD:
            continue
        if _revised(text) and not _revised(prior.text):
            found = DuplicateFinding(FlagType.POSSIBLE_NEW_VERSION, prior.record_id, sim)
        elif _identity(prior.facts, with_date=True) == ids_dated:
            found = DuplicateFinding(FlagType.POSSIBLE_DUPLICATE, prior.record_id, sim)
        else:
            continue
        if best is None or found.similarity > best.similarity:
            best = found
    return [best] if best else []


# ---------------------------------------------------------------------------
# All checks for one record
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    flags: list[Flag]
    cross_matter: list[CrossMatterFinding] = field(default_factory=list)
    duplicates: list[DuplicateFinding] = field(default_factory=list)
    conflicts: list[FactConflictFinding] = field(default_factory=list)


def run_checks(record: CandidateRecord, text: Optional[PreparedText], index: MatterIndex,
               history: list[PriorDocument], audit: AuditLog,
               system_id: str = "system") -> CheckResult:
    """Matcher + guard + duplicate checks, then ``write_flags`` (I5)."""
    flags = run_matching(record, index)
    target = index.get(record.target_matter_id())
    cross = cross_matter_findings(record.facts, target, index) if target else []
    conflicts = fact_conflict_findings(record.facts, target, index) if target else []
    dups = duplicate_findings(record, text, history)
    flags += ([c.to_flag() for c in cross] + [c.to_flag() for c in conflicts]
              + [d.to_flag() for d in dups])
    write_flags(record, flags, audit, system_id)
    return CheckResult(record.flags, cross, dups, conflicts)


__all__ = ["CheckResult", "CrossMatterFinding", "DuplicateFinding", "FactConflictFinding",
           "PriorDocument", "cross_matter_findings", "duplicate_findings",
           "fact_conflict_findings", "run_checks", "text_similarity"]
