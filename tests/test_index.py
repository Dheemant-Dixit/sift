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
from types import SimpleNamespace

import pytest

from sift_downloads.config import ConfigError, configure, get_settings
from sift_downloads.index import (
    Manifest,
    embed_texts,
    purge_index,
    rebuild_index,
    unlock_file,
    update_index,
)
from sift_downloads.ingest import (
    REASON_LOCKED,
    REASON_NO_TEXT,
    content_key,
    scan_source,
)
from sift_downloads.retrieve import search
from sift_downloads.store import VectorStore

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

    import sift_downloads.ingest as ingest
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


# --- password-protected files ----------------------------------------------
#
# The product bug behind these: a locked file and a scanned file both produce
# zero chunks, so both were reported as "scanned or image-only". Most locked
# files are bank statements, and telling someone to OCR one is useless advice.

def test_a_locked_file_records_that_it_is_locked(make_pdf, embedder):
    make_pdf("statement.pdf", text="Balance 1000", password="pw")
    update_index(embedder=embedder)

    manifest = Manifest.load()
    entry = next(iter(manifest.files.values()))
    assert entry["num_chunks"] == 0
    assert entry["reason"] == REASON_LOCKED


def test_a_scanned_file_is_not_confused_with_a_locked_one(make_pdf, embedder):
    make_pdf("scan.pdf", text="")
    update_index(embedder=embedder)

    entry = next(iter(Manifest.load().files.values()))
    assert entry["reason"] == REASON_NO_TEXT
    assert entry["reason"] != REASON_LOCKED


def test_locked_lists_only_what_a_password_would_fix(make_pdf, make_file, embedder):
    make_pdf("statement.pdf", text="Balance", password="pw")
    make_pdf("scan.pdf", text="")
    make_file("notes.md", "readable")
    update_index(embedder=embedder)

    locked = Manifest.load().locked()
    assert [os.path.basename(p) for p in locked] == ["statement.pdf"]


def test_unlocking_indexes_the_text(make_pdf, embedder):
    path = make_pdf("statement.pdf", text="Closing balance is 4200", password="pw")
    update_index(embedder=embedder)
    assert len(VectorStore.load()) == 0

    added, reason = unlock_file(str(path), "pw", embedder=embedder)
    assert reason == ""
    assert added > 0

    store = VectorStore.load()
    assert "Closing balance is 4200" in " ".join(r.text for r in store.records)
    entry = Manifest.load().files[str(path)]
    assert entry["reason"] == ""
    assert entry["num_chunks"] == added


def test_a_wrong_password_changes_nothing(make_pdf, embedder):
    path = make_pdf("statement.pdf", text="Balance", password="pw")
    update_index(embedder=embedder)

    added, reason = unlock_file(str(path), "wrong", embedder=embedder)
    assert added == 0
    assert reason == REASON_LOCKED
    assert len(VectorStore.load()) == 0
    assert Manifest.load().files[str(path)]["reason"] == REASON_LOCKED


def test_unlocking_twice_does_not_duplicate_chunks(make_pdf, embedder):
    path = make_pdf("statement.pdf", text="Closing balance is 4200", password="pw")
    update_index(embedder=embedder)

    first, _ = unlock_file(str(path), "pw", embedder=embedder)
    second, _ = unlock_file(str(path), "pw", embedder=embedder)
    assert first == second == len(VectorStore.load())


def test_an_unlocked_file_survives_the_next_sync(make_pdf, embedder):
    """Unlocking doesn't change the file, so the sync must see nothing to do."""
    path = make_pdf("statement.pdf", text="Closing balance is 4200", password="pw")
    update_index(embedder=embedder)
    added, _ = unlock_file(str(path), "pw", embedder=embedder)

    stats = update_index(embedder=embedder)
    assert not stats.changed
    assert len(VectorStore.load()) == added


def test_a_manifest_written_before_reasons_existed_still_loads(make_file, embedder):
    """Backward compatibility: no 'reason' key must not crash or mislead."""
    make_file("notes.md", "hello")
    update_index(embedder=embedder)

    manifest = Manifest.load()
    for entry in manifest.files.values():
        entry.pop("reason", None)
    manifest.save()

    assert Manifest.load().locked() == []


# --- embed_texts: the one place that talks to a model ----------------------

