"""
Tests for the PR-body check that gates every pull request.

The check is a required status context, which makes its failure modes
asymmetric: a false pass is a template nobody fills in, but a false *fail*
blocks a merge with no bypass and no owner override. So the cases below lean
on real PR bodies from this repo's history rather than on invented ones —
PR #11 in particular, which is four paragraphs of prose with no headings at
all and was a perfectly good pull request.

`problems()` is pure: body text in, list of human-readable problems out. All
the I/O — reading the event payload, asking the API which files changed —
lives in the workflow, so every rule here is testable without a network.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_pr_body.py"
TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"


def _load():
    """`.github` is not an importable package name, so load by path."""
    spec = importlib.util.spec_from_file_location("check_pr_body", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load()


DOCS_ONLY = ["docs/RELEASING.md"]
CODE = ["src/sift_downloads/cli.py"]


# --- the one rule that applies to every PR -----------------------------------

def test_a_body_with_a_filled_verification_section_passes():
    body = "## Verification\n\nDocs only — nothing to run.\n"
    assert check.problems(body, DOCS_ONLY) == []


def test_a_missing_verification_section_is_reported():
    assert check.problems("Fixed it.\n", DOCS_ONLY) != []


def test_an_empty_verification_section_is_reported():
    """A heading with nothing under it is the failure this rule exists for."""
    body = "## Verification\n\n"
    assert check.problems(body, DOCS_ONLY) != []


# --- the unedited template must not merge ------------------------------------

def test_a_leftover_template_comment_is_reported():
    """The guidance lives in HTML comments, so an unfilled template fails on
    its own without needing a separate 'did you edit this' rule."""
    body = "## Verification\n\n<!-- how do you know it works? -->\n"
    assert check.problems(body, DOCS_ONLY) != []


# --- headings scale with what the diff touches -------------------------------

PR_11 = (
    "`lint` joined the required status checks when #10 merged, so "
    "`docs/RELEASING.md` described a gate that no longer exists.\n\n"
    "Docs only — no code, no workflow changes.\n\n"
    "## Verification\n\nGrepped the docs for the old number. Nothing else "
    "in the repo claimed a count.\n"
)


def test_a_real_docs_only_pr_passes_without_bug_or_fix_headings():
    """PR #11 was four paragraphs of prose and a good pull request. A blanket
    three-heading rule would have blocked it, so the rule is conditional."""
    assert check.problems(PR_11, DOCS_ONLY) == []


def test_the_same_body_is_rejected_once_the_diff_touches_code():
    """Same text, different diff — this is the whole point of the condition."""
    assert check.problems(PR_11, CODE) != []


def test_a_code_change_needs_a_section_that_explains_it():
    """Not a fixed vocabulary: PRs #10 and #12 name their sections after what
    they are about, and a rule demanding the literal words "The bug" would
    have rejected both. What is required is that something beyond Verification
    describes the change."""
    body = (
        "## The bug\n\nIt was wrong.\n\n"
        "## The fix\n\nIt is now right.\n\n"
        "## Verification\n\n490 tests, verified by reverting.\n"
    )
    assert check.problems(body, CODE) == []


def test_the_house_style_of_naming_sections_after_the_finding_passes():
    """PR #12's actual shape."""
    body = (
        "## `--top-k 0` pasted the whole index into the prompt\n\nIt did.\n\n"
        "## A negative `chunk_overlap` punched holes in the index\n\nIt did.\n\n"
        "## Verification\n\n490 tests, 17/17 evals.\n"
    )
    assert check.problems(body, CODE) == []


def test_a_code_change_with_only_a_verification_section_is_reported():
    """Verification alone asserts the work was checked without ever saying
    what the work was."""
    body = "## Verification\n\n490 tests pass.\n"
    assert check.problems(body, CODE) != []


# --- the claim CI cannot make for itself -------------------------------------

RETRIEVAL = ["src/sift_downloads/store.py"]


