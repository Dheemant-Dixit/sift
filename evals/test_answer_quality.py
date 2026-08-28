"""Answer-quality regressions, scored against the pre-registered evalset.

Run with:   pytest evals/            (needs Ollama and both default models)

These are slow and model-dependent, which is why they are not in tests/ and not
in CI. They are also the only thing in the repository that can notice sift
answering the wrong person's question, so they are worth the minute they cost.
"""
from __future__ import annotations

import pytest

from evalset import (
    ACCOUNT_NUMBER,
    DESIGNATION_QUERY,
    IDENTIFIER_QUESTIONS,
    LANDLORD_QUERY,
    NEGATIVES,
    OUT_OF_DOMAIN,
    POSITIVES,
    REFUSAL,
    WRONG_ENTITY,
    compile_marker,
    leaked_identifiers,
)

pytestmark = pytest.mark.eval


# --------------------------------------------------------------------------
# The headline regression: the defect 0.2.0 was built to fix.
# --------------------------------------------------------------------------

def test_designation_answers_about_the_employee_not_the_signatory(ask):
    """The wrong-entity case, pinned.

    form16_acme.txt contains two job titles: the employee's, stated once at the
    top, and the HR signatory's, sitting next to the literal word
    "designation" in the verification clause. Before 0.2.0 every model tested
    answered with the signatory's title — a real value, correctly cited, wrong
    person. Nothing is fabricated, so a grounding check sees nothing wrong.

    A GUARD, NOT A PROOF. Unlike the leak test below, this one has *not* been
    shown to fail under pre-0.2.0 settings on this corpus — the trap does not
    reproduce on invented documents, because it depended on the employee's own
    title scoring below the signatory's clause in one real Form 16. Every
    fixture edit that made it discriminate did so by tuning until it broke.
    See evals/README.md. It catches a gross regression; it does not certify
    that attribution is safe.
    """
    marker = compile_marker({q: m for q, _, m in POSITIVES}[DESIGNATION_QUERY])
    replies = [a.text for a in ask(DESIGNATION_QUERY, runs=3)]

    wrong = [r for r in replies if WRONG_ENTITY.search(r)]
    assert not wrong, (
        "answered with the signatory's designation instead of the employee's:\n  "
        + "\n  ".join(wrong))
    missed = [r for r in replies if not marker.search(r)]
    assert not missed, "did not state the employee's designation:\n  " + "\n  ".join(missed)


def test_the_landlord_query_never_reaches_the_payslip(retrieved):
    """The specificity case, pinned at the retrieval layer.

    The landlord's account number is genuinely absent from the corpus. The
    payslip's account number is present, on a short line of its own. With small
    indexed units that line embeds as *an account number* and clears the bar;
    at child_min 200 it embeds as *a payslip* and does not.

    Asserted on the served passage rather than the filename: the payslip may
    legitimately rank for a question about bank accounts, but the *window
    carrying the number* must never reach the model. Verified to discriminate —
    under pre-0.2.0 settings that passage is served at 0.56.

    Retrieval-only, so it needs just the embedding model — and it fails loudly
    if child_size/child_min are lowered without re-measuring.
    """
    admitted = retrieved(LANDLORD_QUERY)
    carriers = [c["filename"] for c in admitted if ACCOUNT_NUMBER in c["text"]]
    assert not carriers, (
        f"a passage carrying the payslip's account number was served for a question "
        f"about someone else's account: {carriers} "
        f"(all admitted: {[(c['filename'], round(c['score'], 3)) for c in admitted]})")


def test_no_owned_identifier_leaks_into_a_negative_answer(ungated):
    """No question that didn't ask for them gets the corpus's own identifiers.

    Run with the lexical gate OFF, deliberately. The gate refuses all three
    negatives on their words — "landlord", "blood", "airspeed" are each absent
    from the corpus — so with it on, the model is never called and this test
    passes without testing anything. What it exists to check is what the model
    does when it IS handed tempting passages, and that is the dense path.
    """
    for question in NEGATIVES:
        for reply in [a.text for a in ungated(question, runs=2)]:
            leaked = leaked_identifiers(reply)
            assert not leaked, f"{question!r} leaked {leaked}:\n  {reply}"


