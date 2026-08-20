"""
One rule about documentation: no prose may state how many tests there are.

The count had been written into four files and every one of them was wrong at
the same time. `README.md` had frozen at 0.4.0's figure, `tests/README.md` and
`docs/DESIGN.md` at 0.3.0's, and `evals/README.md` at whatever the eval suite
was three releases earlier. They disagreed with each other as well as with the
suite, and nothing anywhere would have noticed.

That is the shape of the problem rather than an oversight: a number that changes
on most pull requests, is duplicated across four files, and is checked by nothing
will be wrong most of the time. Refreshing the four would buy about one PR. So
the figures were deleted, and this is the guard that keeps them deleted.

A deletion has nothing to revert — putting the sentences back cannot fail a
test — so what is pinned here is the *premise* that made deleting them safe.
Write `604 tests` into a doc and this fails.

CHANGELOG.md is deliberately exempt. Its counts are a frozen record of what a
released version shipped, which is a different claim from "this is the suite
today", and it is the one place the number does not rot.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every doc that describes the project as it is now. CHANGELOG.md is absent on
# purpose; see the module docstring.
DOCS = [
    "README.md",
    "tests/README.md",
    "evals/README.md",
    "contrib/README.md",
    "docs/DESIGN.md",
    "docs/RELEASING.md",
    # Untracked by design (see .git/info/exclude), so this one skips in CI and
    # checks locally. It is also the doc an agent is most likely to edit, and
    # it carried the same stale count as the four tracked ones.
    "CLAUDE.md",
]

# "560 tests", "17 evals", "604 unit tests", "20 answer-quality evals".
# Digits only: "two tests, not one" is prose about a specific pair and never rots.
COUNT = re.compile(r"\b\d+\s+(?:\w+[- ])?(?:tests?|evals?)\b", re.I)


@pytest.mark.parametrize("name", DOCS)
def test_no_document_claims_an_exact_test_count(name):
    path = REPO_ROOT / name
    if not path.exists():          # contrib/ is optional in a sparse checkout
        pytest.skip(f"{name} is not present")
    found = [
        f"{name}:{i}: {line.strip()}"
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if COUNT.search(line)
    ]
    assert not found, (
        "a doc states an exact test count, which nothing checks and every PR "
        "invalidates:\n  " + "\n  ".join(found))


def test_the_changelog_is_exempt_and_still_carries_its_counts():
    """The exemption is load-bearing, not an oversight.

    If CHANGELOG.md ever stops recording what a release shipped, the rule above
    has quietly eaten the one place the number is meant to be frozen.
    """
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert COUNT.search(changelog), \
        "CHANGELOG.md no longer records any release's test count"
