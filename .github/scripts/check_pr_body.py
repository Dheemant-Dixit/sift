"""Check that a pull request body says what this repo needs it to say.

`problems()` is pure — body text and the list of changed paths go in, a list
of human-readable problems comes out. Every rule is therefore testable without
a network; the workflow does the I/O and this file does the deciding.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

VERIFICATION = "verification"

# Paths whose changes mean the body has to explain itself, not just assert it
# was checked. Workflows and docs are excluded: a one-line docs correction is a
# real pull request, and PR #11 was exactly that.
CODE_PREFIXES = ("src/", "tests/")

# CLAUDE.md: run `pytest evals/` after touching any of these. config.py is on
# the list because every retrieval constant lives there. The rule only asks
# that the body *mention* evals — CI cannot verify the claim either way, and a
# required check with no bypass must never be able to trap a legitimate PR.
# "No evals, this changes no retrieval behaviour" is a passing answer.
EVAL_TRIGGERS = ("chunk.py", "store.py", "retrieve.py", "generate.py", "config.py")

_HEADING = re.compile(r"^\s{0,3}#{2,3}\s+(.*?)\s*$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def sections(body: str) -> dict[str, str]:
    """Map each `## heading`, lowercased, to the text underneath it."""
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = heading.group(1).lower()
            found[current] = []
        elif current is not None:
            found[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in found.items()}


def problems(body: str, changed_files: list[str]) -> list[str]:
    """Return every reason this body should not merge. Empty means fine."""
    found = []
    present = sections(body)
    touches_code = any(f.startswith(CODE_PREFIXES) for f in changed_files)

    if _COMMENT.search(body):
        found.append(
            "The body still contains a `<!-- -->` comment from the template. "
            "Delete each one as you answer it."
        )

    if VERIFICATION not in present:
        found.append(
            "No `## Verification` section. Every PR needs one, even if it only "
            "says what there was to run and why nothing was."
        )
    elif not present[VERIFICATION]:
        found.append(
            "`## Verification` is empty. Say how you know this works — test "
            "counts, and whether new tests were verified to discriminate."
        )

    if touches_code:
        # Deliberately not a fixed vocabulary. PRs #10 and #12 name their
        # sections after the finding rather than "The bug"/"The fix", and a
        # rule spelling those out would have rejected both.
        explains = [name for name in present if name != VERIFICATION]
        if not explains:
            found.append(
                "This diff touches src/ or tests/, so the body needs at least "
                "one section besides `## Verification` describing what changed. "
                "Name it after the finding if that reads better."
            )

    if any(f.endswith(EVAL_TRIGGERS) for f in changed_files) and "eval" not in body.lower():
        found.append(
            "This diff touches a module whose behaviour only `pytest evals/` "
            "can check, and CI never runs it. Say in the body what the evals "
            "did — or why they do not apply."
        )

    return found


def main(argv: list[str] | None = None) -> int:
    """Read the body from the environment and the changed paths from a file.

    The body arrives through `PR_BODY` rather than as an argument or an
    interpolated `${{ }}` because it is author-controlled text: pasting it into
    a shell command line is how a pull request runs code on the runner.
    """
    argv = sys.argv[1:] if argv is None else argv
    changed = [
        line.strip()
        for line in pathlib.Path(argv[0]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    found = problems(os.environ.get("PR_BODY", ""), changed)
    for problem in found:
        print(f"- {problem}")
    if found:
        print("\nSee .github/PULL_REQUEST_TEMPLATE.md. Editing the pull request "
              "description re-runs this check.")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
