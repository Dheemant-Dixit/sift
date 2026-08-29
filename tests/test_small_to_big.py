"""
Tests for the small-to-big split: matching on a small unit, serving a large one.

These encode two measured defects rather than two opinions about design. Both
were wrong-entity failures — the model presenting one party's real, correctly
cited value as another party's — and neither is catchable by a grounding check,
because nothing was fabricated. So they are pinned here instead.

  1. An indexed unit that has been cut down below its subject stops being about
     anything. A payslip block reading only `Bank A/C No <digits>` embeds as *an
     account number*, so "what is the landlord's bank account number?" retrieved
     the user's payslips. `child_min` is the floor that prevents it.

  2. A served passage that has been cut away from its document cannot be
     attributed. A Form 16 holds both the employee's designation and the HR
     signatory's; given the signatory's clause alone, both models answered with
     the signatory's title every time. `doc_head` carries the owner back.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sift_downloads.chunk import chunk_one, document_head, line_blocks
from sift_downloads.config import ConfigError, configure
from sift_downloads.generate import build_context_block
from sift_downloads.index import update_index
from sift_downloads.store import FORMAT_VERSION, IndexedChunk, IndexFormatMismatch, VectorStore

# A form shaped like the one that broke: the owner is named once at the top, and
# a blank line separates every row — which is what let a floor of `max // 3`
# strand the account number in a block of its own.
PAYSLIP = """Employee Code 239277 Employee Name RIVER OKONKWO

Date Of Birth 11/07/2004 Date of Joining 07/07/2025

Designation MACHINE LEARNING ENGINEER Bank Name SOMEBANK

Bank A/C No 50100825844769 PAN NO ABCDE1234F
"""


def _doc(text: str, name: str = "payslip.txt") -> dict:
    return {"text": text, "filename": name, "path": f"/tmp/{name}"}


# --- 1. the indexed unit must keep its subject ------------------------------

def test_a_low_floor_strands_an_identifier_away_from_its_owner():
    """The regression itself: at max//3 the account number loses its owner."""
    blocks = line_blocks(PAYSLIP, 200)  # min_chars defaults to 66

    account = next(b for b in blocks if "Bank A/C No" in b)
    assert "RIVER OKONKWO" not in account, (
        "this test documents the OLD broken behaviour — if the owner now "
        "appears, the default floor changed and the next assertion is stale"
    )


def test_the_floor_keeps_an_identifier_with_the_document_it_belongs_to():
    """With the floor raised, the account number is embedded as part of a payslip.

    That is the whole mechanism: it is not that the model reasons about
    ownership, it is that a 200-char block embeds as *a payslip* and so stops
    being retrieved for questions about somebody else's bank account.
    """
    blocks = line_blocks(PAYSLIP, 300, 200)

    account = next(b for b in blocks if "Bank A/C No" in b)
    assert "RIVER OKONKWO" in account
    assert "Employee Code" in account


def test_a_blank_line_does_not_break_a_block_that_is_still_too_small():
    """Blank lines are a preferred break, not a hard one.

    Badly extracted PDFs put a blank line between every visual row; treating
    those as hard boundaries yields one word per block.
    """
    riddled = "\n\n".join(f"line {i}" for i in range(40))

    blocks = line_blocks(riddled, 300, 200)

    assert all(len(b) >= 100 for b in blocks[:-1]), \
        "blank lines broke blocks below the floor"


def test_cuts_never_land_mid_line():
    """A key-value row must never be split from its value."""
    for block in line_blocks(PAYSLIP, 300, 200):
        for line in block.split("\n"):
            assert line in [row.strip() for row in PAYSLIP.split("\n")]


# --- 2. the served passage must carry its document --------------------------

def test_the_served_window_is_larger_than_the_indexed_unit():
    records = chunk_one(_doc("\n".join(f"row {i} of the document" for i in range(400))))

    assert all(len(r["index_text"]) <= len(r["text"]) for r in records)
    assert any(len(r["index_text"]) < len(r["text"]) for r in records), \
        "nothing was actually split small-to-big"


def test_flattening_many_documents_keeps_both_texts_and_the_head():
    """chunk_documents is the library entry point — it must not drop the new fields."""
    from sift_downloads.chunk import chunk_documents

    records = chunk_documents([_doc(PAYSLIP * 40, "a.txt"), _doc(PAYSLIP * 40, "b.txt")])

    assert {r["filename"] for r in records} == {"a.txt", "b.txt"}
    assert all(r["index_text"] and r["doc_head"] for r in records)


