"""Information structures for IntakeLink (Solution Design Report §7.3).

These are plain dataclasses with no third-party dependencies, so the core
rules can be tested without the UI or an AI provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProcessingStatus(str, Enum):
    RECEIVED = "RECEIVED"
    EXTRACTING = "EXTRACTING"
    MATCHING = "MATCHING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"


class ReviewStatus(str, Enum):
    CANDIDATE = "candidate"
    CORRECTED = "corrected"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class HoldStatus(str, Enum):
    NONE = "NONE"
    HELD_FOR_ATTORNEY = "HELD_FOR_ATTORNEY"


class Origin(str, Enum):
    AI = "ai"
    STAFF = "staff"


class FlagType(str, Enum):
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    FACT_CONFLICT = "FACT_CONFLICT"
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    CROSS_MATTER_RISK = "CROSS_MATTER_RISK"  # tier 2
    CROSS_MATTER_NAME = "CROSS_MATTER_NAME"  # tier 1
    POSSIBLE_NEW_VERSION = "POSSIBLE_NEW_VERSION"
    MATCHING_UNAVAILABLE = "MATCHING_UNAVAILABLE"


class Action(str, Enum):
    VALIDATE = "validate"
    CORRECT = "correct"
    REJECT = "reject"
    ESCALATE = "escalate"
    OVERRIDE_GUARD = "override_guard"  # tier 1 only
    RELEASE_HOLD = "release_hold"      # responsible attorney only


class Role(str, Enum):
    PARALEGAL = "paralegal"
    SENIOR_PARALEGAL = "senior_paralegal"
    ATTORNEY = "attorney"


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: Role


@dataclass
class Matter:
    """One entry in the read-only Matter Index (C5)."""
    matter_id: str
    client_name: str
    date_of_birth: str
    claim_numbers: list[str]
    incident_date: str
    responsible_attorney: str
    assigned_staff: list[str] = field(default_factory=list)
    insurers: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)


@dataclass
class Document:
    doc_id: str
    channel: str
    received_at: datetime
    file_type: str
    pages: int
    sha256: str
    path: Optional[str] = None


@dataclass
class Fact:
    fact_id: str
    field: str
    value: str
    page: Optional[int]          # None only for staff-entered facts
    confidence: float
    origin: Origin = Origin.AI
    entered_by: Optional[str] = None


@dataclass
class Evidence:
    fact_id: str
    matter_field: str
    rule: str
    weight: float


@dataclass
class Proposal:
    matter_id: str
    score: float
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Flag:
    type: FlagType
    blocking: bool
    detail: str
    cleared: bool = False


@dataclass
class Decision:
    actor_id: str
    action: Action
    prior: object
    new: object
    reason: Optional[str]
    at: datetime


@dataclass
class CandidateRecord:
    record_id: str
    document: Document
    processing_status: ProcessingStatus = ProcessingStatus.RECEIVED
    review_status: ReviewStatus = ReviewStatus.CANDIDATE
    hold_status: HoldStatus = HoldStatus.NONE
    document_type: Optional[str] = None
    facts: list[Fact] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    selected_matter_id: Optional[str] = None
    hold_released_by: Optional[str] = None
    decisions: list[Decision] = field(default_factory=list)
    version: int = 1

    # --- helpers used by the rules -------------------------------------
    def open_blocking_flags(self) -> list[Flag]:
        return [f for f in self.flags if f.blocking and not f.cleared]

    def has_open(self, flag_type: FlagType) -> bool:
        return any(f.type == flag_type and not f.cleared for f in self.flags)

    def target_matter_id(self) -> Optional[str]:
        """Matter the record is about: the selected one, else the top proposal."""
        if self.selected_matter_id:
            return self.selected_matter_id
        return self.proposals[0].matter_id if self.proposals else None

    @property
    def is_final(self) -> bool:
        return self.review_status in (ReviewStatus.APPROVED, ReviewStatus.REJECTED)
