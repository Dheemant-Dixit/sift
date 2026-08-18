"""
The vector store — a single source of truth for the index.

WHY THIS EXISTS
---------------
An earlier version kept two parallel structures on disk: a numpy matrix of
embeddings and a JSON list of chunk metadata, linked only by position ("row i
of the matrix is item i of the list"). That invariant was implicit and manually
maintained, so any edit that touched one and forgot the other would silently
return the wrong text for the right vector — a bug with no crash and no error
message.

This module removes that whole class of bug by making misalignment
*unrepresentable*:

  - There is ONE list of records (`IndexedChunk`), and each record carries its
    own vector. There is no second structure to drift out of sync with.
  - The search matrix is a DERIVED cache, rebuilt from the records on demand
    and invalidated on every mutation. It is never a source of truth.
  - Mutation happens only through `add()` / `remove_paths()`, which operate on
    whole records, so callers cannot desync vectors from text even by mistake.
  - Persistence is ONE atomic file: vectors, metadata and provenance are
    written together via a temp file + os.replace, so a crash cannot leave them
    half-updated.

The same reasoning applies one level up, which is what the `header` is for.
Vectors are only comparable to other vectors from the same embedding model, so
the index records which model built it. Without that, swapping models gives you
either a dimension error (if you are lucky) or perfectly well-formed nonsense
scores (if you are not).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sift_downloads.config import Settings, get_settings, validate_top_k

# 2 added the small-to-big split: a record's embedded text and its served text
# are no longer the same string. A version-1 index has only one, so its vectors
# describe whole windows while the code now expects them to describe children —
# scores would be plausible and wrong. It is refused on load instead.
FORMAT_VERSION = 2


class IndexProblem(Exception):
    """Base class for index problems the user can act on."""


class IndexFormatMismatch(IndexProblem):
    """The index was written by a different version of the on-disk format."""


class CorruptIndex(IndexProblem):
    """The index file is internally inconsistent and must not be trusted."""


class IndexModelMismatch(IndexProblem):
    """The index was built by a different embedding model than is configured."""


@dataclass
class IndexedChunk:
    """One chunk of a document, with everything known about it.

    The vector lives *inside* the record rather than in a separate array. That
    co-location is the entire point: a chunk and its embedding are one object.
    """

    path: str            # source file — for incremental updates and citations
    filename: str        # basename — for citations
    chunk_index: int     # which record within its file
    text: str            # what the model is HANDED (the served window)
    vector: np.ndarray   # the normalized embedding of index_text, not of text
    id: int = -1         # row position, assigned on save; never trusted between edits

    # What was actually EMBEDDED. Empty means it equalled `text`, which is what
    # plain fixed-window chunking produces. Keeping both on the one record is
    # the same discipline as keeping the vector here: a chunk, the text it
    # matched on, and the text it serves are one object, so they cannot drift.
    index_text: str = ""
    doc_head: str = ""   # opening of the source document, for attribution

    @property
    def matched_text(self) -> str:
        """The string this record's vector was built from."""
        return self.index_text or self.text