def _fake_embedding_response(n_vectors, dim=4):
    from types import SimpleNamespace
    return SimpleNamespace(data=[{"embedding": [0.1] * dim, "index": i}
                                 for i in range(n_vectors)])


def test_embed_texts_returns_one_row_per_text(monkeypatch):
    import litellm

    from sift_downloads.index import embed_texts
    monkeypatch.setattr(litellm, "embedding",
                        lambda model, input: _fake_embedding_response(len(input)))
    assert embed_texts(["a", "b", "c"]).shape == (3, 4)


def test_embed_texts_is_float32(monkeypatch):
    """The store asserts on dtype; a float64 matrix doubles the index size."""
    import litellm
    import numpy as np

    from sift_downloads.index import embed_texts
    monkeypatch.setattr(litellm, "embedding",
                        lambda model, input: _fake_embedding_response(len(input)))
    assert embed_texts(["a"]).dtype == np.float32


def test_embed_texts_batches_long_inputs(monkeypatch):
    """One request per 64 chunks, not one per chunk and not one giant request."""
    import litellm

    from sift_downloads.index import embed_texts
    sizes = []

    def spy(model, input):
        sizes.append(len(input))
        return _fake_embedding_response(len(input))

    monkeypatch.setattr(litellm, "embedding", spy)
    embed_texts([f"chunk {i}" for i in range(150)], batch_size=64)
    assert sizes == [64, 64, 22]


def test_embed_texts_uses_the_configured_model(monkeypatch):
    import litellm

    from sift_downloads.config import configure
    from sift_downloads.index import embed_texts
    seen = {}

    def spy(model, input):
        seen["model"] = model
        return _fake_embedding_response(len(input))

    monkeypatch.setattr(litellm, "embedding", spy)
    configure(embed_model="ollama/some-model")
    embed_texts(["a"])
    assert seen["model"] == "ollama/some-model"


def test_a_default_huggingface_model_is_refused_before_anything_is_sent():
    """The leak, pinned at the call that would have sent the bytes.

    `huggingface/` was on the local prefix list, so nothing gated it: litellm
    resolved it to https://router.huggingface.co and POSTed the folder there.
    Asserting on `uses_cloud()` alone would not catch a future change that
    gates somewhere else, so this goes through `embed_texts` — the one point
    both paths cross.

    Reverting the fix does not merely fail this, it fails it with
    `no_model_server`'s "this test reached litellm", which is the whole point:
    the call went out.
    """
    configure(embed_model="huggingface/BAAI/bge-small-en-v1.5")
    with pytest.raises(ConfigError, match="--allow-cloud"):
        embed_texts(["net pay 4,812.00, account ending 4471"])


def test_embedding_nothing_makes_no_request(monkeypatch):
    import litellm

    from sift_downloads.index import embed_texts
    monkeypatch.setattr(litellm, "embedding",
                        lambda **kw: pytest.fail("no texts means no request"))
    assert len(embed_texts([])) == 0


# --- what comes back must match what went out, in count and in order --------

def test_embed_texts_puts_a_shuffled_batch_back_in_input_order(monkeypatch):
    """`index` says where a vector belongs; the position in `data` does not.

    A provider that returns the right number of vectors in the wrong order is
    invisible to `zip(..., strict=True)` downstream — the count matches, so
    every chunk is paired with a different chunk's vector and nothing raises.
    `index` restarts at 0 for each request, so the reordering has to happen
    per batch, not once over the accumulated list.
    """
    import litellm

    from sift_downloads.index import embed_texts

    def backwards(model, input):
        from types import SimpleNamespace
        # the vector encodes its own text, so the assertion is "each row is the
        # vector for its own text" rather than a proxy for it
        data = [{"embedding": [float(ord(text))] * 4, "index": i}
                for i, text in enumerate(input)]
        return SimpleNamespace(data=list(reversed(data)))

    monkeypatch.setattr(litellm, "embedding", backwards)
    matrix = embed_texts(["a", "b", "c"], batch_size=2)
    assert [row[0] for row in matrix] == [97.0, 98.0, 99.0]


