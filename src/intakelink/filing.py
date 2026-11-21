"""Filing & Notification (C10).

Filing goes to a mock case-management system (there is no real one; see README
"Known limitations"). Only an approved record with no active hold can be filed,
and each record is filed at most once. Filing never changes review status.

Notifications go to an in-memory outbox. They name the record and the reason
for attention, never details of another client's matter.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .audit import AuditLog
from .models import Action, CandidateRecord, HoldStatus, ReviewStatus, utc_now


class FilingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FilingReceipt:
    cms_ref: str
    record_id: str
    doc_id: str
    matter_id: str
    document_type: Optional[str]
    approved_by: str
    filed_at: datetime


class MockCaseManagementSystem:
    """Stands in for the firm's case-management system (matter folders)."""

    def __init__(self) -> None:
        self._folders: dict[str, list[FilingReceipt]] = {}
        self._by_record: dict[str, FilingReceipt] = {}

    def file(self, record: CandidateRecord, audit: AuditLog,
             system_id: str = "system") -> FilingReceipt:
        try:
            if record.review_status != ReviewStatus.APPROVED:
                raise FilingError("NOT_APPROVED", f"record is {record.review_status.value}")
            if record.hold_status != HoldStatus.NONE:
                raise FilingError("HELD", "record is held for the responsible attorney")
            if record.record_id in self._by_record:
                raise FilingError("ALREADY_FILED", self._by_record[record.record_id].cms_ref)
            if not record.selected_matter_id:
                raise FilingError("NO_MATTER", "approved record has no matter")
        except FilingError as err:
            audit.append(system_id, "file", record.record_id, outcome="denied",
                         detail=f"{err.code}: {err.message}")
            raise
        approval = next(d for d in reversed(record.decisions) if d.action == Action.VALIDATE)
        folder = self._folders.setdefault(record.selected_matter_id, [])
        receipt = FilingReceipt(
            cms_ref=f"CMS-{record.selected_matter_id}-{len(folder) + 1:04d}",
            record_id=record.record_id, doc_id=record.document.doc_id,
            matter_id=record.selected_matter_id, document_type=record.document_type,
            approved_by=approval.actor_id, filed_at=utc_now())
        folder.append(receipt)
        self._by_record[record.record_id] = receipt
        audit.append(system_id, "filed", record.record_id,
                     detail=f"{receipt.cms_ref} approved by {receipt.approved_by}")
        return receipt

    def folder(self, matter_id: str) -> list[FilingReceipt]:
        return list(self._folders.get(matter_id, []))

    def receipt_for(self, record_id: str) -> Optional[FilingReceipt]:
        return self._by_record.get(record_id)

    def all(self) -> list[FilingReceipt]:
        return [r for folder in self._folders.values() for r in folder]


@dataclass(frozen=True)
class Notification:
    to: str
    subject: str
    body: str
    record_id: Optional[str]
    at: datetime


INTAKE_QUEUE = "intake-queue"   # recipient for records that have no matter yet


class Notifier:
    def __init__(self) -> None:
        self._outbox: list[Notification] = []

    def send(self, to: str, subject: str, body: str, record_id: Optional[str],
             audit: AuditLog, system_id: str = "system") -> Notification:
        note = Notification(to, subject, body, record_id, utc_now())
        self._outbox.append(note)
        audit.append(system_id, "notified", record_id, detail=f"{to}: {subject}")
        return note

    def for_user(self, user_id: str) -> list[Notification]:
        return [n for n in self._outbox if n.to == user_id]

    def all(self) -> list[Notification]:
        return list(self._outbox)


__all__ = ["FilingError", "FilingReceipt", "INTAKE_QUEUE", "MockCaseManagementSystem",
           "Notification", "Notifier"]