def normalize(matrix: np.ndarray) -> np.ndarray:
    """Scale each row to unit length, so a dot product equals cosine similarity."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / (norms + 1e-12)


class VectorStore:
    """An in-memory collection of IndexedChunks that can search and persist itself."""

    def __init__(self, records: list[IndexedChunk] | None = None, header: dict | None = None):
        self.records: list[IndexedChunk] = records or []
        self.header: dict = header or {}
        self._matrix: np.ndarray | None = None  # derived cache; None = rebuild needed

    def __len__(self) -> int:
        return len(self.records)

    def paths(self) -> set[str]:
        """The source files currently represented in the store.

        This — not the manifest — is the authoritative answer to "is this file
        indexed?", which is what keeps the sync layer from creating duplicates.
        """
        return {r.path for r in self.records}

    @property
    def dim(self) -> int:
        """Embedding width, taken from the data itself rather than a constant."""
        if self.records:
            return int(self.records[0].vector.shape[0])
        return int(self.header.get("embed_dim") or 0)

    # --- the derived search matrix ----------------------------------------

    @property
    def matrix(self) -> np.ndarray:
        """The (N, dim) matrix of all vectors, rebuilt from records when stale.

        A cache over `self.records`, never a source of truth. Every mutation
        sets it back to None so it is rebuilt fresh on next use.
        """
        if self._matrix is None:
            if self.records:
                self._matrix = np.vstack([r.vector for r in self.records]).astype(np.float32)
            else:
                self._matrix = np.zeros((0, max(self.dim, 1)), dtype=np.float32)
        return self._matrix

    # --- mutation (the only ways to change the store) ---------------------

    def add(self, records: list[IndexedChunk]) -> None:
        self.records.extend(records)
        self._matrix = None

    def remove_paths(self, paths: set[str]) -> int:
        """Drop every chunk belonging to any of `paths`; returns how many went."""
        if not paths:
            return 0
        before = len(self.records)
        self.records = [r for r in self.records if r.path not in paths]
        self._matrix = None
        return before - len(self.records)

    # --- retrieval --------------------------------------------------------

    def search(self, query_vector: np.ndarray, k: int | None = None,
               min_score: float | None = None) -> list[dict]:
        """Return the k most similar chunks to a (normalized) query vector.

        One matrix-vector product, because everything is unit length. Returns
        plain dicts so callers don't depend on the internal record type.
        """
        if k is None:
            k = get_settings().top_k
        validate_top_k(k)
        if not self.records:
            return []
        if query_vector.shape[0] != self.matrix.shape[1]:
            raise CorruptIndex(
                f"query vector is {query_vector.shape[0]}-dimensional but the index "
                f"is {self.matrix.shape[1]}-dimensional — rebuild with `sift index --rebuild`"
            )

        scores = self.matrix @ query_vector
        top = np.argsort(scores)[-k:][::-1]

        results = []
        for rank, i in enumerate(top):
            score = float(scores[i])
            if min_score is not None and score < min_score:
                continue
            r = self.records[i]
            results.append({
                "id": r.id, "path": r.path, "filename": r.filename,
                "chunk_index": r.chunk_index, "text": r.text,
                "index_text": r.matched_text, "doc_head": r.doc_head,
                "score": score, "rank": rank + 1,
            })
        return results

    # --- persistence (one atomic file) ------------------------------------

    def build_header(self, settings: Settings) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "embed_model": settings.embed_model,
            "embed_dim": self.dim,
            "chunk_size": settings.chunk_size,
            "chunk_overlap": settings.chunk_overlap,
            "child_size": settings.child_size,
            "child_min": settings.child_min,
            "doc_head_chars": settings.doc_head_chars,
        }

    def save(self, path: Path | None = None, settings: Settings | None = None) -> None:
        """Persist vectors + metadata + provenance together, atomically.

        Written to a temp file and os.replace()d into position. os.replace is
        atomic, so a reader sees either the whole old index or the whole new
        one — never a half-written file.
        """
        settings = settings or get_settings()
        path = path or settings.index_path

        for i, r in enumerate(self.records):
            r.id = i  # id is just the row index: recomputed, never stored stale

        vectors = self.matrix
        # index_text is stored only when it differs from text, so plain
        # fixed-window indexes don't pay to keep every chunk twice.
        meta = [{"id": r.id, "path": r.path, "filename": r.filename,
                 "chunk_index": r.chunk_index, "text": r.text,
                 "index_text": "" if r.matched_text == r.text else r.index_text,
                 "doc_head": r.doc_head} for r in self.records]
        # Should be impossible by construction — a tripwire that turns any
        # future regression into a loud, immediate crash instead of wrong answers.
        assert len(meta) == vectors.shape[0], \
            f"invariant violated: {len(meta)} records vs {vectors.shape[0]} vectors"

        self.header = self.build_header(settings)

        # Metadata and header as UTF-8 bytes: compact, and no pickle needed,
        # which is what makes the file safe to load.
        meta_bytes = np.frombuffer(json.dumps(meta, ensure_ascii=False).encode("utf-8"),
                                   dtype=np.uint8)
        header_bytes = np.frombuffer(json.dumps(self.header).encode("utf-8"), dtype=np.uint8)

        path.parent.mkdir(parents=True, exist_ok=True)
        # np.savez_compressed appends ".npz" when the name doesn't end in it,
        # so the temp file must already end in ".npz" or os.replace can't find it.
        tmp = path.with_name(path.stem + ".tmp.npz")
        np.savez_compressed(tmp, vectors=vectors, meta=meta_bytes, header=header_bytes)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path | None = None, settings: Settings | None = None,
             check_model: bool = True) -> VectorStore:
        """Load a store from disk (an empty store if there isn't one yet).

        allow_pickle=False means loading can never execute code, so an index
        file is safe to open. Both invariants are re-checked on the way in —
        alignment, and which model built it — and a failure refuses to load
        rather than quietly serving wrong answers.
        """
        settings = settings or get_settings()
        path = path or settings.index_path
        if not path.exists():
            return cls([])

        try:
            # Closed explicitly rather than left to the garbage collector: np.load
            # on an .npz returns a lazy handle that holds the file open, and
            # Windows refuses to unlink an open file. Loading is followed by
            # exactly that when an upgrade replaces the index, so a leaked handle
            # turns "sift rebuilt itself" into PermissionError on one platform.
            # Every value below is materialised inside the block.
            with np.load(path, allow_pickle=False) as data:
                vectors = data["vectors"]
                meta = json.loads(bytes(data["meta"]).decode("utf-8"))
                header = (json.loads(bytes(data["header"]).decode("utf-8"))
                          if "header" in data else {})
        except CorruptIndex:
            raise
        except Exception as e:
            raise CorruptIndex(
                f"could not read the index at {path} ({type(e).__name__}: {e})\n"
                f"  Rebuild it with: sift index --rebuild"
            ) from e

        if len(meta) != vectors.shape[0]:
            raise CorruptIndex(
                f"corrupt index at {path}: {len(meta)} metadata rows vs "
                f"{vectors.shape[0]} vectors\n  Rebuild it with: sift index --rebuild"
            )

        # An older index is not corrupt — it is coherent, just built under
        # different rules. That is precisely why it has to be refused: its
        # vectors describe whole windows, so every score would look reasonable
        # and mean something else. Same stance as the embedding-model check.
        written_with = header.get("format_version", 1) if meta else FORMAT_VERSION
        if written_with != FORMAT_VERSION:
            raise IndexFormatMismatch(
                f"this index is format version {written_with}, but this version of "
                f"sift writes version {FORMAT_VERSION}.\n"
                f"  The change was to what gets embedded, so the existing vectors "
                f"describe different text than searches now assume — the scores "
                f"would be plausible rather than merely wrong.\n"
                f"  Run `sift index` to bring it up to date (it re-embeds once)."
            )

        if check_model and meta:
            built_with = header.get("embed_model")
            if built_with and built_with != settings.embed_model:
                raise IndexModelMismatch(
                    f"this index was built with '{built_with}' but the configured "
                    f"embedding model is '{settings.embed_model}'.\n"
                    f"  Vectors from different models are not comparable — the scores "
                    f"would be meaningless rather than merely wrong.\n"
                    f"  Rebuild with: sift index --rebuild"
                )

        records = [IndexedChunk(vector=vectors[i], **m) for i, m in enumerate(meta)]
        return cls(records, header=header)
