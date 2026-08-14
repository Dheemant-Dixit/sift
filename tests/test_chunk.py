"""
Tests for chunking.

Overlap is the whole point of the design — a sentence sitting across a cut has
to survive whole in at least one chunk — and the loop guard is the kind of thing
that works fine until someone sets an unusual overlap and the process hangs.
"""
from __future__ import annotations

from sift.chunk import _best_cut, chunk_one, chunk_text
from sift.config import configure


def test_short_text_is_one_chunk():
    assert chunk_text("hello world", size=100, overlap=10) == ["hello world"]


def test_chunks_cover_the_whole_text():
    text = "word " * 500
    chunks = chunk_text(text, size=200, overlap=50)
    assert len(chunks) > 1
    # nothing is dropped: every chunk's content appears in the original
    for chunk in chunks:
        assert chunk in text


def test_adjacent_chunks_actually_overlap():
    """Without this, a fact split across a boundary is unfindable in both halves."""
    text = " ".join(f"token{i}" for i in range(400))
    chunks = chunk_text(text, size=300, overlap=100)
    assert len(chunks) >= 2

    tail_words = set(chunks[0].split()[-5:])
    head_words = set(chunks[1].split()[:20])
    assert tail_words & head_words, "expected the end of chunk 0 to reappear in chunk 1"


def test_cut_snaps_to_a_paragraph_break():
    text = "a" * 80 + "\n\n" + "b" * 80
    cut = _best_cut(text, 0, 100)
    assert text[:cut].endswith("\n\n")


def test_cut_prefers_paragraph_over_sentence():
    # Both boundaries sit inside the last 20% of the window (index >= 80), so
    # the preference order is what decides — put them outside it and neither is
    # eligible at all.
    text = "x" * 82 + ". " + "y" + "\n\n" + "z" * 20
    cut = _best_cut(text, 0, 100)
    assert text[:cut].endswith("\n\n")


def test_boundaries_outside_the_snap_window_are_ignored():
    """Snapping only looks at the last 20%, so it can't shorten a chunk badly."""
    text = "x" * 40 + "\n\n" + "y" * 200
    assert _best_cut(text, 0, 100) == 100


def test_cut_falls_back_to_hard_end():
    """One unbroken run of characters has no boundary to snap to."""
    text = "x" * 200
    assert _best_cut(text, 0, 100) == 100


def test_overlap_as_large_as_the_chunk_still_terminates():
    """The max(end - overlap, start + 1) guard: without it this never returns."""
    chunks = chunk_text("word " * 200, size=50, overlap=50)
    assert len(chunks) > 1  # completed rather than hanging


def test_chunk_size_comes_from_settings_at_call_time(tmp_path):
    """The bug this guards: a size bound at import time ignores every override."""
    text = "word " * 400
    configure(chunk_size=200, chunk_overlap=20)
    small = chunk_text(text)
    configure(chunk_size=2000, chunk_overlap=20)
    large = chunk_text(text)
    assert len(small) > len(large)


def test_chunk_one_carries_provenance():
    doc = {"path": "/downloads/lease.pdf", "filename": "lease.pdf",
           "text": "clause " * 500, "num_chars": 3000}
    records = chunk_one(doc)
    assert len(records) > 1
    assert all(r["path"] == "/downloads/lease.pdf" for r in records)
    assert all(r["filename"] == "lease.pdf" for r in records)
    assert [r["chunk_index"] for r in records] == list(range(len(records)))
