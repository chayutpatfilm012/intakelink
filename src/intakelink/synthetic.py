"""Synthetic test data (NFR-12): 20 fictional matters and 64 fictional documents.

Every person, matter, claim, insurer, provider and agency here is invented.
Any resemblance to a real person or organization is coincidental.

Document set (Solution Design Report §8.3):
  30 standard                      one matter, clear evidence
  10 ambiguous / conflicting       5 expected AMBIGUOUS_MATCH, 3 LOW_CONFIDENCE,
                                   2 confident but with a conflicting DOB (FACT_CONFLICT)
  10 cross-matter                  5 tier 1 (another client's name),
                                   5 tier 2 (another client's claim no. or name + DOB)
  12 in 6 duplicate pairs          2 exact, 2 probable (re-sent / re-scanned),
                                   2 newer version (revised document)
   2 corrupted files               text preparation must fail visibly

Expected outcomes in the manifest are written by hand in ``SPECS``; they are
not computed by the matcher, so tests that compare the two are not circular.

Regenerate the committed files with::

    python -m intakelink.synthetic data/synthetic
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .access import MatterIndex
from .fields import (CLAIM_NUMBER, CLIENT_NAME, DATE_OF_BIRTH, DOCUMENT_DATE,
                     INCIDENT_DATE, INSURER, MATTER_NUMBER, PROVIDER)
from .models import Actor, Fact, Matter, Role

# ---------------------------------------------------------------------------
# Matter Index (C5): 20 fictional matters
# ---------------------------------------------------------------------------
HARBORLINE, KEYSTONE, CEDAR, PINECREST, SUMMIT = (
    "Harborline Mutual", "Keystone Casualty", "Cedar Ridge Insurance",
    "Pinecrest Assurance", "Summit Fidelity")
BAYVIEW, NORTHGATE, LAKESIDE, ST_ALDER, RIVERSIDE, MEADOW = (
    "Bayview Orthopedics", "Northgate Imaging", "Lakeside Physical Therapy",
    "St. Alder Medical Center", "Riverside Chiropractic", "Meadowbrook Rehabilitation")


def _m(mid, name, dob, claims, incident, attorney, staff, insurer, provider) -> Matter:
    return Matter(mid, name, dob, claims, incident, responsible_attorney=attorney,
                  assigned_staff=staff, insurers=[insurer], providers=[provider])


MATTERS: list[Matter] = [
    # M-1042 and M-2077 match the fixtures in tests/conftest.py.
    _m("M-1042", "Dana Whitfield", "1984-03-12", ["CLM-55821"], "2026-06-14", "a.reyes", ["p.lee", "s.ortiz"], HARBORLINE, BAYVIEW),
    _m("M-2077", "Marcus Bell", "1979-11-02", ["CLM-60310"], "2026-05-02", "a.khan", ["p.lee"], KEYSTONE, NORTHGATE),
    _m("M-1105", "Priya Raman", "1991-07-23", ["CLM-61477"], "2026-02-09", "a.reyes", ["s.ortiz", "p.garcia"], HARBORLINE, LAKESIDE),
    _m("M-1130", "Tobias Fenwick", "1968-01-30", ["CLM-58802", "CLM-58803"], "2026-03-21", "a.novak", ["p.garcia"], CEDAR, ST_ALDER),
    _m("M-1187", "Lucia Ferreira", "1995-12-05", ["CLM-62019"], "2026-04-17", "a.mensah", ["s.baker"], KEYSTONE, BAYVIEW),
    _m("M-1203", "Gideon Achterberg", "1957-09-14", ["CLM-59934"], "2025-11-28", "a.khan", ["p.lee", "s.baker"], PINECREST, NORTHGATE),
    _m("M-1219", "Hana Kobayashi", "1988-04-02", ["CLM-63155"], "2026-01-11", "a.novak", ["p.garcia"], HARBORLINE, RIVERSIDE),
    _m("M-1244", "Desmond Okafor", "1975-06-19", ["CLM-60772"], "2026-07-03", "a.mensah", ["s.ortiz"], CEDAR, ST_ALDER),
    _m("M-1258", "Rosalind Grey", "1949-02-27", ["CLM-57340"], "2025-10-06", "a.reyes", ["p.lee"], PINECREST, MEADOW),
    _m("M-1276", "Mateo Alvarado", "2001-08-08", ["CLM-64001"], "2026-08-22", "a.khan", ["s.baker"], KEYSTONE, LAKESIDE),
    _m("M-1290", "Ingrid Solberg", "1983-10-15", ["CLM-61820"], "2026-03-02", "a.novak", ["p.garcia", "s.ortiz"], SUMMIT, RIVERSIDE),
    _m("M-1312", "Yusuf Haddad", "1970-05-09", ["CLM-62588"], "2026-05-27", "a.mensah", ["s.baker"], SUMMIT, NORTHGATE),
    _m("M-1335", "Camille Duval", "1993-03-31", ["CLM-63904"], "2026-06-30", "a.reyes", ["s.ortiz"], HARBORLINE, MEADOW),
    _m("M-1351", "Arjun Mehta", "1986-11-21", ["CLM-59210"], "2025-12-19", "a.khan", ["p.lee"], CEDAR, BAYVIEW),
    # Two unrelated clients with the same name (different DOB).
    _m("M-1368", "Maria Santos", "1972-08-30", ["CLM-60045"], "2026-02-14", "a.novak", ["p.garcia"], PINECREST, ST_ALDER),
    _m("M-1372", "Maria Santos", "1998-01-07", ["CLM-64410"], "2026-09-05", "a.mensah", ["s.baker"], SUMMIT, LAKESIDE),
    # One client with two matters (two separate accidents, same insurer).
    _m("M-1389", "Elliot Marsh", "1981-12-12", ["CLM-58115"], "2025-09-19", "a.reyes", ["p.lee"], HARBORLINE, RIVERSIDE),
    _m("M-1407", "Elliot Marsh", "1981-12-12", ["CLM-63772"], "2026-07-25", "a.reyes", ["p.lee"], HARBORLINE, BAYVIEW),
    _m("M-1421", "Noor Siddiqui", "1999-02-03", ["CLM-62260"], "2026-04-08", "a.khan", ["s.ortiz"], KEYSTONE, MEADOW),
    _m("M-1436", "Wendell Pryce", "1965-07-07", ["CLM-61093"], "2026-01-29", "a.mensah", ["p.garcia"], CEDAR, NORTHGATE),
]
_BY_ID = {m.matter_id: m for m in MATTERS}

# Fictional staff. IDs match the fixtures in tests/conftest.py where they overlap.
USERS: dict[str, Actor] = {a.user_id: a for a in [
    Actor("p.lee", Role.PARALEGAL),
    Actor("p.garcia", Role.PARALEGAL),
    Actor("s.ortiz", Role.SENIOR_PARALEGAL),
    Actor("s.baker", Role.SENIOR_PARALEGAL),
    Actor("a.reyes", Role.ATTORNEY),
    Actor("a.khan", Role.ATTORNEY),
    Actor("a.novak", Role.ATTORNEY),
    Actor("a.mensah", Role.ATTORNEY),
]}


def matter_index() -> MatterIndex:
    return MatterIndex(MATTERS)


# ---------------------------------------------------------------------------
# Document specs
# ---------------------------------------------------------------------------
CODES = {"MN": MATTER_NUMBER, "CL": CLAIM_NUMBER, "NM": CLIENT_NAME, "DOB": DATE_OF_BIRTH,
         "INC": INCIDENT_DATE, "INS": INSURER, "PRV": PROVIDER}

Ident = tuple[str, str, Optional[str]]   # (matter_id, field, printed value override)


def ids(matter_id: str, codes: str, **override: str) -> list[Ident]:
    """ids("M-1042", "NM DOB CL") -> identifiers of M-1042 to print on page 1."""
    return [(matter_id, CODES[c], override.get(c)) for c in codes.split()]


@dataclass
class Foreign:
    matter_id: str
    codes: str
    tier: int
    page: int


@dataclass
class Spec:
    key: str
    category: str
    doc_type: str
    idents: list[Ident]
    outcome: str                         # CONFIDENT | AMBIGUOUS_MATCH | LOW_CONFIDENCE | EXTRACTION_FAILED
    truth: Optional[str] = None          # correct matter, when one is knowable
    candidates: tuple[str, ...] = ()     # matters a person must choose between
    foreign: Optional[Foreign] = None
    duplicate_of: Optional[str] = None   # key of the original
    duplicate_kind: Optional[str] = None # exact | probable | newer_version
    fact_conflict_page: Optional[int] = None  # page where the DOB contradicts the matter


def _std(key, mid, doc_type, codes, **ov) -> Spec:
    return Spec(key, "standard", doc_type, ids(mid, codes, **ov), "CONFIDENT", truth=mid)


def _x(key, mid, doc_type, codes, f_mid, f_codes, tier, page=2) -> Spec:
    return Spec(key, f"cross_matter_tier{tier}", doc_type, ids(mid, codes), "CONFIDENT",
                truth=mid, foreign=Foreign(f_mid, f_codes, tier, page))


def _dup(key, original: Spec, kind: str) -> Spec:
    return Spec(key, f"duplicate_{kind}", original.doc_type, list(original.idents),
                "CONFIDENT", truth=original.truth, duplicate_of=original.key,
                duplicate_kind=kind)


_D01 = Spec("D01", "duplicate_original", "medical_bill", ids("M-1203", "NM DOB CL PRV"), "CONFIDENT", truth="M-1203")
_D03 = Spec("D03", "duplicate_original", "insurer_letter", ids("M-1436", "CL NM INC INS"), "CONFIDENT", truth="M-1436")
_D05 = Spec("D05", "duplicate_original", "police_report", ids("M-1244", "NM DOB INC"), "CONFIDENT", truth="M-1244")
_D07 = Spec("D07", "duplicate_original", "medical_record", ids("M-1130", "NM DOB INC PRV"), "CONFIDENT", truth="M-1130")
_D09 = Spec("D09", "duplicate_original", "medical_bill", ids("M-1276", "CL NM DOB PRV"), "CONFIDENT", truth="M-1276")
_D11 = Spec("D11", "duplicate_original", "insurer_letter", ids("M-1421", "CL NM INC INS"), "CONFIDENT", truth="M-1421")

SPECS: list[Spec] = [
    # --- 30 standard -------------------------------------------------------
    _std("S01", "M-1042", "medical_bill", "NM DOB CL PRV"),
    _std("S02", "M-1042", "insurer_letter", "CL NM INC INS"),
    _std("S03", "M-2077", "police_report", "NM DOB INC"),
    _std("S04", "M-2077", "medical_record", "NM DOB CL PRV"),
    _std("S05", "M-1105", "medical_bill", "NM DOB INC PRV"),
    _std("S06", "M-1105", "correspondence", "MN NM"),
    _std("S07", "M-1130", "insurer_letter", "CL NM INS", CL="CLM-58803"),  # second claim
    _std("S08", "M-1130", "medical_record", "NM DOB INC PRV"),
    _std("S09", "M-1187", "wage_statement", "NM DOB INC"),
    _std("S10", "M-1187", "medical_bill", "CL NM PRV"),
    _std("S11", "M-1203", "police_report", "NM DOB INC"),
    _std("S12", "M-1219", "medical_bill", "NM DOB CL PRV"),
    _std("S13", "M-1219", "insurer_letter", "CL INC INS"),
    _std("S14", "M-1244", "medical_record", "NM DOB PRV"),       # exactly 0.6: threshold edge
    _std("S15", "M-1258", "correspondence", "MN NM"),
    _std("S16", "M-1258", "medical_bill", "NM DOB INC PRV"),
    _std("S17", "M-1276", "police_report", "NM DOB INC"),
    _std("S18", "M-1276", "insurer_letter", "CL NM INS"),
    _std("S19", "M-1290", "medical_record", "NM DOB INC PRV"),
    _std("S20", "M-1312", "medical_bill", "CL NM DOB PRV"),
    _std("S21", "M-1312", "wage_statement", "NM DOB INC"),
    _std("S22", "M-1335", "insurer_letter", "CL NM INC INS"),
    _std("S23", "M-1351", "medical_record", "NM DOB CL PRV"),
    _std("S24", "M-1368", "medical_bill", "NM DOB CL PRV"),      # shared name, DOB decides
    _std("S25", "M-1372", "police_report", "NM DOB INC"),        # shared name, DOB decides
    _std("S26", "M-1389", "insurer_letter", "CL NM INS"),        # same client, claim decides
    _std("S27", "M-1407", "medical_bill", "CL NM DOB PRV"),      # same client, claim decides
    _std("S28", "M-1421", "medical_record", "NM DOB INC PRV"),
    _std("S29", "M-1436", "correspondence", "MN NM"),
    _std("S30", "M-1436", "police_report", "NM DOB INC"),

    # --- 10 ambiguous / conflicting ---------------------------------------
    # Claim number of one client, name + DOB of another: 0.6 vs 0.5.
    Spec("A01", "ambiguous", "correspondence", ids("M-1203", "CL") + ids("M-1219", "NM DOB"),
         "AMBIGUOUS_MATCH", candidates=("M-1203", "M-1219")),
    # Name + DOB + incident of one, claim of another: 0.7 vs 0.6.
    Spec("A02", "ambiguous", "correspondence", ids("M-1290", "NM DOB INC") + ids("M-1312", "CL"),
         "AMBIGUOUS_MATCH", candidates=("M-1290", "M-1312")),
    # Same client, two matters, shared insurer: 0.6 vs 0.6.
    Spec("A03", "ambiguous", "insurer_letter", ids("M-1389", "NM DOB INS"),
         "AMBIGUOUS_MATCH", candidates=("M-1389", "M-1407")),
    # Conflicting fact: claim + name identify the matter, but the DOB has a typo
    # (12/15 for 12/05). Confident match (1.0) plus a blocking FACT_CONFLICT.
    Spec("F01", "ambiguous", "insurer_letter",
         ids("M-1187", "CL NM DOB INC INS", DOB="December 15, 1995"),
         "CONFIDENT", truth="M-1187", fact_conflict_page=1),
    # Cover letter quotes one matter number, enclosure identifies another: 1.0 vs 1.0.
    Spec("A05", "ambiguous", "correspondence", ids("M-1042", "MN") + ids("M-1335", "CL NM DOB"),
         "AMBIGUOUS_MATCH", candidates=("M-1042", "M-1335")),
    # Name + DOB + provider of one, claim of another: 0.6 vs 0.6.
    Spec("A06", "ambiguous", "medical_bill", ids("M-1276", "NM DOB PRV") + ids("M-1187", "CL"),
         "AMBIGUOUS_MATCH", candidates=("M-1187", "M-1276")),
    # Conflicting fact: transposed DOB digits (11/12 for 11/21). Claim + name +
    # provider give 0.9; FACT_CONFLICT blocks approval.
    Spec("F02", "ambiguous", "medical_record",
         ids("M-1351", "NM DOB CL PRV", DOB="11/12/1986"),
         "CONFIDENT", truth="M-1351", fact_conflict_page=1),
    # Name only: 0.2.
    Spec("L02", "ambiguous", "correspondence", ids("M-1219", "NM"), "LOW_CONFIDENCE",
         candidates=("M-1219",)),
    # Misspelled name earns nothing (no fuzzy matching); incident date only: 0.2.
    Spec("L03", "ambiguous", "police_report", ids("M-1244", "NM INC", NM="Desmon Okafor"),
         "LOW_CONFIDENCE", candidates=("M-1244",)),
    # Shared name + incident + insurer: 0.5, just under the threshold.
    Spec("L04", "ambiguous", "insurer_letter", ids("M-1372", "NM INC INS"),
         "LOW_CONFIDENCE", candidates=("M-1372", "M-1368")),

    # --- 10 cross-matter ----------------------------------------------------
    _x("X01", "M-1042", "medical_record", "NM DOB CL PRV", "M-2077", "NM", tier=1),
    _x("X02", "M-1335", "insurer_letter", "CL NM INC INS", "M-1290", "NM", tier=1),
    _x("X03", "M-1421", "medical_bill", "NM DOB CL PRV", "M-1312", "NM", tier=1),
    _x("X04", "M-1258", "police_report", "NM DOB INC", "M-1436", "NM", tier=1),
    _x("X05", "M-1219", "correspondence", "MN NM", "M-1187", "NM", tier=1),
    _x("X06", "M-1105", "medical_bill", "NM DOB CL PRV", "M-1203", "NM DOB", tier=2),
    _x("X07", "M-1187", "insurer_letter", "CL NM INC INS", "M-1351", "CL", tier=2),
    _x("X08", "M-1244", "medical_record", "NM DOB CL PRV", "M-1351", "NM DOB", tier=2, page=3),
    _x("X09", "M-1351", "medical_bill", "NM DOB CL PRV", "M-2077", "CL", tier=2),
    _x("X10", "M-1290", "correspondence", "MN NM", "M-1105", "NM DOB", tier=2),

    # --- 6 duplicate pairs ---------------------------------------------------
    _D01, _dup("D02", _D01, "exact"),
    _D03, _dup("D04", _D03, "exact"),
    _D05, _dup("D06", _D05, "probable"),
    _D07, _dup("D08", _D07, "probable"),
    _D09, _dup("D10", _D09, "newer_version"),
    _D11, _dup("D12", _D11, "newer_version"),

    # --- 2 corrupted ---------------------------------------------------------
    Spec("C01", "corrupted", "unknown", [], "EXTRACTION_FAILED"),
    Spec("C02", "corrupted", "unknown", [], "EXTRACTION_FAILED"),
]
_SPEC_BY_KEY = {s.key: s for s in SPECS}

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]

STYLES = {
    "medical_bill": dict(date="us", name="last_first", labels={
        MATTER_NUMBER: "Attorney File", CLAIM_NUMBER: "Claim No.", CLIENT_NAME: "Patient",
        DATE_OF_BIRTH: "DOB", INCIDENT_DATE: "Date of Injury", INSURER: "Payer"}),
    "medical_record": dict(date="us", name="last_first", labels={
        MATTER_NUMBER: "Attorney File", CLAIM_NUMBER: "Claim #", CLIENT_NAME: "Patient Name",
        DATE_OF_BIRTH: "Date of Birth", INCIDENT_DATE: "Date of Injury", INSURER: "Insurance"}),
    "police_report": dict(date="iso", name="first_last", labels={
        MATTER_NUMBER: "Requesting File", CLAIM_NUMBER: "Insurance Claim", CLIENT_NAME: "Party 1",
        DATE_OF_BIRTH: "DOB", INCIDENT_DATE: "Date of Collision", INSURER: "Insurance Carrier",
        PROVIDER: "Transported To"}),
    "insurer_letter": dict(date="long", name="first_last", labels={
        MATTER_NUMBER: "Your File", CLAIM_NUMBER: "Claim Number", CLIENT_NAME: "Claimant",
        DATE_OF_BIRTH: "Claimant DOB", INCIDENT_DATE: "Date of Loss", PROVIDER: "Provider"}),
    "wage_statement": dict(date="us", name="first_last", labels={
        MATTER_NUMBER: "Attorney File", CLAIM_NUMBER: "Claim No.", CLIENT_NAME: "Employee",
        DATE_OF_BIRTH: "Date of Birth", INCIDENT_DATE: "Date of Injury", INSURER: "Carrier"}),
    "correspondence": dict(date="long", name="first_last", labels={
        MATTER_NUMBER: "Your File No.", CLAIM_NUMBER: "Claim No.", CLIENT_NAME: "Re",
        DATE_OF_BIRTH: "Date of Birth", INCIDENT_DATE: "Date of Accident", INSURER: "Carrier",
        PROVIDER: "Provider"}),
}
# Letterhead field: printed as the first line, not as a labeled line.
LETTERHEAD = {"medical_bill": PROVIDER, "medical_record": PROVIDER, "insurer_letter": INSURER}
FIXED_LETTERHEAD = {
    "police_report": "BAYPORT POLICE DEPARTMENT  (fictional)\nTraffic Collision Report",
    "wage_statement": "Corvid Logistics LLC  (fictional employer)\nHuman Resources / Payroll",
    "correspondence": "Clearpath Records Retrieval  (fictional)\n88 Juniper Row, Bayport",
}
TITLES = {"medical_bill": "STATEMENT OF CHARGES", "medical_record": "OFFICE VISIT NOTE",
          "police_report": "Report No. {n}", "insurer_letter": "Claim correspondence",
          "wage_statement": "VERIFICATION OF LOST WAGES", "correspondence": "Records delivery"}


def _seed(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


def fmt_date(iso: str, style: str) -> str:
    d = date.fromisoformat(iso)
    if style == "us":
        return d.strftime("%m/%d/%Y")
    if style == "long":
        return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"
    return d.isoformat()


def fmt_name(name: str, style: str) -> str:
    if style != "last_first":
        return name
    parts = name.split()
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def _value(matter: Matter, fld: str, style: dict, override: Optional[str]) -> str:
    if override is not None:
        return override
    if fld == MATTER_NUMBER:
        return matter.matter_id
    if fld == CLAIM_NUMBER:
        return matter.claim_numbers[0]
    if fld == CLIENT_NAME:
        return fmt_name(matter.client_name, style["name"])
    if fld == DATE_OF_BIRTH:
        return fmt_date(matter.date_of_birth, style["date"])
    if fld == INCIDENT_DATE:
        return fmt_date(matter.incident_date, style["date"])
    if fld == INSURER:
        return matter.insurers[0]
    if fld == PROVIDER:
        return matter.providers[0]
    raise ValueError(fld)


@dataclass
class Rendered:
    pages: list[str]
    facts: list[dict] = field(default_factory=list)   # {field, value, page}


class _Builder:
    def __init__(self, n_pages: int) -> None:
        self.pages: list[list[str]] = [[] for _ in range(n_pages)]
        self.facts: list[dict] = []

    def line(self, page: int, text: str = "") -> None:
        self.pages[page - 1].append(text)

    def fact(self, page: int, label: Optional[str], fld: str, value: str) -> None:
        self.line(page, f"{label}: {value}" if label else value)
        self.facts.append({"field": fld, "value": value, "page": page})

    def done(self) -> Rendered:
        return Rendered(["\n".join(p) for p in self.pages], self.facts)


def _doc_date(spec: Spec, version: int) -> str:
    first = _BY_ID[spec.idents[0][0]] if spec.idents else MATTERS[0]
    base = date.fromisoformat(first.incident_date) + timedelta(days=20 + _seed(spec.key) % 40)
    return (base + timedelta(days=30 * version)).isoformat()


_BODY = {
    "medical_bill": [
        "Itemized charges",
        "  99204  New patient visit, moderate complexity      {a1}",
        "  72040  X-ray, cervical spine, 2-3 views            {a2}",
        "  97110  Therapeutic exercise, 15 min (x4)           {a3}",
    ],
    "medical_record": [
        "Subjective: Patient reports neck and lower back pain since the collision.",
        "Objective: Reduced cervical range of motion; paraspinal tenderness L3-L5.",
        "Assessment: Cervical strain; lumbar sprain.",
    ],
    "police_report": [
        "Narrative: Vehicle 1 was stopped for a red signal when Vehicle 2 struck it",
        "from behind. Party 1 reported neck pain at the scene. No citations issued",
        "to Party 1. Road surface dry; daylight.",
    ],
    "insurer_letter": [
        "We have received the records submitted to date and are reviewing the",
        "claim. Please send any further medical bills and a wage-loss statement.",
        "Our current evaluation amount is {a1}.",
    ],
    "wage_statement": [
        "Pay period summary (hourly employee, 40 h/week)",
        "  Scheduled hours missed:   {h}",
        "  Hourly rate:              {a1}",
        "  Gross wages lost:         {a2}",
    ],
    "correspondence": [
        "Enclosed are the records produced under the authorization on file.",
        "Enclosures: {n} page(s) of records, certification of custodian.",
    ],
}
_PAGES = {"medical_bill": 3, "medical_record": 3, "police_report": 2,
          "insurer_letter": 2, "wage_statement": 2, "correspondence": 2}


def _amounts(key: str, version: int) -> dict[str, str]:
    s = _seed(key)
    a1 = 180 + s % 220 + 35 * version
    a2 = 95 + (s >> 8) % 120
    a3 = 120 + (s >> 16) % 180
    return {"a1": f"${a1:,.2f}", "a2": f"${a2:,.2f}", "a3": f"${a3:,.2f}",
            "total": f"${a1 + a2 + a3:,.2f}", "h": str(16 + s % 48), "n": str(3 + s % 40)}


def _foreign_lines(b: _Builder, spec: Spec, style: dict) -> None:
    f = spec.foreign
    if f is None:
        return
    matter = _BY_ID[f.matter_id]
    intro = {
        "medical_bill": "--- page from another patient's account, scanned with this statement ---",
        "medical_record": "--- page from another chart, scanned with this record ---",
        "police_report": "Other party involved:",
        "insurer_letter": "Also referenced in this payment batch:",
        "correspondence": "Also included in this delivery:",
    }[spec.doc_type]
    b.line(f.page)
    b.line(f.page, intro)
    labels = dict(style["labels"])
    if spec.doc_type == "police_report":
        labels[CLIENT_NAME] = "Party 2"
    if spec.doc_type == "correspondence":
        labels[CLIENT_NAME] = "Records for"
    for code in f.codes.split():
        fld = CODES[code]
        b.fact(f.page, labels[fld], fld, _value(matter, fld, style, None))


def render(spec: Spec, version: int = 0) -> Rendered:
    """Render one document's pages and its ground-truth facts (with pages)."""
    style = STYLES[spec.doc_type]
    b = _Builder(_PAGES[spec.doc_type])
    amounts = _amounts(spec.key if spec.duplicate_of is None else spec.duplicate_of, version)
    idents = list(spec.idents)
    head_field = LETTERHEAD.get(spec.doc_type)

    # Page 1: letterhead, title, date, identifier block.
    if head_field:
        idx = next((i for i, (_, f, _) in enumerate(idents) if f == head_field), None)
        if idx is None:   # a provider's bill always shows the provider
            idents.insert(0, (idents[0][0], head_field, None))
            idx = 0
        mid, fld, ov = idents.pop(idx)
        b.fact(1, None, fld, _value(_BY_ID[mid], fld, style, ov))
        b.line(1, "(fictional organization; synthetic test document)")
    else:
        b.line(1, FIXED_LETTERHEAD[spec.doc_type])
    title = TITLES[spec.doc_type].format(n=f"BPD-{26000 + _seed(spec.key) % 9000}")
    if version:
        title = f"REVISED {title}  (supersedes prior version)"
    b.line(1)
    b.line(1, title)
    b.fact(1, "Date", DOCUMENT_DATE, fmt_date(_doc_date(spec, version), style["date"]))
    b.line(1)
    for mid, fld, ov in idents:
        b.fact(1, style["labels"][fld], fld, _value(_BY_ID[mid], fld, style, ov))

    # Page 2 (and 3): body text.
    b.line(2, "\n".join(_BODY[spec.doc_type]).format(**amounts))
    if spec.doc_type == "medical_bill":
        b.line(3, f"Total due: {amounts['total']}")
        b.line(3, "Remit to the billing office within 30 days of this statement.")
    elif spec.doc_type == "medical_record":
        b.line(3, "Plan: Physical therapy 2x/week for 6 weeks; follow up in 4 weeks.")
        b.line(3, "Electronically signed by the treating provider.")
    else:
        b.line(2)
        b.line(2, "Sincerely," if spec.doc_type in ("insurer_letter", "correspondence")
               else "Prepared by: records unit")
    _foreign_lines(b, spec, style)
    return b.done()


