"""
Tests for the query side, and for the package's public surface.

Two small modules, one shared concern: caching that has to be invalidated.

retrieve.get_store() is memoized because loading the index means decompressing
the whole thing. That cache is also the most dangerous one in the codebase — if
`configure()` fails to clear it, a `--source` or `--data-dir` flag silently
queries the previous folder's index and every answer is wrong without an error.

The package's `__getattr__` is the other half: `import sift_downloads` must not
drag in litellm, numpy and pypdf, or `sift --help` takes a second to print.
"""
from __future__ import annotations

import numpy as np
import pytest

import sift_downloads
from sift_downloads import retrieve
from sift_downloads.config import ConfigError, configure
from sift_downloads.index import update_index
from sift_downloads.retrieve import embed_query, get_store, search


@pytest.fixture
def an_index(embedder, make_file):
    """A real index with two files in it."""
    make_file("lease.md", "The notice period for terminating this lease is 60 days.")
    make_file("recipe.md", "Beat the eggs and fold in the flour until smooth.")
    update_index(embedder=embedder)


@pytest.fixture
def fake_embedding(monkeypatch):
    """Stub the embedding call at retrieve's seam, so no Ollama is needed."""
    from tests.conftest import fake_embed
    monkeypatch.setattr(retrieve, "embed_texts", lambda texts, settings=None: fake_embed(texts))


# --- the store cache --------------------------------------------------------

def test_the_store_is_loaded_once_and_reused(an_index):
    assert get_store() is get_store()


def test_configure_drops_the_cached_store(an_index, tmp_path):
    """The dangerous one: a stale store answers from the wrong folder."""
    first = get_store()
    configure(data_dir=tmp_path / "elsewhere")
    assert get_store() is not first


def test_pointing_at_an_empty_data_dir_gives_an_empty_store(an_index, tmp_path):
    configure(data_dir=tmp_path / "empty")
    assert len(get_store()) == 0


# --- search -----------------------------------------------------------------

def test_search_returns_nothing_when_the_index_is_empty(fake_embedding, source_dir):
    assert search("anything") == []


def test_a_query_matching_a_chunk_exactly_retrieves_that_chunk(an_index, fake_embedding):
    """The fake embedder has no semantics, so ask it the one thing it does know:
    identical text gives an identical vector. That still exercises the real path
    — query vector in, correct record out — without pretending the stub can
    understand a paraphrase."""
    stored = next(r for r in get_store().records if r.filename == "lease.md")
    hits = search(stored.text, top_k=2)
    assert hits[0]["filename"] == "lease.md"
    assert hits[0]["score"] == pytest.approx(1.0)


def test_search_respects_top_k(an_index, fake_embedding):
    assert len(search("lease", top_k=1)) == 1


def test_search_falls_back_to_the_configured_top_k(an_index, fake_embedding):
    configure(top_k=1)
    assert len(search("lease")) == 1


def test_search_applies_a_minimum_score(an_index, fake_embedding):
    assert search("lease", min_score=1.1) == []


def test_every_hit_carries_what_a_citation_needs(an_index, fake_embedding):
    hit = search("lease", top_k=1)[0]
    for field in ("filename", "path", "text", "score", "chunk_index", "rank"):
        assert field in hit, f"a hit without {field!r} cannot be cited or ranked"


def test_hits_come_back_ranked_from_one(an_index, fake_embedding):
    hits = search("lease", top_k=2)
    assert [h["rank"] for h in hits] == [1, 2]


def test_scores_are_in_descending_order(an_index, fake_embedding):
    scores = [h["score"] for h in search("lease", top_k=2)]
    assert scores == sorted(scores, reverse=True)


# --- embed_query ------------------------------------------------------------

def test_a_query_vector_is_normalized(fake_embedding):
    """Cosine similarity is only a dot product if both sides are unit length."""
    vector = embed_query("what is my notice period?")
    assert np.isclose(np.linalg.norm(vector), 1.0)


def test_the_query_is_embedded_with_the_configured_model(monkeypatch):
    from tests.conftest import fake_embed
    seen = {}

    def spy(texts, settings=None):
        seen["model"] = settings.embed_model
        return fake_embed(texts)

    monkeypatch.setattr(retrieve, "embed_texts", spy)
    configure(embed_model="ollama/some-other-model")
    embed_query("q")
    assert seen["model"] == "ollama/some-other-model"


# --- the package's public surface ------------------------------------------

@pytest.mark.parametrize("name", [
    "find_files", "FileHit", "answer", "Answer",
    "update_index", "rebuild_index", "search",
])
def test_every_advertised_name_resolves(name):
    assert getattr(sift_downloads, name) is not None


def test_everything_in_dunder_all_is_reachable():
    for name in sift_downloads.__all__:
        assert getattr(sift_downloads, name) is not None, f"__all__ lists {name}, which is missing"


def test_an_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError):
        # The bare attribute access IS the assertion — B018's "useless
        # expression" is the thing under test, so the check is silenced here
        # rather than for the whole file.
        sift_downloads.definitely_not_a_real_name  # noqa: B018


def test_the_version_is_exported():
    assert sift_downloads.__version__.count(".") == 2


def test_litellm_is_kept_offline_by_the_package_import():
    """A price-list download on import would make 'nothing leaves your machine' untrue."""
    import os
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"


def test_the_library_search_path_rejects_a_top_k_below_one(an_index, fake_embedding):
    """`search` is exported from the package, so it is reachable without the CLI.

    `configure()` never sees `--top-k`, so a settings-level check would leave
    this path — and `answer()`, which delegates straight to it — unguarded.
    """
    with pytest.raises(ConfigError, match="at least 1"):
        search("notice period", top_k=0)
