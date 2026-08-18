"""
Finding FILES, not chunks.

Retrieval returns passages, which is what `ask` needs. But the other half of the
problem — "I know I downloaded it, where is it?" — needs a ranked list of
*files*. This module turns one into the other.

Two signals are combined, and the second one is not a nicety:

  CONTENT.  Group the retrieved chunks by their source file and score each file
    by its BEST chunk, not its average. Averaging punishes long documents, which
    is exactly backwards: one matching page out of a forty-page bank statement
    means the statement is the right file.

  FILENAME.  Score the name itself, lexically. This is required for the tool to
    work at all on a real folder, because a scanned PDF extracts no text and so
    has no chunks and is invisible to embedding search. A file finder that can't
    find `RentalAgreement.pdf` when you type "rental agreement" is broken. The
    manifest records every file it saw — including ones with zero chunks, and
    ones it couldn't index at all (a .zip, a .dmg, a 2GB video) — so all of them
    remain findable by name.

The file's score is the better of the two. Which one fired is reported, because
"matched the filename" and "matched the contents" mean different things to the
person reading the list.
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sift_downloads.config import Settings, get_settings
from sift_downloads.index import Manifest
from sift_downloads.ingest import (
    REASON_DEFERRED,
    REASON_HIDDEN,
    REASON_LOCKED,
    REASON_NO_TEXT,
    REASON_NOT_A_FILE,
    REASON_PARTIAL,
)
from sift_downloads.retrieve import search

log = logging.getLogger(__name__)

# Words too common to say anything about which file you want. Dropped from the
# query before filename matching, so "what is my notice period" matches on
# {notice, period} rather than being diluted to a 40% overlap by "what is my".
_STOPWORDS = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "be", "by", "did", "do", "does",
    "find", "for", "from", "get", "give", "has", "have", "how", "i", "in", "is",
    "it", "its", "many", "me", "much", "my", "of", "on", "or", "our", "show",
    "that", "the", "their", "them", "there", "this", "to", "was", "were", "what",
    "when", "where", "which", "who", "why", "with", "you", "your",
})

# Skip reasons that mean "this file is not worth showing you at all", as opposed
# to "this file exists but its contents couldn't be read".
_UNLISTABLE_REASONS = frozenset({REASON_HIDDEN, REASON_PARTIAL,
                                 REASON_DEFERRED, REASON_NOT_A_FILE})

# Splits a filename into words: on separators, and also at camelCase joins and
# letter/digit boundaries, so "RentalAgreement2024.pdf" -> rental agreement 2024.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")


@dataclass
class FileHit:
    """One file that matched, and enough context to decide if it's the one."""

    path: Path
    score: float
    modified: float                                  # unix mtime
    size: int
    matched_on: str                                  # "content" | "filename" | "both"
    snippet: str = ""                                # best-matching passage
    n_matches: int = 0                               # how many chunks matched
    indexed: bool = True                             # was its text readable?
    note: str = ""                                   # why not, if it wasn't
    duplicates: list[str] = field(default_factory=list)  # identical copies elsewhere

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def age_days(self) -> float:
        return max(0.0, (time.time() - self.modified) / 86400)


# ---------------------------------------------------------------------------
# Filename matching
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    """Break a filename or query into lowercase word tokens."""
    parts = _SEPARATORS.split(_CAMEL_BOUNDARY.sub(" ", text))
    return [p.lower() for p in parts if p]


def filename_score(query_tokens: set[str], filename: str) -> float:
    """How well a filename answers the query, as a 0-1 score.

    The mapping is deliberately conservative. A filename containing EVERY
    meaningful query word is near-certain to be the file you want, so it scores
    0.98 and outranks any content match. A partial overlap lands in the same
    band as an ordinary cosine similarity, so it competes with content matches
    on roughly even terms rather than steamrolling them.
    """
    if not query_tokens:
        return 0.0
    name_tokens = set(tokenize(filename))
    if not name_tokens:
        return 0.0

    overlap = len(query_tokens & name_tokens) / len(query_tokens)
    if overlap >= 0.999:
        return 0.98
    if overlap < 0.5:
        return 0.0
    return 0.40 + 0.5 * overlap