def _probable_copy(pages: list[str], key: str) -> list[str]:
    """A re-sent fax: header stamp on every page and re-flowed spacing."""
    n = len(pages)
    stamp = f"FAX RECEIVED 2026-10-{10 + _seed(key) % 18:02d} 09:{_seed(key) % 60:02d}"
    return [f"{stamp}   PAGE {i}/{n}\n\n" + p.replace(": ", ":  ")
            for i, p in enumerate(pages, start=1)]


def _corrupt_bytes(key: str) -> bytes:
    block = hashlib.sha256(key.encode()).digest()
    return b"\xff\xd8\x00\x13" + block * 64   # neither UTF-8 text nor a PDF


# ---------------------------------------------------------------------------
# Dataset assembly
# ---------------------------------------------------------------------------
def _ordered_specs() -> list[Spec]:
    """Shuffle so file order reveals nothing; a duplicate always follows its original."""
    specs = list(SPECS)
    random.Random(4010).shuffle(specs)
    pos = {s.key: i for i, s in enumerate(specs)}
    for s in list(specs):
        if s.duplicate_of and pos[s.key] < pos[s.duplicate_of]:
            i, j = pos[s.key], pos[s.duplicate_of]
            specs[i], specs[j] = specs[j], specs[i]
            pos[specs[i].key], pos[specs[j].key] = i, j
    return specs


