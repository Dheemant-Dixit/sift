"""
Tests for VectorStore, focused on the two properties that matter most:

  1. vectors and their metadata can never fall out of alignment
  2. an index knows which model built it, and refuses to be queried by another

Both failures are silent by nature — they produce confident wrong answers
rather than crashes — which is exactly why they're worth testing.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from sift.config import configure, get_settings
from sift.store import (CorruptIndex, IndexedChunk, IndexModelMismatch,
                        VectorStore, normalize)


def _chunk(text: str, path: str, i: int, vec) -> IndexedChunk:
    from pathlib import Path
    return IndexedChunk(path=path, filename=Path(path).name, chunk_index=i,
                        text=text, vector=np.asarray(vec, dtype=np.float32))


def _sample_store() -> VectorStore:
    return VectorStore([
        _chunk("alpha", "/a.md", 0, [1.0, 0.0, 0.0]),
        _chunk("beta",  "/a.md", 1, [0.0, 1.0, 0.0]),
        _chunk("gamma", "/b.md", 0, [0.0, 0.0, 1.0]),
    ])


def test_matrix_tracks_records():
    store = _sample_store()
    assert store.matrix.shape == (3, 3)
    assert len(store) == store.matrix.shape[0]


def test_remove_keeps_alignment():
    store = _sample_store()
    assert store.remove_paths({"/a.md"}) == 2
    assert [r.text for r in store.records] == ["gamma"]
    assert store.matrix.shape[0] == len(store.records)
    # the surviving vector is still gamma's, not shifted onto another row
    assert np.allclose(store.matrix[0], [0.0, 0.0, 1.0])


def test_search_returns_the_right_text():
    store = _sample_store()
    query = normalize(np.array([[0.0, 0.0, 1.0]], dtype=np.float32))[0]
    assert store.search(query, k=1)[0]["text"] == "gamma"


def test_save_load_roundtrip(tmp_path):
    store = _sample_store()
    path = tmp_path / "index.npz"
    store.save(path)
    loaded = VectorStore.load(path)

    assert len(loaded) == 3
    assert [r.text for r in loaded.records] == ["alpha", "beta", "gamma"]
    assert np.allclose(loaded.matrix, store.matrix)
    assert [r.id for r in loaded.records] == [0, 1, 2]   # ids are row positions


def test_header_records_provenance(tmp_path):
    settings = get_settings()
    path = tmp_path / "index.npz"
    _sample_store().save(path)

    header = VectorStore.load(path).header
    assert header["embed_model"] == settings.embed_model
    assert header["embed_dim"] == 3
    assert header["chunk_size"] == settings.chunk_size


def test_load_rejects_index_from_a_different_model(tmp_path):
    """Vectors from two models aren't comparable — the scores would be nonsense."""
    path = tmp_path / "index.npz"
    _sample_store().save(path)

    configure(embed_model="ollama/some-other-model")
    with pytest.raises(IndexModelMismatch, match="rebuild"):
        VectorStore.load(path)


def test_load_rejects_corrupt_index(tmp_path):
    """A mismatched index must refuse to load rather than serve wrong answers."""
    path = tmp_path / "index.npz"
    meta = np.frombuffer(
        json.dumps([{"id": 0, "path": "/x", "filename": "x", "chunk_index": 0, "text": "x"}]).encode(),
        dtype=np.uint8)
    # 2 vectors but only 1 metadata row — deliberately broken
    np.savez_compressed(path, vectors=np.zeros((2, 3), np.float32), meta=meta)

    with pytest.raises(CorruptIndex, match="corrupt"):
        VectorStore.load(path)


def test_load_rejects_unreadable_file(tmp_path):
    path = tmp_path / "index.npz"
    path.write_text("this is not an npz archive")
    with pytest.raises(CorruptIndex):
        VectorStore.load(path)


def test_search_rejects_wrong_sized_query(tmp_path):
    """A dimension mismatch is caught explicitly, not left to numpy."""
    store = _sample_store()
    with pytest.raises(CorruptIndex, match="dimensional"):
        store.search(np.zeros(768, dtype=np.float32), k=1)


def test_missing_index_loads_empty(tmp_path):
    store = VectorStore.load(tmp_path / "nope.npz")
    assert len(store) == 0
    assert store.search(np.zeros(3, dtype=np.float32)) == []