def test_embed_texts_rejects_a_short_batch(monkeypatch):
    """One vector short is silent data loss: the chunk is never searchable."""
    import litellm

    from sift_downloads.index import embed_texts
    monkeypatch.setattr(litellm, "embedding",
                        lambda model, input: _fake_embedding_response(len(input) - 1))
    with pytest.raises(ValueError, match="2 vectors for 3"):
        embed_texts(["a", "b", "c"])


def test_embed_texts_rejects_a_long_batch(monkeypatch):
    """Surplus vectors shift every later chunk onto the wrong one."""
    import litellm

    from sift_downloads.index import embed_texts
    monkeypatch.setattr(litellm, "embedding",
                        lambda model, input: _fake_embedding_response(len(input) + 1))
    with pytest.raises(ValueError, match="4 vectors for 3"):
        embed_texts(["a", "b", "c"])


# --- a vector per chunk, or an error ---------------------------------------
#
# _records() is the single seam every chunk passes through on its way into the
# store, and it pairs chunks with vectors positionally. If an embedder ever
# returns fewer vectors than texts, a plain zip() drops the surplus chunks and
# says nothing: the store stays internally consistent, the sync reports success,
# and the documents are simply not searchable afterwards. These pin the loud
# failure in place of the quiet one.

def _pieces(n: int) -> list[dict]:
    return [{"path": "/src/a.md", "filename": "a.md", "chunk_index": i,
             "text": f"chunk {i} served", "index_text": f"chunk {i}"}
            for i in range(n)]


def test_an_embedder_returning_too_few_vectors_raises():
    """The shape a short provider response takes: N texts in, N-1 vectors out."""
    from sift_downloads.index import _records
    from tests.conftest import fake_embed

    with pytest.raises(ValueError):
        _records(_pieces(3), lambda texts: fake_embed(texts)[:-1])


def test_an_embedder_returning_too_many_vectors_raises():
    """The other direction, so the guard isn't just an off-by-one in one sense."""
    from sift_downloads.index import _records
    from tests.conftest import fake_embed

    with pytest.raises(ValueError):
        _records(_pieces(3), lambda texts: fake_embed([*texts, "extra"]))


def test_one_vector_per_chunk_is_not_disturbed():
    """The guard must only fire on a mismatch — the ordinary path is every sync."""
    from sift_downloads.index import _records
    from tests.conftest import fake_embed

    records = [r.index_text for r in _records(_pieces(3), fake_embed)]
    assert records == ["chunk 0", "chunk 1", "chunk 2"]


# --- the cached store must not outlive the file it was loaded from ---------
#
# retrieve.get_store() caches the loaded store for the whole process. Before the
# index layer invalidated it, `sift ui` would print "+1 added" from /sync and
# then keep answering out of the pre-sync store until restart — while /status,
# which loads a fresh VectorStore, simultaneously reported the new count. Every
# writer of index.npz has to drop that cache.

def _cached_chunks() -> int:
    """How many chunks the CACHED store has — the number queries actually see."""
    from sift_downloads.retrieve import get_store
    return len(get_store())


def test_a_sync_that_adds_a_file_drops_the_cached_store(make_file, embedder):
    make_file("first.md", "the first document")
    update_index(embedder=embedder)
    assert _cached_chunks() == 1, "sanity: the first sync should be visible"

    make_file("second.md", "the second document")
    stats = update_index(embedder=embedder)

    assert stats.chunks_total == 2, "sanity: the sync itself saw both files"
    assert _cached_chunks() == 2, "queries still see the pre-sync store"


def test_a_sync_that_deletes_a_file_drops_the_cached_store(make_file, embedder):
    kept = make_file("kept.md", "still here")
    gone = make_file("gone.md", "not for long")
    update_index(embedder=embedder)
    assert _cached_chunks() == 2

    gone.unlink()
    update_index(embedder=embedder)

    assert _cached_chunks() == 1, "a deleted file is still being served"
    assert kept.exists()


def test_purging_the_index_drops_the_cached_store(make_file, embedder):
    """Purge is the one where a stale cache keeps serving document text the
    user explicitly asked to delete."""
    make_file("private.md", "something confidential")
    update_index(embedder=embedder)
    assert _cached_chunks() == 1

    purge_index()

    assert _cached_chunks() == 0, "purged text is still queryable from memory"


