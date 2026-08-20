"""
Chunking over the eval corpus — the two properties, on real documents.

`test_small_to_big.py` checks the floor on hand-written strings a few lines
long. Those strings were written to demonstrate the mechanic, so they
demonstrate it. The four documents in `evals/corpus` were not: they were
written for PR #3, for two other defects, before this property was a question.
That is the whole reason they are used here — a fixture that predates the test
cannot have been tuned to it.

At 0.4.1 they produce 12 indexed units of 66 below `child_min`, because
`line_blocks` consulted the floor on one of its three exits and the tail flush
was not that one. On the real corpus the same defect reached 28.5%.

Two properties, not one. "Nothing is under the floor" alone is satisfied by
deleting the text that is under the floor, which is worse than the bug.

No model server: chunking does not embed.
"""
from __future__ import annotations

from pathlib import Path

from sift_downloads.chunk import chunk_text, line_blocks
from sift_downloads.config import get_settings

CORPUS = Path(__file__).resolve().parents[1] / "evals" / "corpus"


def _children_by_parent() -> list[tuple[str, list[str]]]:
    """Every (parent, children) pair the real pipeline builds over the corpus."""
    settings = get_settings()
    pairs = []
    for path in sorted(CORPUS.iterdir()):
        if path.suffix not in (".txt", ".md"):
            continue
        for parent in chunk_text(path.read_text(), settings.chunk_size, settings.chunk_overlap):
            pairs.append((parent, line_blocks(parent, settings.child_size, settings.child_min)))
    assert pairs, f"no documents found in {CORPUS}"
    return pairs


def test_no_indexed_unit_is_stranded_below_the_floor():
    """`child_min` is documented as a floor, so it has to hold on real documents.

    A unit below it has lost the context that says what KIND of thing it is,
    which is the whole reason the floor exists — see `chunk.py`.

    One exemption, and only one: a parent holding less than `child_min` of text
    in the first place. Its single child is the whole window, so there is
    nothing to merge it with and no context has been stranded. The exemption
    buys no leniency on this corpus — with it and without it, the same 12 units
    fail before the fix and the same zero fail after, and the same two wrong
    fixes are caught either way. It matters on the real index, where 5 units of
    4977 are whole short parents (before the fix, 1978 were stranded children).
    """
    floor = get_settings().child_min
    stranded = [len(c) for _parent, children in _children_by_parent()
                for c in children if len(c) < floor and len(children) > 1]

    assert not stranded, \
        f"{len(stranded)} indexed units stranded under {floor}: {sorted(stranded)}"


def test_chunking_loses_no_text():
    """The floor must be met by merging, never by dropping.

    Without this, the one-line fix — filter out anything under the floor —
    passes the test above while silently deleting the user's text.
    """
    lost = []
    for parent, children in _children_by_parent():
        have = {line.strip() for child in children for line in child.split("\n") if line.strip()}
        lost += [line.strip() for line in parent.split("\n")
                 if line.strip() and line.strip() not in have]

    assert not lost, f"{len(lost)} lines dropped during chunking"
