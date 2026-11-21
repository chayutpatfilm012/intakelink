"""Shared fixtures: a tiny synthetic office with two matters and four people.

All names and numbers are fictional (NFR-12: synthetic data only).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from intakelink.access import MatterIndex
from intakelink.audit import AuditLog
from intakelink.models import (Actor, CandidateRecord, Document, Fact, Flag,
                               FlagType, Matter, Proposal, Role)
from intakelink.review import write_flags

PARALEGAL = Actor("p.lee", Role.PARALEGAL)
SENIOR = Actor("s.ortiz", Role.SENIOR_PARALEGAL)
ATTORNEY = Actor("a.reyes", Role.ATTORNEY)            # responsible for M-1042
OTHER_ATTORNEY = Actor("a.khan", Role.ATTORNEY)       # responsible for M-2077
OUTSIDER = Actor("p.zhou", Role.PARALEGAL)            # on no matter


@pytest.fixture
def index() -> MatterIndex:
    return MatterIndex([
        Matter("M-1042", "Dana Whitfield", "1984-03-12", ["CLM-55821"], "2026-06-14",
               responsible_attorney="a.reyes", assigned_staff=["p.lee", "s.ortiz"]),
        Matter("M-2077", "Marcus Bell", "1979-11-02", ["CLM-60310"], "2026-05-02",
               responsible_attorney="a.khan", assigned_staff=["p.lee"]),
    ])


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


def make_record(*, flags: list[Flag] | None = None, audit: AuditLog | None = None,
                record_id: str = "R-0001") -> CandidateRecord:
    """A medical bill proposed for M-1042, already through matching."""
    doc = Document("D-0001", "email", datetime(2026, 10, 9, 14, 2, tzinfo=timezone.utc),
                   "pdf", 3, "0" * 64)
    record = CandidateRecord(
        record_id=record_id,
        document=doc,
        document_type="medical_bill",
        facts=[Fact("F-1", "claimNumber", "CLM-55821", page=1, confidence=0.97),
               Fact("F-2", "incidentDate", "2026-06-14", page=1, confidence=0.91)],
        proposals=[Proposal("M-1042", 0.8)],
    )
    write_flags(record, flags or [], audit or AuditLog())
    return record


@pytest.fixture
def record(audit: AuditLog) -> CandidateRecord:
    return make_record(audit=audit)


def tier2_flag() -> Flag:
    return Flag(FlagType.CROSS_MATTER_RISK, True, "claim number on page 2 belongs to another matter")


def tier1_flag() -> Flag:
    return Flag(FlagType.CROSS_MATTER_NAME, True, "a name on page 2 matches another matter")
