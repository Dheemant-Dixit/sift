"""
Turning a messy folder into clean {path, filename, text} records.

This is the unglamorous 80% of the work. If the text extracted here is garbage,
every later stage — chunk, embed, retrieve, answer — is garbage too.

A Downloads folder is a specifically hostile input, in ways a curated documents
folder is not, and most of this module is about that:

  - half-finished downloads (.crdownload, .part) that are still being written
  - duplicates: Statement.pdf, Statement (1).pdf, Statement (2).pdf
  - scanned PDFs that contain no extractable text at all
  - password-protected PDFs, which is what most bank statements are
  - the occasional 400MB ISO sitting next to your payslips

Extraction returns one big string per document. Chunking is deliberately NOT
done here — that is chunk.py's job.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import docx  # python-docx
from pypdf import PasswordType, PdfReader

from sift_downloads.config import Settings, get_settings, require_source_dir

log = logging.getLogger(__name__)


class PdfLocked(Exception):
    """A PDF that needs a password sift wasn't given."""


# Why a file isn't in the index. Recorded in the manifest and shown to the
# user, so these strings are part of the interface — and three other modules
# branch on them, which is the real reason none of them is written twice.
# `sift unlock` looks for REASON_LOCKED to decide which files it can help
# with; `scan_source` counts REASON_DEFERRED and `watch.py` re-arms on that
# count; `find.py` hides four of them from results entirely.
#
# Two reasons stay f-strings because they carry a value — the exception name,
# the size limit — and nothing branches on either.
REASON_LOCKED = "password-protected"
REASON_NO_TEXT = "no text layer (scanned or image-only)"
REASON_NOT_A_FILE = "not a file"
REASON_HIDDEN = "hidden file"
REASON_PARTIAL = "download in progress"
REASON_UNSUPPORTED = "unsupported type"
REASON_EMPTY = "empty file"
REASON_DEFERRED = "still being written"

# pypdf narrates every malformed PDF it meets ("invalid pdf header", "EOF marker
# not found") straight to its own logger. On a Downloads folder that is a wall
# of noise about files we already handle and report ourselves, in a friendlier
# way, from load_document().
logging.getLogger("pypdf").setLevel(logging.CRITICAL)

# Suffixes browsers and download managers use for files still in flight.
PARTIAL_SUFFIXES = frozenset({".crdownload", ".part", ".download", ".tmp", ".partial", ".!ut"})

# A file modified within this many seconds is assumed to still be downloading.
# Deliberately shorter than the watcher's debounce so that watch mode doesn't
# skip a file and then never look at it again — see `ScanResult.deferred` and
# how watch.py re-arms on it.
FRESHNESS_GUARD_SECONDS = 2.0

# "Statement (1).pdf" -> "Statement.pdf". Used only to prefer a canonical name
# among byte-identical duplicates; it never decides that two files ARE dupes.
_COPY_SUFFIX_RE = re.compile(r"\s*\((\d+)\)$")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_pdf(path: Path, password: str | None = None) -> str:
    """Pull text out of a PDF, page by page.

    Worth understanding: a PDF stores *visual layout*, not clean text. pypdf
    does well on text-based PDFs and returns nothing at all for scanned/image
    ones (those need OCR, which sift does not do). An empty result here is
    therefore normal and expected, not a bug — see `find.py` for why such files
    are still locatable by name.

    Encryption is handled explicitly rather than by catching pypdf's error,
    because pypdf raises it lazily on page access — far from the cause, and
    indistinguishable there from an ordinary broken file.
    """
    reader = PdfReader(path)
    if reader.is_encrypted:
        # The empty password first: many PDFs are encrypted only to forbid
        # printing or copying, and open with no password at all. Costs one call
        # and spares the user a prompt they don't need.
        if reader.decrypt(password or "") == PasswordType.NOT_DECRYPTED:
            raise PdfLocked(path.name)
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def extract_docx(path: Path) -> str:
    return "\n".join(p.text for p in docx.Document(path).paragraphs)


def extract_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


EXTRACTORS = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".md": extract_text_file,
    ".txt": extract_text_file,
}


def load_document(path: Path, password: str | None = None) -> tuple[dict | None, str]:
    """Extract one file into a record.

    Returns (record, "") on success and (None, reason) on failure, following the
    same shape as is_indexable() below. The reason matters: it is stored in the
    manifest and shown to the user, and it is the difference between telling
    someone their bank statement needs a password and telling them it needs OCR.

    Never raises: one broken PDF in a folder of 300 must not abort the sync.
    """
    extractor = EXTRACTORS.get(path.suffix.lower())
    if extractor is None:
        return None, "unsupported type"
    try:
        # Only PDFs can be locked, so only extract_pdf takes a password.
        raw = extract_pdf(path, password) if extractor is extract_pdf else extractor(path)
        text = raw.strip()
    except PdfLocked:
        log.info("  ! locked  %s: %s", path.name, REASON_LOCKED)
        return None, REASON_LOCKED
    except Exception as e:
        log.info("  ! skipped %s: %s: %s", path.name, type(e).__name__, e)
        return None, f"unreadable ({type(e).__name__})"

    if not text:
        log.info("  ! empty   %s: %s", path.name, REASON_NO_TEXT)
        return None, REASON_NO_TEXT

    return {"path": str(path), "filename": path.name,
            "text": text, "num_chars": len(text)}, ""


