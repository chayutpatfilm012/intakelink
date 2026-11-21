"""Held-out evaluation set: 40 fictional documents in formats the extractor
was NOT written for.

Purpose: the development set (``intakelink.synthetic``) and the rule-based
extractor share one format, so 100% there proves only that the pipeline
works. This set uses six other fictional templates - unknown labels, values
on the line under the label, prose, several fields on one line, 2-digit years,
dotted dates, ordinals, and OCR character swaps - to measure how the system
behaves on text it has not seen.

Rules for keeping it held out:
  * It was written after the extractor, and the extractor must not be
    changed to fit it. If the extractor is ever tuned on this set, the set
    stops being held out and a new one is needed.
  * Ground truth is what is printed: each fact's value is the text exactly as
    it appears on its page (including OCR errors), and its page number.
  * There are no expected system outcomes, only the truth (correct matter,
    cross-matter identifiers, conflicting DOB). ``intakelink.evaluate``
    measures the system against it.

Composition: 30 standard (5 per template), 6 cross-matter (3 tier 1, 3 tier 2),
4 with a conflicting DOB. Same 20 fictional matters as the development set.

Regenerate with ``python -m intakelink.heldout data/heldout``.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from .fields import (CLAIM_NUMBER, CLIENT_NAME, DATE_OF_BIRTH, INCIDENT_DATE,
                     INSURER, PROVIDER)
from .models import Matter
from .synthetic import MATTERS, _Builder

_BY_ID = {m.matter_id: m for m in MATTERS}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _d(iso: str) -> date:
    return date.fromisoformat(iso)


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _first_last(m: Matter) -> str:
    return m.client_name


def _last_first(m: Matter) -> str:
    parts = m.client_name.split()
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


_OCR = str.maketrans({"i": "1", "l": "1", "O": "0", "o": "0"})


def _ocr(text: str, key: str) -> str:
    """Deterministic OCR-style damage: swap about every third eligible char."""
    out, n = [], int(hashlib.sha256(key.encode()).hexdigest()[:4], 16)
    for ch in text:
        swapped = ch.translate(_OCR)
        if swapped != ch:
            n += 1
            out.append(swapped if n % 3 == 0 else ch)
        else:
            out.append(ch)
    return "".join(out)


# Each renderer writes page 1 (header + identifiers) and page 2 (body),
# returning nothing; facts are recorded through the builder.
# ``dob`` lets a spec print a wrong date of birth (fact-conflict documents).
Renderer = Callable[[_Builder, Matter, str, str], None]


def _eob(b: _Builder, m: Matter, dob: str, key: str) -> None:
    b.line(1, f"{m.insurers[0]} - EXPLANATION OF BENEFITS")
    b.facts.append({"field": INSURER, "value": m.insurers[0], "page": 1})
    b.line(1, "This is not a bill. (fictional insurer; synthetic test document)")
    b.line(1)
    b.fact(1, "Member", CLIENT_NAME, _last_first(m).upper())
    b.fact(1, "Birthdate", DATE_OF_BIRTH, _d(dob).strftime("%Y.%m.%d"))
    b.fact(1, "Claim Ref", CLAIM_NUMBER, m.claim_numbers[0])
    b.line(1, f"Loss Date - {_d(m.incident_date).strftime('%m/%d/%Y')}")
    b.facts.append({"field": INCIDENT_DATE, "value": _d(m.incident_date).strftime("%m/%d/%Y"), "page": 1})
    b.line(2, "Service      Billed     Allowed    You owe\n97110        $160.00    $118.40    $0.00")


def _discharge(b: _Builder, m: Matter, dob: str, key: str) -> None:
    b.line(1, m.providers[0])
    b.facts.append({"field": PROVIDER, "value": m.providers[0], "page": 1})
    b.line(1, "EMERGENCY DEPARTMENT DISCHARGE SUMMARY (synthetic)")
    b.line(1)
    for label, fld, value in (
            ("PATIENT NAME", CLIENT_NAME, _first_last(m)),
            ("DATE OF BIRTH", DATE_OF_BIRTH, _d(dob).strftime("%m/%d/%y")),
            ("DATE OF VISIT / INJURY", INCIDENT_DATE,
             f"{_MONTHS[_d(m.incident_date).month - 1]} {_d(m.incident_date).day}, {_d(m.incident_date).year}")):
        b.line(1, label)
        b.line(1, value)
        b.facts.append({"field": fld, "value": value, "page": 1})
    b.line(2, "Discharge instructions: rest, ice, ibuprofen as directed. Return if symptoms worsen.")


def _pharmacy(b: _Builder, m: Matter, dob: str, key: str) -> None:
    b.line(1, "Cornerstone Pharmacy #0412 (fictional)")
    b.line(1, "PRESCRIPTION RECEIPT")
    name, birth = _first_last(m), f"{_d(dob).month}/{_d(dob).day}/{_d(dob).year}"
    b.line(1, f"Rx for: {name}   DOB {birth}")
    b.facts += [{"field": CLIENT_NAME, "value": name, "page": 1},
                {"field": DATE_OF_BIRTH, "value": birth, "page": 1}]
    b.line(1, f"Ref. claim {m.claim_numbers[0]} - injury-related prescription")
    b.facts.append({"field": CLAIM_NUMBER, "value": m.claim_numbers[0], "page": 1})
    b.line(2, "Cyclobenzaprine 10 mg  #30   $24.10\nTotal paid: $24.10")


def _prose(b: _Builder, m: Matter, dob: str, key: str) -> None:
    b.line(1, "Ashgrove & Pell LLP (fictional opposing counsel)")
    b.line(1)
    b.line(1, "Dear Claims Representative,")
    inc = _d(m.incident_date)
    when = f"{_MONTHS[inc.month - 1]} {_ordinal(inc.day)}, {inc.year}"
    claim = m.claim_numbers[0].replace("-", " ")
    name = _first_last(m)
    b.line(1, f"We write regarding {name}, who was hurt in the collision of {when}.")
    b.line(1, f"Kindly quote claim {claim} on all correspondence.")
    b.facts += [{"field": CLIENT_NAME, "value": name, "page": 1},
                {"field": INCIDENT_DATE, "value": when, "page": 1},
                {"field": CLAIM_NUMBER, "value": claim, "page": 1}]
    b.line(2, "We request the complete claim file within thirty days.\nVery truly yours,")


def _pt_note(b: _Builder, m: Matter, dob: str, key: str) -> None:
    b.line(1, m.providers[0])
    b.facts.append({"field": PROVIDER, "value": m.providers[0], "page": 1})
    b.line(1, "Physical Therapy Progress Report (synthetic)")
    b.line(1)
    b.fact(1, "Patient", CLIENT_NAME, _last_first(m))
    b.fact(1, "D.O.B.", DATE_OF_BIRTH, _d(dob).strftime("%m/%d/%Y"))
    b.fact(1, "Claim #", CLAIM_NUMBER, m.claim_numbers[0])
    b.fact(1, "DOI", INCIDENT_DATE, _d(m.incident_date).strftime("%m/%d/%Y"))
    b.line(2, "Visits to date: 8 of 12. Cervical rotation improved 20 degrees.")


def _ocr_bill(b: _Builder, m: Matter, dob: str, key: str) -> None:
    head = _ocr(m.providers[0], key + "p")
    b.line(1, head)
    b.facts.append({"field": PROVIDER, "value": head, "page": 1})
    b.line(1, _ocr("STATEMENT OF CHARGES (scanned copy)", key))
    b.line(1)
    for label, fld, value in (
            ("Patient", CLIENT_NAME, _last_first(m)),
            ("DOB", DATE_OF_BIRTH, _d(dob).strftime("%m/%d/%Y")),
            ("Claim No.", CLAIM_NUMBER, m.claim_numbers[0])):
        printed = _ocr(value, key + fld)
        b.line(1, f"{_ocr(label, key + label)}: {printed}")
        b.facts.append({"field": fld, "value": printed, "page": 1})
    b.line(2, _ocr("99213 Office visit $145.00\nTotal due: $145.00", key))


TEMPLATES: dict[str, tuple[str, Renderer]] = {
    "eob": ("insurer_letter", _eob),
    "discharge": ("medical_record", _discharge),
    "pharmacy": ("medical_bill", _pharmacy),
    "prose_letter": ("correspondence", _prose),
    "pt_note": ("medical_record", _pt_note),
    "ocr_bill": ("medical_bill", _ocr_bill),
}
_ORDER = list(TEMPLATES)


def _foreign(b: _Builder, template: str, other: Matter, tier: int) -> None:
    """Another client's identifiers on page 2, in the template's own style."""
    if tier == 1:
        line = {"eob": f"Other covered person: {other.client_name}",
                "discharge": f"Visitor log: {other.client_name}",
                "pharmacy": f"Picked up by: {other.client_name}",
                "prose_letter": f"cc: {other.client_name}",
                "pt_note": f"Seen in same session: {other.client_name}",
                "ocr_bill": f"Guarantor: {other.client_name}"}[template]
        b.line(2, line)
        b.facts.append({"field": CLIENT_NAME, "value": other.client_name, "page": 2})
    else:
        claim = other.claim_numbers[0]
        b.line(2, f"Payment also applied to claim {claim} (see attached).")
        b.facts.append({"field": CLAIM_NUMBER, "value": claim, "page": 2})


@dataclass
class HSpec:
    key: str
    category: str              # standard | cross_matter_tier1 | cross_matter_tier2 | fact_conflict
    template: str
    matter_id: str
    foreign: Optional[tuple[str, int]] = None   # (matter_id, tier)
    wrong_dob: Optional[str] = None             # ISO date printed instead of the real DOB


def _shift_dob(iso: str) -> str:
    """A plausible typo: day off by 10 (12th -> 22nd, 21st -> 11th)."""
    d = _d(iso)
    day = d.day + 10 if d.day <= 18 else d.day - 10
    return d.replace(day=day).isoformat()


def specs() -> list[HSpec]:
    out: list[HSpec] = []
    for i in range(30):
        out.append(HSpec(f"H{i + 1:02d}", "standard", _ORDER[i % 6],
                         MATTERS[(i * 7 + 3) % 20].matter_id))
    pairs = [("M-1042", "M-1258"), ("M-1219", "M-1436"), ("M-1335", "M-2077"),   # tier 1
             ("M-1105", "M-1351"), ("M-1290", "M-1187"), ("M-1421", "M-1203")]   # tier 2
    for j, (mid, other) in enumerate(pairs):
        tier = 1 if j < 3 else 2
        out.append(HSpec(f"HX{j + 1}", f"cross_matter_tier{tier}", _ORDER[(j + 1) % 6], mid,
                         foreign=(other, tier)))
    for j, mid in enumerate(["M-1276", "M-1312", "M-1130", "M-1421"]):
        template = ["eob", "pt_note", "discharge", "ocr_bill"][j]
        out.append(HSpec(f"HF{j + 1}", "fact_conflict", template, mid,
                         wrong_dob=_shift_dob(_BY_ID[mid].date_of_birth)))
    return out


def build() -> tuple[list[dict], dict[str, bytes]]:
    manifest, files = [], {}
    for n, s in enumerate(specs(), start=1):
        m = _BY_ID[s.matter_id]
        b = _Builder(2)
        doc_type, render = TEMPLATES[s.template]
        render(b, m, s.wrong_dob or m.date_of_birth, s.key)
        if s.foreign:
            _foreign(b, s.template, _BY_ID[s.foreign[0]], s.foreign[1])
        done = b.done()
        name = f"held-{n:02d}.txt"
        files[name] = ("\n\f".join(done.pages) + "\n").encode("utf-8")
        manifest.append({
            "file": name, "key": s.key, "category": s.category, "template": s.template,
            "document_type": doc_type, "truth_matter": s.matter_id,
            "cross_matter": (None if not s.foreign else
                             {"matter": s.foreign[0], "tier": s.foreign[1], "page": 2}),
            "fact_conflict": {"field": DATE_OF_BIRTH, "page": 1} if s.wrong_dob else None,
            "facts": done.facts,
        })
    return manifest, files


def write(root: Path) -> None:
    root = Path(root)
    (root / "documents").mkdir(parents=True, exist_ok=True)
    manifest, files = build()
    for name, data in files.items():
        (root / "documents" / name).write_bytes(data)
    (root / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/heldout")
    write(out)
    print(f"wrote {len(specs())} held-out documents to {out}")
