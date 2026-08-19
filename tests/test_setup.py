"""
Tests for `sift setup` — the half of doctor that acts.

Two seams, both stubbed here and never reached for real:
`doctor._installed_ollama_models` says what Ollama has, and `setup._pull_stream`
is the only place this module talks to `/api/pull`.

The assertions lean on two habits this repo learned the hard way. A model sift
cannot pull has to be *named*, because a missing line reads as fine. And a pull
that stops early must fail loudly — a stream that ends without `success` has
downloaded a partial model, and reporting that as done is the silent kind of
wrong.
"""
from __future__ import annotations

import pytest

from sift_downloads import doctor, setup
from sift_downloads.config import configure, get_settings
from sift_downloads.setup import PullProgress, SetupError, plan_setup, pull_model


@pytest.fixture
def ollama(monkeypatch):
    """Control what Ollama appears to have installed. None = not running."""
    box = {"models": ["nomic-embed-text:latest", "llama3.1:8b"]}
    monkeypatch.setattr(doctor, "_installed_ollama_models", lambda: box["models"])
    return box


@pytest.fixture
def stream(monkeypatch):
    """Control what `/api/pull` appears to send back, per model."""
    box = {"events": [{"status": "success"}], "asked": []}

    def fake(model: str):
        box["asked"].append(model)
        yield from box["events"]

    monkeypatch.setattr(setup, "_pull_stream", fake)
    return box


# --- planning ---------------------------------------------------------------

def test_a_model_ollama_does_not_have_is_planned_for_pulling(ollama):
    ollama["models"] = []
    plan = plan_setup(get_settings())
    assert plan.to_pull == ["ollama/nomic-embed-text", "ollama_chat/llama3.1:8b"]
    assert plan.ready is False


def test_a_model_already_pulled_is_left_alone(ollama):
    plan = plan_setup(get_settings())
    assert plan.to_pull == []
    assert plan.ready is True


def test_one_model_configured_for_both_jobs_is_pulled_once(ollama):
    ollama["models"] = []
    configure(embed_model="ollama/same-model", chat_model="ollama/same-model")
    plan = plan_setup(get_settings())
    assert plan.to_pull == ["ollama/same-model"]


def test_a_model_sift_cannot_pull_is_named_rather_than_ignored(ollama):
    configure(chat_model="claude-sonnet-4-5", allow_cloud=True)
    plan = plan_setup(get_settings())
    assert plan.to_pull == []
    assert [m for m, _ in plan.skipped] == ["claude-sonnet-4-5"]
    assert "ollama" in dict(plan.skipped)["claude-sonnet-4-5"].lower()


def test_setup_says_how_to_install_ollama_when_there_is_no_server(ollama):
    """With no server nothing is installed, so everything is still named."""
    ollama["models"] = None
    plan = plan_setup(get_settings())
    assert plan.server_up is False
    assert "ollama" in plan.install_hint.lower()
    assert plan.to_pull == ["ollama/nomic-embed-text", "ollama_chat/llama3.1:8b"]
    assert plan.ready is False


def test_a_setup_that_needs_no_ollama_at_all_is_ready_with_the_server_down(ollama):
    """A cloud-only configuration never asks Ollama for anything, so a down
    server is not its problem — offering to fix it would be a false alarm."""
    ollama["models"] = None
    configure(embed_model="claude-embed", chat_model="claude-chat", allow_cloud=True)
    plan = plan_setup(get_settings())
    assert plan.to_pull == []
    assert plan.ready is True


# --- pulling ----------------------------------------------------------------

def test_pulling_asks_ollama_for_the_bare_model_name(stream):
    pull_model("ollama_chat/llama3.1:8b", lambda p: None)
    assert stream["asked"] == ["llama3.1:8b"]


def test_progress_reports_bytes_across_every_layer(stream):
    stream["events"] = [
        {"status": "pulling manifest"},
        {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 40},
        {"status": "pulling b", "digest": "sha256:b", "total": 300, "completed": 30},
        {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 100},
        {"status": "success"},
    ]
    seen: list[PullProgress] = []
    pull_model("ollama/m", seen.append)

    # A layer that reports again replaces its earlier count, it doesn't add to it.
    assert (seen[-1].completed, seen[-1].total) == (130, 400)
    assert seen[-1].model == "ollama/m"
    assert [p.total for p in seen] == [0, 100, 400, 400, 400]


def test_an_error_from_ollama_stops_the_pull_and_names_the_model(stream):
    stream["events"] = [{"status": "pulling manifest"}, {"error": "file does not exist"}]
    with pytest.raises(SetupError) as e:
        pull_model("ollama/nope", lambda p: None)
    assert "ollama/nope" in str(e.value)
    assert "file does not exist" in str(e.value)


def test_a_stream_that_stops_before_success_is_a_failure(stream):
    """Half a model on disk reported as done is the failure that hides itself."""
    stream["events"] = [
        {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 40},
    ]
    with pytest.raises(SetupError) as e:
        pull_model("ollama/half", lambda p: None)
    assert "ollama/half" in str(e.value)


# --- the request itself -----------------------------------------------------
#
# Everything above stubs `_pull_stream`, so these two are all that stand between
# a typo and a `sift setup` that only fails on a stranger's machine.

def test_an_unreachable_ollama_becomes_a_sentence_not_a_traceback(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(setup.urllib.request, "urlopen", boom)
    with pytest.raises(SetupError, match="connection refused"):
        list(setup._pull_stream("nomic-embed-text"))


def test_the_pull_is_posted_to_the_ollama_pull_endpoint(monkeypatch):
    import json

    sent = {}

    class FakeResponse:
        def __enter__(self):
            return [b'{"status": "success"}\n']

        def __exit__(self, *exc):
            return False

    def capture(request, timeout=None):
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr(setup.urllib.request, "urlopen", capture)

    assert list(setup._pull_stream("nomic-embed-text")) == [{"status": "success"}]
    assert sent["url"] == "http://localhost:11434/api/pull"
    assert sent["body"]["model"] == "nomic-embed-text"