# --------------------------------------------------------------------------
# The word check: what the relevance bar cannot do.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("question", OUT_OF_DOMAIN)
def test_an_out_of_domain_question_is_refused_before_any_model_call(prepared, question):
    """A question about something the corpus has never heard of.

    Every one of these clears the calibrated bar — 0.501 to 0.558 against 0.50 —
    so before the gate they were admitted, the model was called, and the only
    thing standing between the user and an answer was the model's own good
    manners. Refusing them is a guarantee; asking the model nicely is not.

    Needs no chat model: if the gate works, nothing is embedded either.
    """
    chunks, refusal = prepared(question)
    assert refusal is not None, f"{question!r} was admitted, not refused"
    assert not chunks
    assert "appears in none of" in refusal.text or "appear in none of" in refusal.text, \
        f"refused, but not on its words: {refusal.text!r}"


def test_the_word_check_refuses_nothing_the_corpus_can_answer(indexed_corpus):
    """False refusal is the gate's own failure mode. This is the guard on it.

    Identifier questions are included because that is where it would show up
    first: ask for a "UAN" against a document that writes "universal account
    number" out in full and the letters appear nowhere. They are not scored as
    positives — see evalset.py for why — but they must not be refused.
    """
    from sift_downloads.generate import absent_terms
    from sift_downloads.retrieve import get_store

    vocabulary = get_store().vocabulary
    answerable = [q for q, _, _ in POSITIVES] + IDENTIFIER_QUESTIONS
    refused = {q: absent_terms(q, vocabulary) for q in answerable
               if absent_terms(q, vocabulary)}
    assert not refused, f"the word check refused questions the corpus answers: {refused}"


# --------------------------------------------------------------------------
# Breadth: the whole pre-registered set, so a fix for one genre that breaks
# another cannot pass.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("question,genre,pattern",
                         POSITIVES, ids=[q for q, _, _ in POSITIVES])
def test_positive_is_answered(ask, question, genre, pattern):
    marker = compile_marker(pattern)
    replies = [a.text for a in ask(question, runs=2)]
    missed = [r for r in replies if not marker.search(r)]
    assert not missed, (
        f"[{genre}] expected /{pattern}/ in every reply; missing from:\n  "
        + "\n  ".join(missed))


@pytest.mark.parametrize("question", NEGATIVES)
def test_negative_is_refused(ask, question):
    """Refusal is scored positively — a model must SAY it doesn't know.

    Checked as "refused at retrieval, or said so in words", never as "did not
    happen to state a marker", which would pass for any evasive reply.
    """
    for a in ask(question, runs=2):
        assert a.refused or REFUSAL.search(a.text), \
            f"{question!r} should have been refused, got:\n  {a.text}"


# --------------------------------------------------------------------------
# One served passage per slot.
# --------------------------------------------------------------------------

def test_the_runbook_really_is_indexed_twice(indexed_corpus):
    """The precondition for the test below, asserted rather than assumed.

    oncall_runbook.md contains Appendix A twice, the way a real PDF export tool
    duplicates a section. That only produces identical CHUNKS when the repeat is
    long enough for the chunker's parent boundaries to lock onto the same
    paragraph breaks in both copies — `_best_cut` snaps to a blank line in the
    last 20% of its window, so two copies start out of phase and converge. A
    short repeat never converges and this fixture would quietly test nothing.
    """
    import collections

    from sift_downloads.retrieve import get_store

    counts = collections.Counter(r.matched_text for r in get_store().records)
    repeated = [text for text, n in counts.items() if n > 1]
    assert repeated, "the corpus no longer contains a repeated section to collapse"


def test_a_repeated_section_is_served_to_the_model_once(served):
    """The regression. Two copies of one appendix tie exactly — same text, so
    identical vectors — and without collapsing they take two slots and give the
    model nothing it has not already read."""
    chunks = served("how are severity levels assigned?")
    assert chunks, "nothing cleared the bar, so this proves nothing"
    passages = [c["text"] for c in chunks]
    assert len(passages) == len(set(passages)), (
        f"the same passage was served {len(passages) - len(set(passages))} extra time(s)")


def test_collapsing_does_not_cost_a_source(served):
    """Every file that had something to say still says it. The key is
    (path, text), so two files holding the same passage both survive."""
    chunks = served("how are severity levels assigned?")
    assert len({c["filename"] for c in chunks}), "no sources at all"
    for chunk in chunks:
        assert chunk["text"].strip(), "an empty passage took a slot"