def test_touching_a_retrieval_module_requires_the_body_to_mention_evals():
    """`evals/` is excluded from testpaths and never runs in CI, so the body is
    the only place that result can be recorded. Nothing else in this check is
    about a fact the machine could have established on its own."""
    body = "## The change\n\nFaster.\n\n## Verification\n\n490 tests pass.\n"
    assert check.problems(body, RETRIEVAL) != []


def test_stating_the_evals_result_satisfies_it():
    body = "## The change\n\nFaster.\n\n## Verification\n\n490 tests, 17/17 evals green.\n"
    assert check.problems(body, RETRIEVAL) == []


def test_saying_why_evals_do_not_apply_also_satisfies_it():
    """The rule forces the author to consider evals, not to have run them —
    CI could not verify the claim either way, and a required check with no
    bypass must not be able to trap a legitimate PR."""
    body = (
        "## The change\n\nA docstring typo.\n\n"
        "## Verification\n\nNo evals — this changes no retrieval behaviour.\n"
    )
    assert check.problems(body, RETRIEVAL) == []


def test_an_ordinary_code_change_does_not_trigger_the_evals_rule():
    body = "## The change\n\nA CLI flag.\n\n## Verification\n\n490 tests pass.\n"
    assert check.problems(body, ["src/sift_downloads/cli.py"]) == []


# --- the template and the check must agree -----------------------------------

def test_the_unedited_template_does_not_pass_its_own_check():
    """The guidance is written as HTML comments precisely so that submitting
    the template untouched fails, without needing a separate rule for it."""
    assert check.problems(TEMPLATE.read_text(encoding="utf-8"), CODE) != []


def test_the_template_offers_the_section_the_check_demands():
    """Pins the two halves together: renaming the heading in one place and not
    the other would ship a template that can never pass."""
    assert check.VERIFICATION in check.sections(TEMPLATE.read_text(encoding="utf-8"))


# --- the entry point ---------------------------------------------------------

def test_main_exits_nonzero_and_names_the_problem(tmp_path, monkeypatch, capsys):
    files = tmp_path / "files.txt"
    files.write_text("src/sift_downloads/cli.py\n")
    monkeypatch.setenv("PR_BODY", "nothing here")
    assert check.main([str(files)]) == 1
    assert "Verification" in capsys.readouterr().out


def test_main_exits_zero_on_a_good_body(tmp_path, monkeypatch):
    files = tmp_path / "files.txt"
    files.write_text("docs/RELEASING.md\n")
    monkeypatch.setenv("PR_BODY", "## Verification\n\nDocs only.\n")
    assert check.main([str(files)]) == 0


# --- quoting a comment is not leaving one behind ------------------------------

def test_a_comment_marker_inside_backticks_is_not_a_leftover():
    """PR #13 introduced this check and its own body documented the rule as
    `<!-- -->`, in a table, inside backticks. The check fired on it.

    A required context has no bypass, so a rule that cannot tell prose about a
    comment from an actual comment is worse than no rule."""
    body = (
        "## What changed\n\n| always | no leftover `<!-- -->` comments |\n\n"
        "## Verification\n\n507 tests.\n"
    )
    assert check.problems(body, CODE) == []


def test_a_comment_inside_a_fenced_block_is_not_a_leftover():
    body = (
        "## What changed\n\nThe template reads:\n\n"
        "```markdown\n<!-- delete this as you answer it -->\n```\n\n"
        "## Verification\n\n507 tests.\n"
    )
    assert check.problems(body, CODE) == []


def test_the_evals_claim_still_counts_inside_a_fenced_block():
    """Code is stripped for the comment rule ONLY. Pasting the eval run is the
    most natural way to report it, and stripping fences here would reject the
    best possible answer."""
    body = (
        "## What changed\n\nFaster.\n\n"
        "## Verification\n\n```\n$ pytest evals/\n17 passed\n```\n"
    )
    assert check.problems(body, RETRIEVAL) == []
