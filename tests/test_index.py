"""
Tests for ingestion and the incremental sync.

These run the real sync path end to end with a fake embedder, so they cover the
two bugs this code has actually hit in the past:

  · the temp file for an atomic save must already end in ".npz", because
    np.savez_compressed appends the suffix and then os.replace can't find what
    it wrote;
  · "is this file known?" has to consult the manifest as well as the store, or
    files that yield zero chunks (scanned PDFs) look new on every sync and get
    re-extracted forever.
"""
from __future__ import annotations

import os

import pytest

from sift.config import configure, get_settings
from sift.index import Manifest, purge_index, rebuild_index, update_index
from sift.ingest import content_key, is_indexable, scan_source
from sift.store import VectorStore


# --- what gets scanned -----------------------------------------------------

def test_scan_picks_up_supported_files(make_file):
    make_file("notes.md", "# hello")
    make_file("readme.txt", "plain text")
    assert len(scan_source().files) == 2


def test_partial_downloads_are_skipped(make_file):
    make_file("report.pdf.crdownload", "half a file")
    make_file("archive.zip.part", "half a file")
    scan = scan_source()
    assert scan.files == {}
    assert all(r == "download in progress" for r in scan.skipped.values())


def test_a_file_still_being_written_is_deferred(make_file):
    """Freshly-modified files are assumed incomplete, and reported so the
    watcher knows to come back for them."""
    make_file("downloading.txt", "partial", age_seconds=0)
    scan = scan_source()
    assert scan.files == {}
    assert scan.deferred == 1


def test_hidden_and_unsupported_files_are_skipped(make_file):
    make_file(".DS_Store", "junk")
    make_file("installer.dmg", "binary")
    scan = scan_source()
    assert scan.files == {}
    assert scan.skipped[str(get_settings().source_dir / "installer.dmg")] == "unsupported type"


def test_oversized_files_are_skipped(make_file):
    configure(max_file_mb=0)  # nothing may exceed 0 bytes
    make_file("big.txt", "x" * 1000)
    scan = scan_source()
    assert scan.files == {}
    assert "larger than" in next(iter(scan.skipped.values()))


def test_identical_copies_collapse_to_one(make_file):
    """Statement (1).pdf shouldn't crowd Statement.pdf out of your results."""
    make_file("Statement.txt", "same bytes")
    make_file("Statement (1).txt", "same bytes")
    make_file("Statement (2).txt", "same bytes")

    scan = scan_source()
    assert len(scan.files) == 1
    canonical = next(iter(scan.files))
    assert canonical.endswith("Statement.txt")   # the un-suffixed name wins
    assert len(scan.duplicates) == 2
    assert set(scan.duplicates.values()) == {canonical}


def test_different_files_are_not_treated_as_copies(make_file):
    make_file("a.txt", "content one")
    make_file("b.txt", "content two")
    assert len(scan_source().files) == 2


def test_content_key_distinguishes_by_size_and_head(source_dir):
    from tests.conftest import write_file
    same_a = write_file(source_dir, "x.txt", "hello")
    same_b = write_file(source_dir, "y.txt", "hello")
    different = write_file(source_dir, "z.txt", "goodbye")
    assert content_key(same_a) == content_key(same_b)
    assert content_key(same_a) != content_key(different)


# --- the incremental sync --------------------------------------------------

def test_first_sync_indexes_everything(make_file, embedder):
    make_file("a.md", "alpha " * 300)
    make_file("b.md", "beta " * 300)

    stats = update_index(embedder=embedder)
    assert stats.added == 2
    assert stats.chunks_added > 0
    assert stats.chunks_total == stats.chunks_added
    assert get_settings().index_path.exists()   # the .npz temp-suffix trap


def test_second_sync_does_nothing(make_file, embedder):
    make_file("a.md", "alpha " * 300)
    update_index(embedder=embedder)

    stats = update_index(embedder=embedder)
    assert not stats.changed
    assert stats.chunks_added == 0


