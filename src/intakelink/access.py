"""Matter Access Control (X1), NFR-01.

An actor may read or act on a record only when they are assigned to the
record's matter or are its responsible attorney.
"""
from __future__ import annotations

from typing import Mapping, Optional

from .models import Actor, Matter, Role


class MatterIndex:
    """Read-only lookup of matters (C5). Built once from synthetic data."""

    def __init__(self, matters: list[Matter]) -> None:
        self._by_id: dict[str, Matter] = {m.matter_id: m for m in matters}

    def get(self, matter_id: Optional[str]) -> Optional[Matter]:
        return self._by_id.get(matter_id) if matter_id else None

    def all(self) -> list[Matter]:
        return list(self._by_id.values())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Matter]) -> "MatterIndex":
        return cls(list(data.values()))


def is_on_matter(actor: Actor, matter: Optional[Matter]) -> bool:
    if matter is None:
        return False
    return actor.user_id in matter.assigned_staff or actor.user_id == matter.responsible_attorney


def is_responsible_attorney(actor: Actor, matter: Optional[Matter]) -> bool:
    return (matter is not None
            and actor.role == Role.ATTORNEY
            and actor.user_id == matter.responsible_attorney)


def can_handle_intake(actor: Actor) -> bool:
    """Intake queue: records with no matter yet (low confidence, unreadable).

    Such a record may hold any client's data, so only senior paralegals (the
    intake owners) may open or act on it. They may assign it to any matter with
    a reason; from then on only that matter's team has access.
    """
    return actor.role == Role.SENIOR_PARALEGAL


def can_handle_escalation(actor: Actor, matter: Optional[Matter]) -> bool:
    """Escalation owner: the matter's attorney or a senior paralegal on it (§7.5)."""
    if not is_on_matter(actor, matter):
        return False
    return actor.role in (Role.ATTORNEY, Role.SENIOR_PARALEGAL)