# ---------------------------------------------------------------------------
# Scanning the folder
# ---------------------------------------------------------------------------

def file_fingerprint(path: Path) -> dict:
    """A cheap change-detector: size + last-modified time.

    Deliberately does not hash the contents — stat() is instant, so the whole
    folder can be fingerprinted on every sync. A content hash would catch the
    rare 'same size, same mtime, different bytes' edit, at the cost of reading
    every file every time.
    """
    st = path.stat()
    return {"size": st.st_size, "mtime": st.st_mtime}


def content_key(path: Path, head_bytes: int = 8192) -> str:
    """An identity for duplicate detection: size + hash of the first 8KB.

    Not a full content hash — that would mean reading every byte of every file
    on every scan. Size plus a head hash is enough to collapse the
    `Statement (1).pdf` family, which is what this is for, while being far too
    weak to be trusted for anything security-related.
    """
    st = path.stat()
    with path.open("rb") as fh:
        head = fh.read(head_bytes)
    return f"{st.st_size}:{hashlib.sha256(head).hexdigest()[:32]}"


def _canonical_name_rank(path: Path) -> tuple:
    """Sort key picking which of several identical files is the 'real' one.

    Prefers a name with no ' (n)' copy suffix, then the shorter name, then the
    older file. So `Statement.pdf` wins over `Statement (1).pdf`.
    """
    stem = path.stem
    match = _COPY_SUFFIX_RE.search(stem)
    copy_number = int(match.group(1)) if match else 0
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = float("inf")
    return (copy_number, len(path.name), mtime, path.name)


def is_indexable(path: Path, settings: Settings) -> tuple[bool, str]:
    """Should this file be extracted? Returns (yes/no, reason-if-no).

    The reasons are surfaced by `sift status`, so 'why isn't my file in here'
    has an answer that doesn't require reading the source.
    """
    if not path.is_file():
        return False, REASON_NOT_A_FILE
    if path.name.startswith("."):
        return False, REASON_HIDDEN
    suffix = path.suffix.lower()
    if suffix in PARTIAL_SUFFIXES:
        return False, REASON_PARTIAL
    if suffix not in settings.extensions:
        return False, REASON_UNSUPPORTED
    try:
        st = path.stat()
    except OSError as e:
        return False, f"unreadable ({type(e).__name__})"
    if st.st_size == 0:
        return False, REASON_EMPTY
    if st.st_size > settings.max_file_bytes:
        return False, f"larger than {settings.max_file_mb}MB"
    if time.time() - st.st_mtime < FRESHNESS_GUARD_SECONDS:
        return False, REASON_DEFERRED
    return True, ""


@dataclass
class ScanResult:
    """What one look at the source folder found."""

    files: dict[str, dict]        # path -> fingerprint, the files to index
    duplicates: dict[str, str]    # duplicate path -> canonical path it copies
    skipped: dict[str, str]       # path -> human-readable reason
    deferred: int                 # files skipped only because they're too fresh


def scan_source(settings: Settings | None = None) -> ScanResult:
    """Look at the source folder and decide what deserves indexing.

    Non-recursive on purpose. Downloads is flat, and descending into an
    unzipped project folder would drag in thousands of irrelevant files.
    """
    settings = settings or get_settings()
    source = require_source_dir(settings)

    keep: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    deferred = 0
    candidates: list[Path] = []

    for path in sorted(source.iterdir()):
        ok, reason = is_indexable(path, settings)
        if ok:
            candidates.append(path)
        elif reason == REASON_NOT_A_FILE:
            continue  # directories aren't interesting enough to report
        else:
            skipped[str(path)] = reason
            if reason == REASON_DEFERRED:
                deferred += 1

    # Collapse byte-identical copies down to one canonical file. Without this,
    # a search for "statement" returns the same document three times and pushes
    # genuinely different files off the end of the list.
    by_content: dict[str, list[Path]] = {}
    for path in candidates:
        try:
            by_content.setdefault(content_key(path), []).append(path)
        except OSError as e:
            skipped[str(path)] = f"unreadable ({type(e).__name__})"

    duplicates: dict[str, str] = {}
    for group in by_content.values():
        canonical, *copies = sorted(group, key=_canonical_name_rank)
        keep[str(canonical)] = file_fingerprint(canonical)
        for copy in copies:
            duplicates[str(copy)] = str(canonical)

    return ScanResult(files=keep, duplicates=duplicates, skipped=skipped, deferred=deferred)


def iter_source_files(settings: Settings | None = None):
    """Yield the indexable files in the source folder (duplicates collapsed)."""
    for path_str in scan_source(settings).files:
        yield Path(path_str)


def load_documents(settings: Settings | None = None) -> list[dict]:
    """Extract text from every indexable file (skipping failures)."""
    return [doc for path in iter_source_files(settings)
            if (doc := load_document(path)[0])]