# ---------------------------------------------------------------------------
# The search itself
# ---------------------------------------------------------------------------

def _content_hits(query: str, pool: int, settings: Settings) -> dict[str, dict]:
    """Group chunk hits by source file, keeping each file's best passage.

    Degrades to nothing rather than raising: filename matching needs no model,
    so an unreachable Ollama should narrow the results, not fail the command.
    """
    try:
        chunks = search(query, top_k=pool, min_score=settings.find_min_score, settings=settings)
    except Exception as e:
        log.warning("content search unavailable (%s: %s) — matching on filenames only",
                    type(e).__name__, e)
        return {}

    by_path: dict[str, dict] = {}
    for chunk in chunks:
        entry = by_path.get(chunk["path"])
        if entry is None:
            by_path[chunk["path"]] = {"score": chunk["score"], "snippet": chunk["text"], "n": 1}
        else:
            entry["n"] += 1
            if chunk["score"] > entry["score"]:      # best chunk wins, never the mean
                entry["score"] = chunk["score"]
                entry["snippet"] = chunk["text"]
    return by_path


def _clean_snippet(text: str, limit: int = 220) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit].rstrip() + "…"


def find_files(query: str, limit: int = 10, recent_first: bool = False,
               settings: Settings | None = None) -> list[FileHit]:
    """Rank the files in the source folder against a query."""
    settings = settings or get_settings()
    manifest = Manifest.load(settings=settings)

    # Ask for far more chunks than files wanted: several may land in the same
    # document, and `find` wants coverage where `ask` wants precision.
    content = _content_hits(query, max(limit * 8, 50), settings)

    query_tokens = {t for t in tokenize(query) if t not in _STOPWORDS}

    # Every file the last scan saw is a candidate, including ones whose contents
    # could not be read — those are exactly the files people struggle to find.
    candidates: dict[str, str] = dict.fromkeys(manifest.files, "")
    for path, reason in manifest.skipped.items():
        if reason not in _UNLISTABLE_REASONS:
            candidates[path] = reason

    hits: list[FileHit] = []
    for path_str, skip_reason in candidates.items():
        path = Path(path_str)
        try:
            stat = path.stat()
        except OSError:
            continue  # deleted since the last sync; a re-index will drop it

        entry = content.get(path_str)
        content_score = 0.0
        if entry:
            # A small bonus for matching in several places — enough to break a
            # tie between two files whose best passages score alike, not enough
            # to reorder a clearly better single match.
            content_score = entry["score"] + min(0.03 * (entry["n"] - 1), 0.06)

        name_score = filename_score(query_tokens, path.name)

        if content_score <= 0 and name_score <= 0:
            continue
        if content_score and name_score:
            matched_on = "both"
        elif content_score:
            matched_on = "content"
        else:
            matched_on = "filename"

        recorded = manifest.files.get(path_str, {})
        num_chunks = recorded.get("num_chunks", 0)
        note = skip_reason
        if not note and num_chunks == 0:
            # Use the reason extraction actually gave, rather than guessing.
            # Manifests written before reasons existed fall back to the old
            # assumption, which is right for everything except locked files.
            note = recorded.get("reason") or REASON_NO_TEXT
            if note == REASON_LOCKED:
                note = f'{REASON_LOCKED} — run: sift unlock "{path.name}"'

        hits.append(FileHit(
            path=path,
            score=max(content_score, name_score),
            modified=stat.st_mtime,
            size=stat.st_size,
            matched_on=matched_on,
            snippet=_clean_snippet(entry["snippet"]) if entry else "",
            n_matches=entry["n"] if entry else 0,
            indexed=num_chunks > 0,
            note=note,
            duplicates=manifest.siblings_of(path_str),
        ))

    if recent_first:
        # An explicit flag, never the default: it genuinely helps with "the
        # invoice from last week", and it genuinely distorts relevance, so the
        # user should be the one deciding which they want. Two weeks is the
        # half-life; a file downloaded today gets +0.10, one from a month ago
        # gets almost nothing.
        for hit in hits:
            hit.score += 0.10 * math.exp(-hit.age_days / 14)

    hits.sort(key=lambda h: (-h.score, -h.modified))
    return hits[:limit]