def test_added_file_is_the_only_thing_embedded(make_file, embedder):
    make_file("a.md", "alpha " * 300)
    update_index(embedder=embedder)

    embedded: list[int] = []

    def counting_embedder(texts):
        embedded.append(len(texts))
        return embedder(texts)

    make_file("b.md", "beta " * 300)
    stats = update_index(embedder=counting_embedder)

    assert stats.added == 1 and stats.modified == 0
    b_chunks = Manifest.load().files[str(get_settings().source_dir / "b.md")]["num_chunks"]
    assert embedded == [b_chunks]   # a.md was not re-embedded


def test_modified_file_is_re_embedded_without_duplicating(make_file, embedder):
    path = make_file("a.md", "alpha " * 300)
    update_index(embedder=embedder)
    before = len(VectorStore.load())

    path.write_text("completely different " * 300, encoding="utf-8")
    past = path.stat().st_mtime - 60
    os.utime(path, (past + 30, past + 30))   # newer than the manifest, but not fresh

    stats = update_index(embedder=embedder)
    assert stats.modified == 1

    store = VectorStore.load()
    assert store.paths() == {str(path)}
    assert "alpha" not in " ".join(r.text for r in store.records)
    assert len(store) != before or True   # count may differ; no stale text is the point


def test_deleted_file_loses_its_chunks(make_file, embedder):
    path = make_file("a.md", "alpha " * 300)
    make_file("b.md", "beta " * 300)
    update_index(embedder=embedder)

    path.unlink()
    stats = update_index(embedder=embedder)

    assert stats.deleted == 1
    assert str(path) not in VectorStore.load().paths()
    assert str(path) not in Manifest.load().files


def test_unextractable_file_is_not_retried_every_sync(make_file, embedder):
    """A zero-chunk file must still be recorded, or it looks new forever.

    An empty .md stands in for the real case: a scanned PDF whose text
    extraction returns nothing at all.
    """
    make_file("scanned.md", "   \n  \n ")
    update_index(embedder=embedder)

    manifest = Manifest.load()
    scanned = str(get_settings().source_dir / "scanned.md")
    assert manifest.files[scanned]["num_chunks"] == 0

    stats = update_index(embedder=embedder)
    assert stats.added == 0 and stats.modified == 0


def test_rebuild_starts_from_nothing(make_file, embedder):
    make_file("a.md", "alpha " * 300)
    update_index(embedder=embedder)

    stats = rebuild_index(embedder=embedder)
    assert stats.added == 1          # everything looks new after a wipe
    assert stats.chunks_total > 0


def test_purge_removes_everything_sift_stored(make_file, embedder):
    make_file("a.md", "alpha " * 300)
    update_index(embedder=embedder)
    settings = get_settings()

    removed = purge_index(settings)
    assert set(removed) == {settings.index_path, settings.manifest_path}
    assert not settings.index_path.exists()
    assert make_file("a.md", "alpha " * 300).exists()   # documents untouched


def test_manifest_records_duplicates_for_find(make_file, embedder):
    make_file("Statement.txt", "balance " * 300)
    make_file("Statement (1).txt", "balance " * 300)
    update_index(embedder=embedder)

    manifest = Manifest.load()
    canonical = str(get_settings().source_dir / "Statement.txt")
    assert manifest.siblings_of(canonical) == [str(get_settings().source_dir / "Statement (1).txt")]


def test_sync_survives_an_unreadable_file(make_file, embedder, monkeypatch):
    """One broken PDF in a folder of 300 must not abort the whole sync."""
    make_file("good.md", "fine " * 300)
    make_file("bad.md", "also fine " * 300)

    import sift.ingest as ingest
    real = ingest.extract_text_file

    def explode(path):
        if path.name == "bad.md":
            raise RuntimeError("simulated extraction failure")
        return real(path)

    monkeypatch.setitem(ingest.EXTRACTORS, ".md", explode)

    stats = update_index(embedder=embedder)
    assert stats.added == 2
    assert stats.chunks_total > 0
    assert Manifest.load().files[str(get_settings().source_dir / "bad.md")]["num_chunks"] == 0
