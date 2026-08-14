"""
Building and incrementally maintaining the index.

This is the *sync layer*: it decides WHAT needs (re)embedding and drives the
VectorStore accordingly. Everything about keeping vectors aligned with their
text lives in store.py — here we only ever add and remove whole files' worth of
chunks.

Incremental is the point. A full rebuild of a real Downloads folder takes
tens of seconds because every chunk has to be embedded; a sync where nothing
changed takes under a second, because the manifest can prove nothing changed.
That difference is what makes `sift index` cheap enough to run before every
query.

State on disk (in the platform data dir, never the install dir):
  index.npz     — the VectorStore: vectors + metadata + provenance, one atomic file
  manifest.json — per-file fingerprints, duplicates and skip reasons
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import litellm
import numpy as np

from sift.chunk import chunk_one
from sift.config import Settings, get_settings
from sift.ingest import load_document, scan_source
from sift.store import IndexedChunk, VectorStore, normalize

log = logging.getLogger(__name__)

Embedder = Callable[[list[str]], np.ndarray]


def embed_texts(texts: list[str], settings: Settings | None = None,
                batch_size: int = 64) -> np.ndarray:
    """Embed a list of strings into an (N, dim) float32 matrix."""
    settings = settings or get_settings()
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = litellm.embedding(model=settings.embed_model, input=batch)
        vectors.extend(item["embedding"] for item in resp.data)
        log.info("  embedded %d/%d", min(i + batch_size, len(texts)), len(texts))
    return np.array(vectors, dtype=np.float32)


# ---------------------------------------------------------------------------
# The manifest — everything we know about the folder, minus the vectors
# ---------------------------------------------------------------------------

@dataclass
class Manifest:
    """Fingerprints and bookkeeping, used to detect what changed since last sync.

    `duplicates` and `skipped` are not needed to build the index, but they are
    what lets `sift find` report "there are 2 other copies of this" and
    `sift status` answer "why isn't my file in here".
    """

    files: dict[str, dict] = field(default_factory=dict)       # path -> {size, mtime, num_chunks}
    duplicates: dict[str, str] = field(default_factory=dict)   # copy path -> canonical path
    skipped: dict[str, str] = field(default_factory=dict)      # path -> reason
    updated_at: float = 0.0

    @classmethod
    def load(cls, path: Path | None = None, settings: Settings | None = None) -> "Manifest":
        settings = settings or get_settings()
        path = path or settings.manifest_path
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("manifest at %s is unreadable — treating as empty", path)
            return cls()
        return cls(
            files=data.get("files", {}),
            duplicates=data.get("duplicates", {}),
            skipped=data.get("skipped", {}),
            updated_at=data.get("updated_at", 0.0),
        )

    def save(self, path: Path | None = None, settings: Settings | None = None) -> None:
        """Write atomically, for the same reason the index is written atomically."""
        settings = settings or get_settings()
        path = path or settings.manifest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def siblings_of(self, canonical_path: str) -> list[str]:
        """Other files on disk that are byte-identical copies of this one."""
        return [dup for dup, target in self.duplicates.items() if target == canonical_path]


def _changed(fingerprint: dict, entry: dict | None) -> bool:
    """True if a file's fingerprint differs from what the manifest recorded."""
    if entry is None:
        return True
    return (fingerprint["size"] != entry.get("size")
            or fingerprint["mtime"] != entry.get("mtime"))


# ---------------------------------------------------------------------------
# The incremental sync
# ---------------------------------------------------------------------------

@dataclass
class SyncStats:
    added: int = 0
    modified: int = 0
    deleted: int = 0
    chunks_added: int = 0
    chunks_total: int = 0
    files_total: int = 0
    duplicates: int = 0
    skipped: int = 0
    deferred: int = 0        # files too freshly written to read yet
    seconds: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.deleted)


def update_index(settings: Settings | None = None,
                 embedder: Embedder | None = None) -> SyncStats:
    """Sync the index to the current state of the folder, touching only what changed.

    `embedder` exists so tests (and anyone embedding this as a library) can swap
    in a deterministic function and exercise the whole sync path without a model
    server running.
    """
    settings = settings or get_settings()
    embed = embedder or (lambda texts: embed_texts(texts, settings))
    t0 = time.time()

    scan = scan_source(settings)
    store = VectorStore.load(settings=settings)
    manifest = Manifest.load(settings=settings)

    # A file counts as "known" if it has chunks in the store OR the manifest
    # recorded processing it. The manifest half matters: files that yield zero
    # chunks (scanned PDFs, encrypted PDFs) are absent from the store, so
    # without it they would look new on every single sync and get re-extracted
    # forever. Duplicates are still impossible because a path's old chunks are
    # removed before its new ones are added.
    known = store.paths() | set(manifest.files)
    added = [p for p in scan.files if p not in known]
    modified = [p for p in scan.files
                if p in known and _changed(scan.files[p], manifest.files.get(p))]
    deleted = [p for p in known if p not in scan.files]

    stats = SyncStats(
        added=len(added), modified=len(modified), deleted=len(deleted),
        duplicates=len(scan.duplicates), skipped=len(scan.skipped),
        deferred=scan.deferred,
    )

    to_add = added + modified
    if to_add or deleted:
        # Remove vanished files AND the previous version of anything about to be
        # rewritten. Clearing to_add's paths first makes the whole operation
        # idempotent: even if stale chunks somehow lingered, no duplicates.
        store.remove_paths(set(deleted) | set(to_add))
        for path in deleted:
            manifest.files.pop(path, None)

        new_pieces: list[dict] = []
        for path in to_add:
            doc = load_document(Path(path))
            pieces = chunk_one(doc, settings) if doc else []  # failed extraction -> 0 chunks
            new_pieces.extend(pieces)
            # Record the fingerprint even for 0-chunk files, so a scanned PDF
            # isn't re-extracted on every future sync.
            manifest.files[path] = {**scan.files[path], "num_chunks": len(pieces)}

        if new_pieces:
            log.info("Embedding %d new/changed chunks ...", len(new_pieces))
            vectors = normalize(embed([c["text"] for c in new_pieces]))
            store.add([
                IndexedChunk(path=c["path"], filename=c["filename"],
                             chunk_index=c["chunk_index"], text=c["text"], vector=vector)
                for c, vector in zip(new_pieces, vectors)
            ])
        stats.chunks_added = len(new_pieces)
        store.save(settings=settings)

    # The manifest is rewritten even on a no-op sync: duplicates and skip
    # reasons can change without any indexed file changing (a new copy appears,
    # a huge file is dropped in), and `find`/`status` read them.
    manifest.duplicates = scan.duplicates
    manifest.skipped = scan.skipped
    manifest.updated_at = time.time()
    manifest.save(settings=settings)

    stats.chunks_total = len(store)
    stats.files_total = len(manifest.files)
    stats.seconds = round(time.time() - t0, 2)
    return stats


def rebuild_index(settings: Settings | None = None,
                  embedder: Embedder | None = None) -> SyncStats:
    """Wipe the index and build it fresh — a full re-embed of everything.

    Needed after changing the embedding model or the chunking knobs, since
    neither can be applied retroactively to vectors already on disk.
    """
    settings = settings or get_settings()
    settings.index_path.unlink(missing_ok=True)
    settings.manifest_path.unlink(missing_ok=True)
    return update_index(settings, embedder)


def purge_index(settings: Settings | None = None) -> list[Path]:
    """Delete everything sift has stored. Returns the files removed."""
    settings = settings or get_settings()
    removed = []
    for path in (settings.index_path, settings.manifest_path):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed
