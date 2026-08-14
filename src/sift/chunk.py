"""
Splitting a document into overlapping windows.

Each chunk becomes one unit that gets embedded and retrieved independently, so
the size is a real tradeoff: too small and a chunk loses the context that makes
it meaningful, too large and its embedding averages out into mush that matches
everything weakly and nothing strongly.

Cuts are *snapped* to a nearby natural boundary (paragraph > line > sentence >
word) so a chunk rarely ends mid-sentence. This is a readable cousin of
LangChain's RecursiveCharacterTextSplitter — enough to see the mechanic without
the machinery.
"""
from __future__ import annotations

from sift.config import Settings, get_settings


def _best_cut(text: str, start: int, hard_end: int) -> int:
    """Find a good place to end a chunk at or before `hard_end`.

    Only the last ~20% of the window is searched, so snapping can tidy up an
    ending without drastically shortening the chunk. With no boundary found, it
    cuts at hard_end.
    """
    if hard_end >= len(text):
        return len(text)

    window = text[start:hard_end]
    search_from = int(len(window) * 0.8)

    for marker in ("\n\n", "\n", ". ", " "):
        idx = window.rfind(marker, search_from)
        if idx != -1:
            # +len(marker) keeps the boundary characters with the current chunk.
            return start + idx + len(marker)

    return hard_end


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Split one document's text into overlapping chunks.

    `size` and `overlap` default to the active settings rather than being bound
    at import time, which is what lets --chunk-size actually take effect.
    """
    settings = get_settings()
    size = settings.chunk_size if size is None else size
    overlap = settings.chunk_overlap if overlap is None else overlap

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = _best_cut(text, start, start + size)

        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)

        if end >= len(text):
            break
        # Step back by `overlap` so adjacent chunks share a margin — a sentence
        # split across a boundary still appears whole in one of them. The
        # max(..., start + 1) is a liveness guard: without it, an overlap as
        # large as the chunk would leave `start` unchanged and loop forever.
        start = max(end - overlap, start + 1)

    return chunks


def chunk_one(doc: dict, settings: Settings | None = None) -> list[dict]:
    """Chunk a single document into records carrying their provenance.

    Every chunk keeps `path` (so the indexer can drop a file's chunks when it
    changes or disappears) and `filename` (so an answer can cite its source).
    """
    settings = settings or get_settings()
    pieces = chunk_text(doc["text"], settings.chunk_size, settings.chunk_overlap)
    return [
        {
            "filename": doc["filename"],
            "path": doc["path"],
            "chunk_index": i,
            "text": piece,
        }
        for i, piece in enumerate(pieces)
    ]


def chunk_documents(documents: list[dict], settings: Settings | None = None) -> list[dict]:
    """Flatten a list of documents into a single list of chunk records."""
    return [rec for doc in documents for rec in chunk_one(doc, settings)]
