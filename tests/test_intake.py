"""Slice 2: intake (C1), write-once document store (C2), text preparation (C3)."""
from __future__ import annotations

import os
import stat

import pytest

from intakelink.audit import AuditLog
from intakelink.intake import (DocumentStore, Intake, IntakeError, StoreError,
                               TextPrepError, prepare_text, sha256_hex)
from intakelink.models import ProcessingStatus, ReviewStatus

THREE_PAGES = ("Page one: Claim No. CLM-55821\r\n\fPage two\n\fPage three\n\f").encode()


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    return DocumentStore(tmp_path / "originals")


@pytest.fixture
def intake(store, audit) -> Intake:
    return Intake(store, audit)


# --- C2 write-once store ---------------------------------------------------
def test_store_refuses_a_second_write_for_the_same_id(store):
    store.put("D-0001", "txt", b"original")
    with pytest.raises(StoreError, match="write-once"):
        store.put("D-0001", "txt", b"replacement")
    with pytest.raises(StoreError, match="write-once"):
        store.put("D-0001", "pdf", b"replacement under another extension")
    assert store.get("D-0001", sha256_hex(b"original")) == b"original"


def test_stored_original_is_read_only(store):
    path = store.put("D-0001", "txt", b"original")
    assert not os.stat(path).st_mode & stat.S_IWRITE


def test_store_detects_a_changed_file(store):
    path = store.put("D-0001", "txt", b"original")
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)  # simulate tampering outside the app
    path.write_bytes(b"tampered")
    with pytest.raises(StoreError, match="integrity"):
        store.get("D-0001", sha256_hex(b"original"))


def test_store_rejects_path_like_ids(store):
    with pytest.raises(StoreError):
        store.put("../escape", "txt", b"x")


# --- C3 text preparation ---------------------------------------------------
def test_pages_are_numbered_from_one_and_split_on_form_feed():
    text = prepare_text("D-0001", "txt", THREE_PAGES)
    assert text.page_count == 3                       # trailing \f adds no page
    assert [p.number for p in text.pages] == [1, 2, 3]
    assert text.page(1).text == "Page one: Claim No. CLM-55821"   # CRLF cleaned
    assert text.page(3).text == "Page three"
    with pytest.raises(IndexError):
        text.page(4)


def test_blank_middle_page_keeps_later_page_numbers():
    text = prepare_text("D-0001", "txt", b"first\f\fthird")
    assert text.page(2).text == ""
    assert text.page(3).text == "third"


@pytest.mark.parametrize("data, why", [
    (b"", "empty"),
    (b"\xff\xfe\x00\x00garbage\x93", "UTF-8"),
    (b"text\x00with nul", "binary"),
    (b"   \n\f  \n", "no readable text"),
])
def test_unreadable_text_files_fail(data, why):
    with pytest.raises(TextPrepError, match=why):
        prepare_text("D-0001", "txt", data)


def test_file_named_pdf_without_pdf_header_fails():
    with pytest.raises(TextPrepError, match="%PDF"):
        prepare_text("D-0001", "pdf", b"this is not really a pdf")


# --- C1 intake ---------------------------------------------------------------
def test_receive_creates_candidate_with_pages(intake, store, audit):
    result = intake.receive(THREE_PAGES, "bill.txt", "email")
    record = result.record
    assert result.error is None
    assert record.record_id == "R-0001" and record.document.doc_id == "D-0001"
    assert record.document.pages == 3
    assert record.document.sha256 == sha256_hex(THREE_PAGES)
    assert record.processing_status == ProcessingStatus.RECEIVED
    assert record.review_status == ReviewStatus.CANDIDATE
    assert store.get("D-0001", record.document.sha256) == THREE_PAGES
    assert [e.event for e in audit.for_record("R-0001")] == ["document_received", "text_prepared"]


def test_repeated_bytes_get_their_own_document(intake):
    first = intake.receive(THREE_PAGES, "bill.txt", "email").record
    second = intake.receive(THREE_PAGES, "bill (1).txt", "portal").record
    assert first.document.doc_id != second.document.doc_id
    assert first.document.sha256 == second.document.sha256


def test_corrupted_file_is_kept_and_marked_failed(intake, store, audit):
    result = intake.receive(b"\x89\x00\xff\xfe broken", "scan.txt", "scan")
    assert result.text is None and result.error
    assert result.record.processing_status == ProcessingStatus.EXTRACTION_FAILED
    assert result.record.review_status == ReviewStatus.CANDIDATE
    assert store.find("D-0001") is not None            # original still kept
    assert audit.for_record("R-0001")[-1].event == "text_prep_failed"


@pytest.mark.parametrize("filename, channel, code", [
    ("photo.heic", "email", "UNSUPPORTED_TYPE"),
    ("noextension", "email", "UNSUPPORTED_TYPE"),
    ("bill.txt", "fax-gateway", "UNKNOWN_CHANNEL"),
])
def test_refused_files_are_not_stored_and_are_logged(intake, store, audit, filename, channel, code):
    with pytest.raises(IntakeError) as err:
        intake.receive(b"some text", filename, channel)
    assert err.value.code == code
    assert store.ids() == []
    assert audit.denials()[-1].event == "intake_refused"


def test_ids_continue_after_restart(store):
    Intake(store, AuditLog()).receive(b"one", "a.txt", "email")
    again = Intake(store, AuditLog()).receive(b"two", "b.txt", "email")
    assert again.record.document.doc_id == "D-0002"


def test_pdf_text_with_optional_extra():
    pypdf = pytest.importorskip("pypdf")
    import io
    writer = pypdf.PdfWriter()
    writer.add_blank_page(612, 792)
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(TextPrepError, match="no readable text"):
        prepare_text("D-0001", "pdf", buf.getvalue())   # blank page = needs OCR
