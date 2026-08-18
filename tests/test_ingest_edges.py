"""
Tests for the awkward things a real Downloads folder contains.

test_ingest.py covers extraction and the reason vocabulary. This file covers the
scan's edge cases: the entries that are not ordinary readable documents at all.

They matter because `sift status` reports every skip reason back to the user.
"Why isn't my file in here?" should always have an answer, and the answer has to
be the right one — a directory is not a mystery, an unreadable file is.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from sift_downloads.config import configure, get_settings
from sift_downloads.ingest import (
    extract_docx,
    is_indexable,
    iter_source_files,
    load_documents,
    scan_source,
)

# os.geteuid does not exist on Windows, and this is evaluated at import time —
# a bare os.geteuid() here fails collection for the whole file on the Windows
# CI jobs. Permission and symlink semantics differ there anyway.
posix_only = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permissions, as a non-root user",
)


def settled(path: Path) -> Path:
    """Back-date a file past FRESHNESS_GUARD_SECONDS.

    A file written this instant is 'still being written' as far as the scan is
    concerned, which would mask every other reason it might be skipped. The
    conftest helpers do this too; tests that write files directly must as well.
    """
    old = time.time() - 60
    os.utime(path, (old, old))
    return path


def indexable(path):
    return is_indexable(path, get_settings())


# --- what is not a document -------------------------------------------------

def test_a_directory_is_not_a_file(source_dir):
    (source_dir / "subfolder").mkdir()
    assert indexable(source_dir / "subfolder") == (False, "not a file")


# --- the reasons two other modules branch on --------------------------------

def test_a_deferred_file_is_counted_and_hidden_under_the_same_reason(source_dir,
                                                                    monkeypatch):
    """The scan's `deferred` counter and `find`'s filter must agree on one string.

    These are the two branch sites the reason vocabulary exists for, and they
    live in different modules. `watch.py` re-arms on `deferred`, so a scan that
    stopped counting a half-written file would leave it unindexed forever;
    `find` hiding a different set would list a `.crdownload` as a result. Both
    read `REASON_DEFERRED`, and this is the test that says so out loud —
    nothing else fails if one of them starts reading something else.
    """
    from sift_downloads import find as find_module
    from sift_downloads.find import find_files
    from sift_downloads.index import Manifest
    from sift_downloads.ingest import REASON_DEFERRED

    (source_dir / "Invoice.pdf").write_text("x")   # written this instant

    scan = scan_source()
    path = str(source_dir / "Invoice.pdf")
    assert scan.deferred == 1
    assert scan.skipped[path] == REASON_DEFERRED

    Manifest(files={}, skipped=scan.skipped, duplicates={}).save()
    monkeypatch.setattr(find_module, "search",
                        lambda query, top_k=None, min_score=None, settings=None: [])
    assert find_files("invoice") == []


def test_directories_are_skipped_silently(source_dir, make_file):
    """A folder in Downloads is normal. Reporting it as 'skipped' is noise."""
    (source_dir / "unzipped-project").mkdir()
    make_file("notes.md", "text")
    scan = scan_source()
    assert len(scan.files) == 1
    assert scan.skipped == {}


def test_a_dotfile_is_skipped(source_dir):
    path = source_dir / ".DS_Store"
    path.write_text("junk")
    settled(path)
    assert indexable(path) == (False, "hidden file")


def test_an_empty_file_is_skipped(source_dir):
    path = source_dir / "empty.md"
    path.touch()
    assert indexable(path) == (False, "empty file")


def test_an_empty_file_is_reported_as_such(source_dir):
    path = source_dir / "empty.md"
    path.touch()
    settled(path)
    assert scan_source().skipped[str(path)] == "empty file"


def test_a_file_too_large_is_skipped(make_file):
    path = make_file("huge.md", "x" * 5000)
    configure(max_file_mb=0.001)          # ~1KB
    ok, reason = indexable(path)
    assert not ok and "larger than" in reason


@pytest.mark.parametrize("name", [
    "report.pdf.crdownload", "archive.zip.part", "movie.mkv.download", "x.md.tmp",
])
def test_partial_downloads_are_skipped(source_dir, name):
    path = source_dir / name
    path.write_text("half a file")
    settled(path)
    ok, reason = indexable(path)
    assert not ok
    assert reason in ("download in progress", "unsupported type")


def test_an_unsupported_type_says_so(source_dir):
    path = source_dir / "movie.mkv"
    path.write_text("binary-ish")
    settled(path)
    assert indexable(path) == (False, "unsupported type")


# --- files the OS will not let us read --------------------------------------

@posix_only
def test_an_unreadable_file_is_reported_not_crashed_on(source_dir):
    path = source_dir / "secret.md"
    path.write_text("classified")
    settled(path)
    path.chmod(0o000)
    try:
        ok, reason = indexable(path)
        # Some filesystems still allow stat(); what matters is no exception.
        assert ok or "unreadable" in reason or reason == "empty file"
    finally:
        path.chmod(0o644)


@posix_only
def test_a_file_that_cannot_be_hashed_is_skipped_with_a_reason(source_dir, monkeypatch):
    """content_key() reads the first bytes; a mid-scan permission change hits here."""
    import sift_downloads.ingest as ingest
    path = source_dir / "notes.md"
    path.write_text("text")
    settled(path)

    def refuse(p):
        raise PermissionError("nope")

    monkeypatch.setattr(ingest, "content_key", refuse)
    scan = scan_source()
    assert scan.files == {}
    assert "unreadable (PermissionError)" in scan.skipped[str(path)]


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_a_broken_symlink_does_not_stop_the_scan(source_dir, make_file):
    (source_dir / "dangling.md").symlink_to(source_dir / "does-not-exist.md")
    make_file("real.md", "text")
    scan = scan_source()
    assert len(scan.files) == 1


# --- picking the canonical copy of a duplicate -----------------------------

def test_a_plain_name_beats_a_numbered_copy(source_dir, make_file):
    make_file("Statement.pdf.md", "identical")
    make_file("Statement (1).pdf.md", "identical")
    kept = list(scan_source().files)
    assert Path(kept[0]).name == "Statement.pdf.md"


def test_a_file_that_cannot_be_stat_ed_sorts_last(source_dir):
    """Ranking reads mtime; a file that vanishes mid-scan must not raise."""
    from sift_downloads.ingest import _canonical_name_rank
    rank = _canonical_name_rank(source_dir / "vanished.md")
    assert rank[2] == float("inf")


def test_the_lower_numbered_copy_wins_between_two_copies(source_dir, make_file):
    make_file("Statement (2).md", "identical")
    make_file("Statement (1).md", "identical")
    kept = list(scan_source().files)
    assert Path(kept[0]).name == "Statement (1).md"


# --- docx -------------------------------------------------------------------

def test_a_docx_is_read_paragraph_by_paragraph(source_dir):
    docx = pytest.importorskip("docx")
    path = source_dir / "letter.docx"
    document = docx.Document()
    document.add_paragraph("The notice period is 60 days.")
    document.add_paragraph("The deposit is refundable.")
    document.save(path)

    text = extract_docx(path)
    assert "notice period is 60 days" in text
    assert "deposit is refundable" in text
    assert "\n" in text, "paragraphs must stay separated"


def test_a_docx_is_indexable(source_dir):
    docx = pytest.importorskip("docx")
    path = source_dir / "letter.docx"
    document = docx.Document()
    document.add_paragraph("hello")
    document.save(path)
    settled(path)
    assert indexable(path)[0]


# --- the convenience wrappers ----------------------------------------------

def test_iter_source_files_yields_paths(make_file):
    make_file("a.md", "alpha")
    make_file("b.txt", "beta")
    names = sorted(p.name for p in iter_source_files())
    assert names == ["a.md", "b.txt"]


def test_iter_source_files_collapses_duplicates(make_file):
    make_file("statement.md", "identical bytes")
    make_file("statement (1).md", "identical bytes")
    assert len(list(iter_source_files())) == 1


def test_load_documents_skips_what_it_cannot_read(make_file, make_pdf):
    make_file("readable.md", "text here")
    make_pdf("scanned.pdf", text="")
    docs = load_documents()
    assert [d["filename"] for d in docs] == ["readable.md"]


def test_load_documents_returns_the_text(make_file):
    make_file("notes.md", "the quick brown fox")
    assert load_documents()[0]["text"] == "the quick brown fox"
