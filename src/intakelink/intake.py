"""Intake (C1), write-once Document Store (C2) and Text Preparation (C3).

Flow: ``Intake.receive`` checks the file, stores the original bytes once,
creates a CandidateRecord in RECEIVED, then prepares page-numbered text.
If the text cannot be prepared the record moves to EXTRACTION_FAILED so a
person sees it; nothing is silently dropped.

System code here sets processing status only. It never sets review status.

Supported inputs:
  * ``.txt``: UTF-8 text; a form feed (``\\f``) separates pages.
  * ``.pdf``: needs the optional ``pdf`` extra (pypdf). Without it, or for a
    file that is not a readable PDF, text preparation fails visibly.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .audit import AuditLog
from .models import CandidateRecord, Document, ProcessingStatus, utc_now

SUPPORTED_TYPES = {"txt", "pdf"}
MAX_BYTES = 25 * 1024 * 1024
CHANNELS = {"email", "portal", "scan", "upload"}
PAGE_BREAK = "\f"


class IntakeError(Exception):
    """A file was refused at intake (never stored)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class StoreError(Exception):
    """A write-once or integrity rule of the Document Store was broken."""


class TextPrepError(Exception):
    """The stored file could not be turned into page-numbered text."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# C2: write-once document store
# ---------------------------------------------------------------------------
class DocumentStore:
    """Keeps each original file exactly once, under its document ID.

    * ``put`` creates the file exclusively; a second ``put`` for the same ID
      is refused, so an original can never be replaced.
    * Stored files are marked read-only.
    * ``get`` re-hashes the bytes and refuses a file whose hash changed.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, doc_id: str, file_type: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9-]+", doc_id):
            raise StoreError(f"invalid document id {doc_id!r}")
        return self.root / f"{doc_id}.{file_type}"

    def put(self, doc_id: str, file_type: str, data: bytes) -> Path:
        if self.find(doc_id) is not None:
            raise StoreError(f"{doc_id} is already stored; originals are write-once")
        path = self._path(doc_id, file_type)
        try:
            with path.open("xb") as fh:  # exclusive create: fails if it exists
                fh.write(data)
        except FileExistsError as err:
            raise StoreError(f"{doc_id} is already stored; originals are write-once") from err
        os.chmod(path, stat.S_IREAD)
        return path

    def find(self, doc_id: str) -> Optional[Path]:
        matches = sorted(self.root.glob(f"{doc_id}.*"))
        return matches[0] if matches else None

    def get(self, doc_id: str, expected_sha256: str) -> bytes:
        path = self.find(doc_id)
        if path is None:
            raise StoreError(f"{doc_id} is not in the store")
        data = path.read_bytes()
        if sha256_hex(data) != expected_sha256:
            raise StoreError(f"{doc_id} failed its integrity check")
        return data

    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.root.iterdir() if p.is_file())


# ---------------------------------------------------------------------------
# C3: page-numbered text preparation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Page:
    number: int   # 1-based, as a person would cite it
    text: str


@dataclass(frozen=True)
class PreparedText:
    doc_id: str
    pages: tuple[Page, ...]

    def page(self, number: int) -> Page:
        if not 1 <= number <= len(self.pages):
            raise IndexError(f"{self.doc_id} has no page {number}")
        return self.pages[number - 1]

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip("\n")


def _pages_from_txt(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as err:
        raise TextPrepError(f"not valid UTF-8 text (byte {err.start})") from err
    if "\x00" in text:
        raise TextPrepError("file contains binary data")
    pages = text.split(PAGE_BREAK)
    # A trailing page break does not start a new page.
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()
    return pages


def _pages_from_pdf(data: bytes) -> list[str]:
    if not data.startswith(b"%PDF-"):
        raise TextPrepError("not a PDF file (missing %PDF header)")
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError as err:
        raise TextPrepError("PDF text needs the optional 'pdf' extra (pip install -e .[pdf])") from err
    import io
    try:
        reader = PdfReader(io.BytesIO(data))
        return [p.extract_text() or "" for p in reader.pages]
    except Exception as err:  # pypdf raises many types for damaged files
        raise TextPrepError(f"PDF could not be read: {err}") from err


def prepare_text(doc_id: str, file_type: str, data: bytes) -> PreparedText:
    """Turn stored bytes into numbered pages, or raise TextPrepError."""
    if not data:
        raise TextPrepError("file is empty")
    if file_type == "txt":
        raw_pages = _pages_from_txt(data)
    elif file_type == "pdf":
        raw_pages = _pages_from_pdf(data)
    else:
        raise TextPrepError(f"no text preparation for .{file_type}")
    pages = tuple(Page(i, _clean(t)) for i, t in enumerate(raw_pages, start=1))
    if not any(re.search(r"[A-Za-z0-9]", p.text) for p in pages):
        raise TextPrepError("no readable text (a scan may need OCR)")
    return PreparedText(doc_id, pages)


# ---------------------------------------------------------------------------
# C1: intake
# ---------------------------------------------------------------------------
@dataclass
class IntakeResult:
    record: CandidateRecord
    text: Optional[PreparedText]
    error: Optional[str] = None


def file_type_of(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")


class Intake:
    """Receives files from a channel and creates one candidate record each.

    Every received file gets its own document ID, even if its bytes repeat an
    earlier file; duplicate checks happen later (slice 4) and are shown to a
    person, not applied silently.
    """

    def __init__(self, store: DocumentStore, audit: AuditLog,
                 system_id: str = "system") -> None:
        self.store = store
        self.audit = audit
        self.system_id = system_id
        self._seq = len(store.ids())

    def _next_ids(self) -> tuple[str, str]:
        self._seq += 1
        return f"D-{self._seq:04d}", f"R-{self._seq:04d}"

    def receive(self, data: bytes, filename: str, channel: str,
                received_at: Optional[datetime] = None) -> IntakeResult:
        file_type = file_type_of(filename)
        try:
            if channel not in CHANNELS:
                raise IntakeError("UNKNOWN_CHANNEL", f"channel {channel!r} is not accepted")
            if file_type not in SUPPORTED_TYPES:
                raise IntakeError("UNSUPPORTED_TYPE", f".{file_type or '?'} files are not accepted")
            if len(data) > MAX_BYTES:
                raise IntakeError("TOO_LARGE", f"file is over {MAX_BYTES} bytes")
        except IntakeError as err:
            self.audit.append(self.system_id, "intake_refused", None, outcome="denied",
                              detail=f"{err.code}: {filename}")
            raise

        doc_id, record_id = self._next_ids()
        digest = sha256_hex(data)
        path = self.store.put(doc_id, file_type, data)
        document = Document(doc_id, channel, received_at or utc_now(), file_type,
                            pages=0, sha256=digest, path=str(path))
        record = CandidateRecord(record_id=record_id, document=document)
        self.audit.append(self.system_id, "document_received", record_id,
                          detail=f"{doc_id} {filename} via {channel} sha256={digest[:12]}")

        try:
            text = prepare_text(doc_id, file_type, self.store.get(doc_id, digest))
        except TextPrepError as err:
            record.processing_status = ProcessingStatus.EXTRACTION_FAILED
            self.audit.append(self.system_id, "text_prep_failed", record_id, detail=str(err))
            return IntakeResult(record, None, str(err))

        document.pages = text.page_count
        self.audit.append(self.system_id, "text_prepared", record_id,
                          detail=f"{text.page_count} page(s)")
        return IntakeResult(record, text)


__all__ = ["DocumentStore", "Intake", "IntakeError", "IntakeResult", "Page",
           "PreparedText", "StoreError", "TextPrepError", "prepare_text", "sha256_hex"]
