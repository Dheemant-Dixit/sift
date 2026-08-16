"""
Tests for answering — and above all for the refusal guarantee.

generate.py makes one promise the model does not get a vote on: if nothing
retrieved clears the relevance bar, no model call happens at all. Its own
docstring calls that "the difference between 'usually doesn't make things up'
and 'cannot make things up about documents it never saw'".

A prompt instruction is a request; that guarantee is the only part of grounding
which is enforced in code, so it is the part worth pinning down. Most of the
tests below therefore install a `litellm.completion` that raises on contact:
they do not check that the refusal text is nice, they check that the model was
never reached.

Nothing here needs Ollama. `search` is replaced at the seam where generate.py
imports it, and completions are faked.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sift_downloads import generate
from sift_downloads.config import ConfigError, configure
from sift_downloads.generate import (Answer, AnswerStream, answer,
                                     build_context_block, build_messages,
                                     prepare)


def chunk(filename="notes.md", text="some text", score=0.9):
    return {"filename": filename, "path": f"/src/{filename}",
            "text": text, "score": score}


@pytest.fixture
def retrieved(monkeypatch):
    """Control exactly what retrieval hands back."""
    box = {"chunks": []}

    def fake_search(question, top_k=None, min_score=None, settings=None):
        return list(box["chunks"])

    monkeypatch.setattr(generate, "search", fake_search)
    return box


@pytest.fixture
def no_model(monkeypatch):
    """Make any model call an error, so 'never called' is testable."""
    def explode(*args, **kwargs):
        raise AssertionError("the model was called when it must not have been")

    monkeypatch.setattr(generate.litellm, "completion", explode)


@pytest.fixture
def spy_model(monkeypatch):
    """Record calls and return a canned completion."""
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content="the answer [notes.md]")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(generate.litellm, "completion", fake_completion)
    return calls


def stream_of(*deltas):
    """Build a fake streaming completion yielding these text deltas."""
    def fake_completion(**kwargs):
        for d in deltas:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=d))])
    return fake_completion


# --- the guarantee: no relevant chunks means no model call -----------------

def test_nothing_retrieved_refuses_without_calling_the_model(retrieved, no_model):
    retrieved["chunks"] = []
    result = answer("what is my blood group?")
    assert result.refused
    assert result.chunks == []


def test_everything_below_the_bar_refuses_without_calling_the_model(retrieved, no_model):
    retrieved["chunks"] = [chunk(score=0.54), chunk(score=0.51)]
    result = answer("what is my blood group?", min_score=0.55)
    assert result.refused


def test_a_chunk_exactly_on_the_bar_is_admitted(retrieved, spy_model):
    retrieved["chunks"] = [chunk(score=0.55)]
    result = answer("q", min_score=0.55)
    assert not result.refused
    assert len(spy_model) == 1


def test_refusal_reports_the_near_miss_so_a_high_bar_looks_like_a_high_bar(retrieved, no_model):
    retrieved["chunks"] = [chunk("payslip.pdf", score=0.54)]
    result = answer("q", min_score=0.55)
    assert result.best_near_miss["filename"] == "payslip.pdf"
    assert "payslip.pdf" in result.text
    assert "0.54" in result.text


def test_refusal_with_an_empty_index_has_no_near_miss(retrieved, no_model):
    retrieved["chunks"] = []
    result = answer("q")
    assert result.best_near_miss is None
    assert "couldn't find" in result.text.lower()


def test_the_refusal_is_not_a_model_answer_that_happens_to_say_no(retrieved, no_model):
    """A refusal must be structurally distinguishable from a model saying 'I don't know'."""
    retrieved["chunks"] = []
    assert answer("q").refused is True


def test_a_model_answer_is_never_marked_refused(retrieved, spy_model):
    retrieved["chunks"] = [chunk(score=0.9)]
    assert answer("q").refused is False


# --- prepare(), the shared guardrail --------------------------------------

def test_prepare_returns_a_refusal_instead_of_chunks_when_nothing_clears(retrieved):
    retrieved["chunks"] = [chunk(score=0.1)]
    chunks, refusal = prepare("q", min_score=0.55)
    assert chunks == []
    assert refusal is not None and refusal.refused


def test_prepare_returns_no_refusal_when_something_clears(retrieved):
    retrieved["chunks"] = [chunk(score=0.9)]
    chunks, refusal = prepare("q", min_score=0.55)
    assert refusal is None
    assert len(chunks) == 1


def test_prepare_filters_out_only_the_chunks_below_the_bar(retrieved):
    retrieved["chunks"] = [chunk("a.md", score=0.9), chunk("b.md", score=0.2),
                           chunk("c.md", score=0.6)]
    chunks, _ = prepare("q", min_score=0.55)
    assert [c["filename"] for c in chunks] == ["a.md", "c.md"]


