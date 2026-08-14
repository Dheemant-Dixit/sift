"""
Answering a question from the retrieved passages.

Retrieve the best chunks, paste them into a carefully worded prompt, and let the
model compose an answer that cites its sources.

The prompt is doing real work. Three rules turn a confident guesser into a
document-grounded assistant:
  1. answer ONLY from the provided context,
  2. if the context doesn't contain the answer, say so,
  3. cite the source filename for each claim.

And one rule the model doesn't get a vote on: if nothing retrieved clears the
relevance bar, no model call happens at all. A prompt instruction is a request;
this is a guarantee. It is the difference between "usually doesn't make things
up" and "cannot make things up about documents it never saw".
"""
from __future__ import annotations

from dataclasses import dataclass, field

import litellm

from sift.config import Settings, check_cloud_consent, get_settings, warn_if_cloud
from sift.retrieve import search

SYSTEM_PROMPT = """You are a helpful assistant that answers questions strictly \
from the provided context, which comes from the user's own documents.

Rules:
- Use ONLY the information in the CONTEXT. Do not use outside knowledge.
- If the context does not contain the answer, say "I couldn't find that in your \
documents." Do not guess.
- Cite the source filename(s) in [brackets] after the facts you use.
- Be concise."""


@dataclass
class Answer:
    """The result of one question.

    A structure rather than a formatted string, so that the library and the CLI
    can disagree about presentation. `refused` is the interesting field: it
    distinguishes "the model said it didn't know" from "we never asked".
    """

    text: str
    chunks: list[dict] = field(default_factory=list)
    refused: bool = False
    best_near_miss: dict | None = None

    @property
    def sources(self) -> list[str]:
        """De-duplicated source filenames, in the order they were retrieved."""
        seen: list[str] = []
        for chunk in self.chunks:
            if chunk["filename"] not in seen:
                seen.append(chunk["filename"])
        return seen


def build_context_block(chunks: list[dict]) -> str:
    """Format retrieved chunks into a labeled context block.

    Each passage is tagged with its filename so the model has something concrete
    to cite — an untagged wall of text can only produce untraceable claims.
    """
    return "\n\n---\n\n".join(f"[Source: {c['filename']}]\n{c['text']}" for c in chunks)


def prepare(question: str, top_k: int | None = None, min_score: float | None = None,
            settings: Settings | None = None) -> tuple[list[dict], Answer | None]:
    """Retrieve, filter, and apply the guardrail.

    Returns (chunks, refusal). A non-None refusal means STOP — do not call a
    model. Shared by the streaming and non-streaming paths so the guardrail has
    exactly one implementation and can't drift between them.
    """
    settings = settings or get_settings()
    top_k = settings.top_k if top_k is None else top_k
    min_score = settings.min_score if min_score is None else min_score

    retrieved = search(question, top_k=top_k, settings=settings)
    chunks = [c for c in retrieved if c["score"] >= min_score]

    # Nothing relevant means no model call at all. The near-miss is reported so
    # that a bar set too high looks like a bar set too high, rather than like an
    # empty folder.
    if not chunks:
        best = retrieved[0] if retrieved else None
        text = "I couldn't find that in your documents."
        if best:
            text += (f"\n  (nothing cleared the {min_score:.2f} relevance bar; "
                     f"closest was {best['filename']} at {best['score']:.2f})")
        return [], Answer(text=text, chunks=[], refused=True, best_near_miss=best)

    check_cloud_consent(settings)
    warn_if_cloud(settings)
    return chunks, None


def build_messages(question: str, chunks: list[dict]) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",
         "content": f"CONTEXT:\n{build_context_block(chunks)}\n\nQUESTION: {question}"},
    ]


def answer(question: str, top_k: int | None = None, min_score: float | None = None,
           settings: Settings | None = None) -> Answer:
    """Run the full question path: retrieve, filter, ground, generate."""
    settings = settings or get_settings()
    chunks, refusal = prepare(question, top_k, min_score, settings)
    if refusal is not None:
        return refusal

    response = litellm.completion(
        model=settings.chat_model,
        messages=build_messages(question, chunks),
        temperature=0.1,  # low: we want faithful extraction, not creativity
    )
    return Answer(text=response.choices[0].message.content or "", chunks=chunks)


class AnswerStream:
    """A question in progress, for interfaces that want tokens as they arrive.

    Retrieval has already happened by the time you hold one of these, so
    `chunks` and `refusal` are available immediately — the UI can show what it's
    about to read from before a single token exists. Iterating yields text
    deltas and accumulates them; `finish()` returns the completed Answer.
    """

    def __init__(self, question: str, top_k: int | None = None,
                 min_score: float | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.question = question
        self.chunks, self.refusal = prepare(question, top_k, min_score, self.settings)
        self.text = ""

    def __iter__(self):
        if self.refusal is not None:
            return                                  # refused: never call the model
        response = litellm.completion(
            model=self.settings.chat_model,
            messages=build_messages(self.question, self.chunks),
            temperature=0.1,
            stream=True,
        )
        for part in response:
            delta = part.choices[0].delta.content
            if delta:
                self.text += delta
                yield delta

    def finish(self) -> Answer:
        if self.refusal is not None:
            return self.refusal
        return Answer(text=self.text, chunks=self.chunks)