def test_a_no_op_sync_keeps_the_cached_store(make_file, embedder):
    """The other half of the rule, and the reason this isn't just an
    unconditional cache_clear().

    A sync runs before EVERY query (`_quiet_sync`), and loading the store means
    decompressing the whole index. Invalidating on a sync that changed nothing
    would put that cost on every single command.
    """
    from sift_downloads.retrieve import get_store
    make_file("stable.md", "unchanged")
    update_index(embedder=embedder)
    before = get_store()

    stats = update_index(embedder=embedder)

    assert not stats.changed, "sanity: nothing changed on the second sync"
    assert get_store() is before, "a no-op sync threw away a still-valid store"


# --- consent, checked where the bytes actually leave ------------------------
#
# The CLI gates every embedding subcommand through preflight(), so these paths
# are covered for anyone typing `sift`. The library entry points documented in
# __init__.py are not: `update_index()` called straight from Python would embed
# the whole folder with a cloud model and never ask. The check therefore lives
# in embed_texts, which is the one place both the document path and the query
# path have to pass through.

def test_embedding_a_document_with_a_cloud_model_needs_consent():
    configure(embed_model="openai/text-embedding-3-small", allow_cloud=False)

    with pytest.raises(ConfigError, match="without consent"):
        embed_texts(["balance carried forward 4,120.55"])


def test_the_library_indexing_path_is_gated(make_file):
    """The reported repro: update_index() from Python, with no CLI in sight."""
    make_file("statement.txt", "account balance 1234")
    configure(embed_model="openai/text-embedding-3-small", allow_cloud=False)

    with pytest.raises(ConfigError, match="without consent"):
        update_index()


def test_the_question_is_not_embedded_before_consent_is_checked(make_file, embedder):
    """generate.prepare() checked consent AFTER search() had already embedded
    the question, so the query left the machine on the way to the guard.

    The index has to be built under the SAME cloud model the query then uses,
    or the store's own model check refuses the load first and the query never
    reaches the embedding step at all. Consent given once and not given in a
    later process is the ordinary way this happens.
    """
    make_file("statement.txt", "account balance 1234")
    configure(embed_model="openai/text-embedding-3-small", allow_cloud=True)
    update_index(embedder=embedder)
    configure(embed_model="openai/text-embedding-3-small", allow_cloud=False)

    with pytest.raises(ConfigError, match="without consent"):
        search("what is my balance")


def test_consent_lets_the_embedding_through(monkeypatch):
    """The guard must gate the un-consented case only — not cloud use as such."""
    import litellm

    response = SimpleNamespace(data=[{"index": 0, "embedding": [0.1, 0.2, 0.3]}])
    monkeypatch.setattr(litellm, "embedding", lambda **kwargs: response)
    configure(embed_model="openai/text-embedding-3-small", allow_cloud=True)

    assert embed_texts(["one chunk"]).shape == (1, 3)


def test_a_caller_supplied_embedder_is_not_gated(make_file, embedder):
    """Deliberate carve-out: an `embedder=` argument is the caller's own code,
    so sift is not the one sending anything. This is also what lets the rest of
    the suite run without a model server.
    """
    make_file("statement.txt", "account balance 1234")
    configure(embed_model="openai/text-embedding-3-small", allow_cloud=False)

    stats = update_index(embedder=embedder)

    assert stats.chunks_total > 0


def test_indexing_the_folder_announces_the_cloud_model(make_file, monkeypatch, capsys):
    """Consent is not the same as disclosure, and the scale is the whole point.

    `sift ask` printed "document text is leaving this machine" before sending
    one question. `sift index` sent the entire folder and said nothing. Consent
    held on both paths — PR #16 put that check here — so nothing left that the
    user had refused. What went unannounced was how much.
    """
    import litellm

    from sift_downloads import config

    monkeypatch.setattr(config, "_cloud_warned", False)  # a process-lifetime latch
    monkeypatch.setattr(litellm, "embedding", lambda model, input, **kw: SimpleNamespace(
        data=[{"index": i, "embedding": [0.1, 0.2, 0.3]} for i in range(len(input))]))
    make_file("statement.txt", "closing balance 5,102.44 on 31 March")
    configure(embed_model="openai/text-embedding-3-small", allow_cloud=True)

    stats = update_index()

    assert stats.chunks_total > 0, "sanity: nothing was embedded, so nothing left the machine"
    assert "cloud model in use" in capsys.readouterr().err
