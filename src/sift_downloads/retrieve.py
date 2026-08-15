"""
Retrieval — the query side of the index.

Embed the question with the SAME model used at index time (query and chunks
have to share a vector space, which is what the index's provenance header
enforces), then let the VectorStore do the similarity search.

All the matrix and alignment mechanics live in store.py; this is just the
query-side glue.
"""
from __future__ import annotations

import functools

from sift_downloads.config import Settings, get_settings
from sift_downloads.index import embed_texts
from sift_downloads.store import VectorStore, normalize


@functools.lru_cache(maxsize=1)
def get_store() -> VectorStore:
    """Load the store once per process and reuse it across queries.

    Cached because loading means decompressing the whole index. `config.reset_caches()`
    clears this — without that, a `--source` or `--data-dir` flag would go on
    querying the previously loaded index.
    """
    return VectorStore.load()


def embed_query(query: str, settings: Settings | None = None):
    """Embed one query and normalize it exactly as the chunks were normalized."""
    settings = settings or get_settings()
    return normalize(embed_texts([query], settings))[0]


def search(query: str, top_k: int | None = None, min_score: float | None = None,
           settings: Settings | None = None) -> list[dict]:
    """Return the top_k chunks most similar to `query`, each with its score."""
    settings = settings or get_settings()
    top_k = settings.top_k if top_k is None else top_k
    store = get_store()
    if not len(store):
        return []
    return store.search(embed_query(query, settings), k=top_k, min_score=min_score)