def test_an_unbroken_line_is_served_whole_rather_than_cut_mid_sentence():
    """A known limit, pinned deliberately.

    Cuts land only on line boundaries, so a document extracted as one long line
    yields child == parent and degrades to plain windows. That is the right
    trade for the genre this exists for — splitting mid-line is exactly what
    strands a form's value from its key — but it means prose with no newlines
    gets no small-to-big benefit, which is worth knowing before blaming the
    embedding model for it.
    """
    records = chunk_one(_doc("x " * 4000))

    assert all(r["index_text"] == r["text"] for r in records)


def test_every_record_carries_the_document_opening():
    records = chunk_one(_doc(PAYSLIP * 40))

    assert records, "fixture too small to chunk"
    for record in records:
        assert record["doc_head"].startswith("Employee Code 239277 Employee Name")


def test_the_document_head_reaches_the_model():
    """Attribution lives or dies on this string appearing in the prompt."""
    block = build_context_block([{
        "filename": "239277_2025-26.pdf",
        "text": "I, ADA LOVELACE, in the capacity of PRINCIPAL HR BUSINESS PARTNER "
                "(designation) do hereby certify",
        "doc_head": "Form 16 Employee Name: RIVER OKONKWO",
    }])

    assert "Form 16 Employee Name: RIVER OKONKWO" in block
    assert '[Source: 239277_2025-26.pdf | File name reads: "239277 2025 26" ' \
           '| Document begins:' in block


def test_a_chunk_without_a_head_still_renders_a_plain_source_label():
    """Old callers and head-disabled indexes must not produce a broken label."""
    block = build_context_block([{"filename": "a.md", "text": "body"}])

    assert block == "[Source: a.md]\nbody"


def test_the_head_length_is_a_setting_and_can_be_switched_off():
    assert document_head("a" * 500, 120) == "a" * 120
    assert document_head("a" * 500, 0) == ""


def test_the_head_collapses_whitespace_so_it_stays_one_line():
    assert document_head("Name:\n\n  RIVER   OKONKWO\n", 120) == "Name: RIVER OKONKWO"


# --- the invariant that makes all of it work --------------------------------

def test_the_indexed_text_is_what_gets_embedded_not_the_served_text(make_file):
    """If this inverts, retrieval silently degrades to plain windows.

    Nothing would crash and no score would look wrong — the index would simply
    be built from parents while every docstring claimed otherwise.
    """
    make_file("doc.txt", PAYSLIP * 40)
    seen: list[str] = []

    def recording_embedder(texts):
        seen.extend(texts)
        return np.ones((len(texts), 16), dtype=np.float32)

    update_index(embedder=recording_embedder)

    store = VectorStore.load()
    assert seen, "nothing was embedded"
    assert set(seen) == {r.matched_text for r in store.records}
    assert any(r.index_text != r.text for r in store.records)


def test_both_texts_survive_a_save_load_round_trip():
    store = VectorStore([IndexedChunk(
        path="/a.txt", filename="a.txt", chunk_index=0,
        text="the whole parent window", index_text="the child",
        doc_head="Name: RIVER OKONKWO",
        vector=np.ones(4, dtype=np.float32))])
    store.save()

    reloaded = VectorStore.load()

    record = reloaded.records[0]
    assert record.text == "the whole parent window"
    assert record.index_text == "the child"
    assert record.doc_head == "Name: RIVER OKONKWO"


def test_search_returns_the_served_text_not_the_matched_text():
    store = VectorStore([IndexedChunk(
        path="/a.txt", filename="a.txt", chunk_index=0,
        text="the whole parent window", index_text="the child",
        doc_head="Name: RIVER OKONKWO",
        vector=np.array([1.0, 0.0], dtype=np.float32))])

    hit = store.search(np.array([1.0, 0.0], dtype=np.float32), k=1)[0]

    assert hit["text"] == "the whole parent window"
    assert hit["index_text"] == "the child"
    assert hit["doc_head"] == "Name: RIVER OKONKWO"


def test_a_plain_window_index_does_not_store_its_text_twice():
    """With child_size = 0 the two texts coincide and only one is persisted."""
    configure(child_size=0)
    records = chunk_one(_doc("x " * 4000))

    assert all(r["index_text"] == r["text"] for r in records)


# --- refusing an index built under the old rules ----------------------------

def test_an_older_index_is_refused_rather_than_silently_misread():
    """A v1 index is coherent, not corrupt — which is why it must be refused.

    Its vectors describe whole windows. Searching them with code that assumes
    children returns plausible scores for the wrong reasons, and a plausible
    wrong answer is worse than an error.
    """
    store = VectorStore([IndexedChunk(
        path="/a.txt", filename="a.txt", chunk_index=0, text="body",
        vector=np.ones(4, dtype=np.float32))])
    store.save()

    _downgrade_index_on_disk(configure().index_path)

    with pytest.raises(IndexFormatMismatch, match="sift index"):
        VectorStore.load()


