"""Audit Log (X2): an append-only trail of every action and refusal.

FR-08 and NFR-09: entries can be added and read, never edited or deleted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .models import utc_now


@dataclass(frozen=True)
class AuditEntry:
    at: datetime
    actor_id: str
    event: str            # e.g. "validate", "release_hold", "hold_set"
    record_id: Optional[str]
    outcome: str          # "ok" or "denied"
    detail: str = ""

    def to_json(self) -> str:
        data = asdict(self)
        data["at"] = self.at.isoformat()
        return json.dumps(data, ensure_ascii=False)


class AuditLog:
    """In-memory append-only log, optionally mirrored to a JSON Lines file."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._entries: list[AuditEntry] = []
        self._path = Path(path) if path else None

    def append(self, actor_id: str, event: str, record_id: Optional[str],
               outcome: str = "ok", detail: str = "") -> AuditEntry:
        entry = AuditEntry(utc_now(), actor_id, event, record_id, outcome, detail)
        self._entries.append(entry)
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(entry.to_json() + "\n")
        return entry

    def entries(self) -> tuple[AuditEntry, ...]:
        """Read-only view; callers cannot change or remove entries."""
        return tuple(self._entries)

    def for_record(self, record_id: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.record_id == record_id]

    def denials(self) -> list[AuditEntry]:
        return [e for e in self._entries if e.outcome == "denied"]

    def __iter__(self) -> Iterator[AuditEntry]:
        return iter(self.entries())

    def __len__(self) -> int:
        return len(self._entries)
