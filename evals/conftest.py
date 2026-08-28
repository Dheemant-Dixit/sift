"""Fixtures for the answer-quality evals.

Unlike tests/conftest.py, which forbids real model calls, everything here needs
them. That is the point: this suite exists to measure the parts of sift that a
fake embedder cannot reach. It is not collected by `pytest` (testpaths is
tests/), and it skips itself rather than failing when Ollama is not running.

The index is built once per session with the real pipeline — real chunking,
real embeddings, the product's own update_index() — because an eval that
reimplements the pipeline stops testing the pipeline the moment the two drift.
"""
from __future__ import annotations

import pytest

from evalset import CORPUS, MIN_SCORE
from sift_downloads import config, doctor
from sift_downloads.config import configure


def _missing_models(settings) -> list[str]:
    """Ollama models the eval needs but the machine doesn't have."""
    installed = doctor._installed_ollama_models()
    if installed is None:
        return ["<no Ollama server>"]
    wanted = [settings.embed_model, settings.chat_model]
    return [m for m in wanted if not doctor._model_present(installed, m)]


@pytest.fixture(scope="session")
def indexed_corpus(tmp_path_factory):
    """Point sift at the fixture corpus and build a real index over it."""
    data = tmp_path_factory.mktemp("eval-data")

    import os
    for key in [k for k in os.environ if k.startswith("SIFT_")]:
        os.environ.pop(key)
    os.environ["SIFT_SOURCE"] = str(CORPUS)
    os.environ["SIFT_DATA_DIR"] = str(data)
    os.environ["SIFT_MIN_SCORE"] = str(MIN_SCORE)

    config.reset_caches()
    settings = configure()

    missing = _missing_models(settings)
    if missing:
        pytest.skip(f"needs Ollama with: {', '.join(missing)}")

    from sift_downloads.index import update_index
    from sift_downloads.retrieve import get_store

    stats = update_index(settings=settings)
    assert stats.chunks_total, f"the fixture corpus produced no chunks ({stats})"
    get_store.cache_clear()
    yield settings

    config.reset_caches()
    get_store.cache_clear()


@pytest.fixture(scope="session")
def ask(indexed_corpus):
    """Answer a question through the real generate path, N times.

    Repeated because a single sample from a temperature-0.1 local model is not
    evidence. Tests assert on all N, so a marker that only sometimes appears is
    a failure rather than a coin flip that happened to land right.
    """
    from sift_downloads.generate import answer

    def _ask(question: str, runs: int = 3):
        return [answer(question, settings=indexed_corpus) for _ in range(runs)]

    return _ask


@pytest.fixture(scope="session")
def ungated(indexed_corpus):
    """The same corpus with the lexical gate off.

    The gate refuses all three negatives on their words alone, which is the
    right product behaviour and stops the leak test below from testing
    anything: a question the model never sees cannot leak. So that test runs
    the dense path deliberately, and the two mechanisms stay measurable apart.
    """
    import dataclasses

    from sift_downloads.generate import answer

    settings = dataclasses.replace(indexed_corpus, lexical_gate=False)

    def _ask(question: str, runs: int = 3):
        return [answer(question, settings=settings) for _ in range(runs)]

    return _ask


@pytest.fixture(scope="session")
def prepared(indexed_corpus):
    """(chunks, refusal) straight from the guardrail — no chat model needed.

    Everything `ask` does up to the model call, which is where both refusals
    live: the word check and the relevance bar.
    """
    from sift_downloads.generate import prepare

    def _prepared(query: str) -> tuple[list[dict], object]:
        return prepare(query, settings=indexed_corpus)

    return _prepared


@pytest.fixture(scope="session")
def served(prepared):
    """The passages the MODEL is handed, after the bar and after collapsing.

    `retrieved` below is the raw store output; this is what `ask` would put in
    the context block. Same reason it exists separately: it needs only the
    274MB embedding model, so the duplicate-passage regression stays checkable
    without the 4.9GB chat one.
    """
    def _served(query: str) -> list[dict]:
        return prepared(query)[0]

    return _served


@pytest.fixture(scope="session")
def retrieved(indexed_corpus):
    """The passages a query actually admits, at the product's own thresholds.

    Cheaper than `ask` and needs only the 274MB embedding model, so the
    retrieval-layer regressions stay checkable on a machine that never
    downloaded the 4.9GB one.
    """
    from sift_downloads.retrieve import search

    def _retrieved(query: str) -> list[dict]:
        return search(query, settings=indexed_corpus)

    return _retrieved
