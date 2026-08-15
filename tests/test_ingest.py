"""
Tests for extraction, and for reporting the RIGHT reason when it fails.

The bug these are written around: every file that produced no chunks was
described to the user as "scanned or image-only", because the only fact that
survived extraction was `num_chunks == 0`. On a real Downloads folder that
message was wrong more often than right — most unreadable PDFs are
password-protected bank statements, which OCR would never have fixed.

So the reason is now carried out of load_document() and recorded, and these
tests hold the distinctions apart.
"""
from __future__ import annotations

import pytest

from sift.ingest import (REASON_LOCKED, REASON_NO_TEXT, PdfLocked, extract_pdf,
                         load_document)


# --- extraction ------------------------------------------------------------

def test_ordinary_pdf_extracts_its_text(make_pdf):
    path = make_pdf("lease.pdf", text="Notice period is 60 days")
    assert "Notice period is 60 days" in extract_pdf(path)


def test_locked_pdf_raises_rather_than_returning_nothing(make_pdf):
    """Silently returning '' here is what caused the misdiagnosis."""
    path = make_pdf("statement.pdf", text="Balance 1000", password="hunter2")
    with pytest.raises(PdfLocked):
        extract_pdf(path)


def test_locked_pdf_opens_with_the_right_password(make_pdf):
    path = make_pdf("statement.pdf", text="Balance 1000", password="hunter2")
    assert "Balance 1000" in extract_pdf(path, "hunter2")


def test_wrong_password_still_raises(make_pdf):
    path = make_pdf("statement.pdf", text="Balance 1000", password="hunter2")
    with pytest.raises(PdfLocked):
        extract_pdf(path, "guess")


def test_permissions_only_pdf_needs_no_prompt(make_pdf):
    """Plenty of PDFs are encrypted only to forbid printing, and open with an
    empty password. Those should never reach the user as a prompt."""
    path = make_pdf("readonly.pdf", text="Public notice", password="")
    assert "Public notice" in extract_pdf(path)


# --- the reason that comes back --------------------------------------------

def test_a_readable_file_reports_no_reason(make_file):
    doc, reason = load_document(make_file("notes.md", "hello"))
    assert reason == ""
    assert doc["text"] == "hello"


def test_locked_pdf_is_reported_as_locked_not_as_scanned(make_pdf):
    """The regression test for the whole feature."""
    doc, reason = load_document(make_pdf("statement.pdf", text="x", password="pw"))
    assert doc is None
    assert reason == REASON_LOCKED
    assert reason != REASON_NO_TEXT


def test_pdf_without_a_text_layer_is_reported_as_such(make_pdf):
    """A genuinely scanned document: opens fine, yields nothing."""
    doc, reason = load_document(make_pdf("scan.pdf", text=""))
    assert doc is None
    assert reason == REASON_NO_TEXT


def test_locked_pdf_opens_when_given_the_password(make_pdf):
    doc, reason = load_document(
        make_pdf("statement.pdf", text="Balance 1000", password="pw"), password="pw")
    assert reason == ""
    assert "Balance 1000" in doc["text"]


def test_a_corrupt_file_names_the_error(make_file):
    path = make_file("broken.pdf", "this is not a pdf at all")
    doc, reason = load_document(path)
    assert doc is None
    assert reason.startswith("unreadable (")


def test_unsupported_types_say_so(make_file):
    doc, reason = load_document(make_file("archive.zip", "PK..."))
    assert doc is None
    assert reason == "unsupported type"