def test_a_refused_load_does_not_hold_the_index_file_open(make_file, embedder):
    """The exact production sequence: load fails, then the file is replaced.

    np.load on an .npz returns a lazy handle that keeps the file open. POSIX
    unlinks an open file happily, so this passes trivially there — on Windows it
    raises PermissionError, which turned the automatic upgrade into a crash on
    one platform. Cheap to assert, and the matrix is where it earns its keep.
    """
    make_file("doc.txt", PAYSLIP * 40)
    update_index(embedder=embedder)
    settings = configure()
    _downgrade_index_on_disk(settings.index_path)

    with pytest.raises(IndexFormatMismatch):
        VectorStore.load()

    settings.index_path.unlink()  # PermissionError on Windows if the handle leaked


def test_a_successful_load_does_not_hold_the_index_file_open(make_file, embedder):
    make_file("doc.txt", PAYSLIP * 40)
    update_index(embedder=embedder)
    settings = configure()

    VectorStore.load()

    settings.index_path.unlink()


def _downgrade_index_on_disk(index_path) -> None:
    """Rewrite the index's header to claim format version 1.

    The `with` matters on Windows: an open .npz handle blocks the rewrite.
    """
    import json
    with np.load(index_path, allow_pickle=False) as loaded:
        data = dict(loaded)
    header = json.loads(bytes(data["header"]).decode())
    header["format_version"] = 1
    data["header"] = np.frombuffer(json.dumps(header).encode(), dtype=np.uint8)
    np.savez_compressed(index_path, **data)


def test_a_sync_upgrades_an_old_index_instead_of_failing(make_file, embedder):
    """The upgrade is the next sync being slow once, not a command to remember."""
    make_file("doc.txt", PAYSLIP * 40)
    update_index(embedder=embedder)
    settings = configure()
    _downgrade_index_on_disk(settings.index_path)

    stats = update_index(embedder=embedder)

    assert stats.upgraded
    assert stats.chunks_total > 0
    assert len(VectorStore.load()) == stats.chunks_total


def test_an_ordinary_sync_is_not_reported_as_an_upgrade(make_file, embedder):
    make_file("doc.txt", PAYSLIP * 40)
    update_index(embedder=embedder)

    assert not update_index(embedder=embedder).upgraded


def test_an_upgrade_names_the_files_whose_text_it_could_not_keep(make_file, embedder,
                                                                monkeypatch):
    """A rebuild cannot recover text that only a password made readable.

    sift never stores the password, so that text is genuinely gone — and losing
    it silently would quietly shrink what `ask` can see, with nothing on screen
    to explain why. The file gets named instead.
    """
    make_file("secret.txt", PAYSLIP * 40)
    update_index(embedder=embedder)
    settings = configure()
    _downgrade_index_on_disk(settings.index_path)

    # Now the file reads as locked, exactly as an unlocked PDF does after its
    # in-memory password is gone.
    from sift_downloads import index as index_module
    monkeypatch.setattr(index_module, "load_document",
                        lambda *a, **k: (None, "password-protected"))

    stats = update_index(embedder=embedder)

    assert stats.upgraded
    assert [Path(p).name for p in stats.needs_unlock] == ["secret.txt"]


def test_an_upgrade_reports_nothing_lost_when_every_file_still_reads(make_file, embedder):
    make_file("doc.txt", PAYSLIP * 40)
    update_index(embedder=embedder)
    _downgrade_index_on_disk(configure().index_path)

    assert update_index(embedder=embedder).needs_unlock == []


def test_an_empty_index_is_not_rejected_for_its_version():
    """Nothing to misread, so nothing to refuse."""
    VectorStore([]).save()

    assert len(VectorStore.load()) == 0


def test_the_header_records_the_knobs_that_shaped_the_index():
    """Provenance for the settings that decide what a vector even describes."""
    store = VectorStore([IndexedChunk(
        path="/a.txt", filename="a.txt", chunk_index=0, text="body",
        index_text="body", vector=np.ones(4, dtype=np.float32))])
    store.save()

    header = VectorStore.load().header
    assert header["format_version"] == FORMAT_VERSION
    assert header["child_size"] == 300
    assert header["child_min"] == 200
    assert header["doc_head_chars"] == 120


# --- configuration ----------------------------------------------------------

def test_a_child_can_never_be_larger_than_the_window_it_is_cut_from():
    """Clamped, not rejected — the user set chunk_size, not child_size."""
    settings = configure(chunk_size=200)

    assert settings.child_size <= 200
    assert settings.child_min <= settings.child_size


def test_negative_sizes_are_rejected():
    with pytest.raises(ConfigError, match="cannot be negative"):
        configure(child_size=-1)