def build_dataset() -> tuple[list[dict], dict[str, bytes]]:
    """Return (manifest entries, {filename: bytes}) for all 64 documents."""
    manifest: list[dict] = []
    files: dict[str, bytes] = {}
    filenames: dict[str, str] = {}
    start = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
    channels = ["email", "portal", "scan", "upload"]

    for i, spec in enumerate(_ordered_specs(), start=1):
        ext = "pdf" if spec.key == "C01" else "txt"
        name = f"doc-{i:02d}.{ext}"
        filenames[spec.key] = name
        facts: list[dict] = []
        if spec.category == "corrupted":
            data = _corrupt_bytes(spec.key)
        else:
            if spec.duplicate_kind == "exact":
                data = files[filenames[spec.duplicate_of]]
                facts = next(m["facts"] for m in manifest if m["key"] == spec.duplicate_of)
            else:
                original = _SPEC_BY_KEY[spec.duplicate_of] if spec.duplicate_of else spec
                version = 1 if spec.duplicate_kind == "newer_version" else 0
                rendered = render(original, version)
                pages = rendered.pages
                if spec.duplicate_kind == "probable":
                    # Stamp and spacing change; values and pages do not.
                    pages = _probable_copy(pages, spec.key)
                data = ("\n\f".join(pages) + "\n").encode("utf-8")
                facts = rendered.facts
        files[name] = data
        manifest.append({
            "file": name,
            "key": spec.key,
            "category": spec.category,
            "channel": channels[i % 4],
            "received_at": (start + timedelta(minutes=17 * i)).isoformat(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "document_type": None if spec.category == "corrupted" else spec.doc_type,
            "truth_matter": spec.truth,
            "candidate_matters": list(spec.candidates),
            "expected_outcome": spec.outcome,
            "cross_matter": (None if spec.foreign is None else
                             {"matter": spec.foreign.matter_id, "tier": spec.foreign.tier,
                              "page": spec.foreign.page}),
            "duplicate": (None if spec.duplicate_of is None else
                          {"kind": spec.duplicate_kind, "of": filenames[spec.duplicate_of]}),
            "fact_conflict": (None if spec.fact_conflict_page is None else
                              {"field": DATE_OF_BIRTH, "page": spec.fact_conflict_page}),
            "facts": facts,
        })
    return manifest, files


def gold_facts(entry: dict) -> list[Fact]:
    """Ground-truth facts of a manifest entry, as the extractor should return them."""
    return [Fact(f"F-{n}", f["field"], f["value"], page=f["page"], confidence=1.0)
            for n, f in enumerate(entry["facts"], start=1)]


def matters_json() -> list[dict]:
    return [asdict(m) for m in MATTERS]


def load_matter_index(path: Path) -> MatterIndex:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return MatterIndex([Matter(**m) for m in data])


def load_manifest(root: Path) -> list[dict]:
    return json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def write_dataset(root: Path) -> None:
    root = Path(root)
    docs = root / "documents"
    docs.mkdir(parents=True, exist_ok=True)
    manifest, files = build_dataset()
    for name, data in files.items():
        (docs / name).write_bytes(data)
    (root / "matters.json").write_bytes(_dump(matters_json()).encode("utf-8"))
    (root / "manifest.json").write_bytes(_dump(manifest).encode("utf-8"))


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/synthetic")
    write_dataset(out)
    print(f"wrote {len(SPECS)} documents and {len(MATTERS)} matters to {out}")
