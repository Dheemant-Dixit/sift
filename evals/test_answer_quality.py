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
    LANDLORD_QUERY,
    NEGATIVES,
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
    marker = compile_marker(dict((q, m) for q, _, m in POSITIVES)[DESIGNATION_QUERY])
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


def test_no_owned_identifier_leaks_into_a_negative_answer(ask):
    """No question that didn't ask for them gets the corpus's own identifiers."""
    for question in NEGATIVES:
        for reply in [a.text for a in ask(question, runs=2)]:
            leaked = leaked_identifiers(reply)
            assert not leaked, f"{question!r} leaked {leaked}:\n  {reply}"


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