def test_prepare_falls_back_to_the_configured_bar(retrieved):
    configure(min_score=0.8)
    retrieved["chunks"] = [chunk(score=0.7)]
    chunks, refusal = prepare("q")
    assert refusal is not None, "the settings bar should apply when none is passed"


# --- cloud consent is checked before any document text is sent ------------

def test_a_cloud_model_without_consent_raises_instead_of_sending(retrieved, no_model):
    configure(chat_model="anthropic/claude-sonnet-4-5", allow_cloud=False)
    retrieved["chunks"] = [chunk(score=0.9)]
    with pytest.raises(ConfigError, match="without consent"):
        answer("q")


def test_a_cloud_model_with_consent_is_allowed(retrieved, spy_model):
    configure(chat_model="anthropic/claude-sonnet-4-5", allow_cloud=True)
    retrieved["chunks"] = [chunk(score=0.9)]
    assert not answer("q").refused


def test_consent_is_not_checked_when_the_answer_is_refused_anyway(retrieved, no_model):
    """A refusal sends nothing, so it must not be blocked by a consent error."""
    configure(chat_model="anthropic/claude-sonnet-4-5", allow_cloud=False)
    retrieved["chunks"] = []
    assert answer("q").refused


# --- the prompt the model actually receives -------------------------------

def test_every_passage_is_tagged_with_its_source(retrieved):
    block = build_context_block([chunk("a.pdf", "alpha"), chunk("b.pdf", "beta")])
    assert "[Source: a.pdf]" in block and "alpha" in block
    assert "[Source: b.pdf]" in block and "beta" in block


def test_passages_are_separated_so_they_cannot_read_as_one_document(retrieved):
    block = build_context_block([chunk("a.pdf", "alpha"), chunk("b.pdf", "beta")])
    assert "---" in block


def test_the_system_prompt_forbids_outside_knowledge(retrieved):
    messages = build_messages("q", [chunk()])
    system = messages[0]["content"]
    assert messages[0]["role"] == "system"
    assert "ONLY" in system
    assert "cite" in system.lower()


def test_the_question_and_context_reach_the_model_together(retrieved):
    messages = build_messages("what is my notice period?", [chunk("lease.md", "60 days")])
    user = messages[1]["content"]
    assert "what is my notice period?" in user
    assert "60 days" in user
    assert "lease.md" in user


def test_the_model_is_called_with_a_low_temperature(retrieved, spy_model):
    retrieved["chunks"] = [chunk(score=0.9)]
    answer("q")
    assert spy_model[0]["temperature"] == 0.1


def test_the_model_is_called_with_the_configured_chat_model(retrieved, spy_model):
    configure(chat_model="ollama_chat/something-else")
    retrieved["chunks"] = [chunk(score=0.9)]
    answer("q")
    assert spy_model[0]["model"] == "ollama_chat/something-else"


# --- Answer.sources --------------------------------------------------------

def test_sources_are_deduplicated_but_keep_retrieval_order():
    a = Answer(text="", chunks=[chunk("b.pdf"), chunk("a.pdf"), chunk("b.pdf")])
    assert a.sources == ["b.pdf", "a.pdf"]


def test_sources_of_a_refusal_are_empty():
    assert Answer(text="no", refused=True).sources == []


# --- streaming shares the guardrail, it does not reimplement it -----------

def test_a_refused_stream_never_calls_the_model(retrieved, no_model):
    retrieved["chunks"] = []
    stream = AnswerStream("q")
    assert list(stream) == []
    assert stream.finish().refused


def test_a_stream_exposes_its_chunks_before_any_token_exists(retrieved, monkeypatch):
    retrieved["chunks"] = [chunk("lease.md", score=0.9)]
    monkeypatch.setattr(generate.litellm, "completion", stream_of("never read"))
    stream = AnswerStream("q")
    assert [c["filename"] for c in stream.chunks] == ["lease.md"]
    assert stream.refusal is None


def test_a_stream_accumulates_its_deltas(retrieved, monkeypatch):
    retrieved["chunks"] = [chunk(score=0.9)]
    monkeypatch.setattr(generate.litellm, "completion", stream_of("60 ", "days"))
    stream = AnswerStream("q")
    assert list(stream) == ["60 ", "days"]
    assert stream.finish().text == "60 days"


def test_a_stream_skips_empty_deltas(retrieved, monkeypatch):
    retrieved["chunks"] = [chunk(score=0.9)]
    monkeypatch.setattr(generate.litellm, "completion", stream_of("a", None, "b"))
    assert list(AnswerStream("q")) == ["a", "b"]


def test_streaming_and_non_streaming_refuse_on_exactly_the_same_input(retrieved, no_model):
    """One guardrail, two callers — they must not drift apart."""
    retrieved["chunks"] = [chunk(score=0.54)]
    configure(min_score=0.55)
    assert answer("q").refused == AnswerStream("q").finish().refused is True
