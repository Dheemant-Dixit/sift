"""
Splitting a document into units that are matched and served.

Each chunk becomes one unit that gets embedded and retrieved independently, so
the size is a real tradeoff: too small and a chunk loses the context that makes
it meaningful, too large and its embedding averages out into mush that matches
everything weakly and nothing strongly.

Cuts are *snapped* to a nearby natural boundary (paragraph > line > sentence >
word) so a chunk rarely ends mid-sentence. This is a readable cousin of
LangChain's RecursiveCharacterTextSplitter — enough to see the mechanic without
the machinery.

SMALL-TO-BIG: THE TEXT WE MATCH IS NOT THE TEXT WE SERVE
--------------------------------------------------------
One window has to satisfy two opposed jobs. Matching wants it small, so the
embedding is *about* one thing. Answering wants it large, so the model can see
enough to be right. Serving one size for both means losing one of them.

So each record carries two texts: a small `index_text` (what gets embedded) and
the surrounding `text` (what the model is handed). A question matches a precise
child; the model reads the whole parent it came from.

`child_min` is the knob that stops this from leaking. It is a floor on how small
an indexed unit may get, and it exists because of a measured failure: at a floor
of 66 characters a payslip produced a child reading only `Bank A/C No <digits>`.
Stripped of its document, that embeds as *an account number* — so "what is the
landlord's bank account number?" retrieved the user's payslips and offered their
contents. At a floor of 200 the same text embeds as *a payslip*, which is not
near that question at all, and the payslips stop being retrieved for it.

The rule that generalises: an indexed unit must stay large enough to still say
what KIND of thing it is. Precision below that point is precision about nothing.
"""
from __future__ import annotations

from sift_downloads.config import Settings, get_settings


def _best_cut(text: str, start: int, hard_end: int) -> int:
    """Find a good place to end a chunk at or before `hard_end`.

    Only the last ~20% of the window is searched, so snapping can tidy up an
    ending without drastically shortening the chunk. With no boundary found, it
    cuts at hard_end.
    """
    if hard_end >= len(text):
        return len(text)

    window = text[start:hard_end]
    search_from = int(len(window) * 0.8)

    for marker in ("\n\n", "\n", ". ", " "):
        idx = window.rfind(marker, search_from)
        if idx != -1:
            # +len(marker) keeps the boundary characters with the current chunk.
            return start + idx + len(marker)

    return hard_end


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Split one document's text into overlapping chunks.

    `size` and `overlap` default to the active settings rather than being bound
    at import time, which is what lets --chunk-size actually take effect.
    """
    settings = get_settings()
    size = settings.chunk_size if size is None else size
    overlap = settings.chunk_overlap if overlap is None else overlap

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = _best_cut(text, start, start + size)

        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)

        if end >= len(text):
            break
        # Step back by `overlap` so adjacent chunks share a margin — a sentence
        # split across a boundary still appears whole in one of them. The
        # max(..., start + 1) is a liveness guard: without it, an overlap as
        # large as the chunk would leave `start` unchanged and loop forever.
        start = max(end - overlap, start + 1)

    return chunks


def line_blocks(text: str, max_chars: int, min_chars: int | None = None) -> list[str]:
    """Merge whole lines into blocks, preferring blank-line boundaries.

    Cuts never land mid-line, so a key-value record is never split from its
    value — which is the failure mode that matters on forms.

    A blank line is a PREFERRED break, not a hard one: it only ends a block once
    the block has reached `min_chars`. That distinction is the whole ballgame on
    badly-extracted PDFs, where a blank line between every visual row would
    otherwise yield one word per block.
    """
    if min_chars is None:
        min_chars = max_chars // 3

    blocks: list[str] = []
    current: list[str] = []
    size = 0

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            if size >= min_chars:            # preferred break, but only once big enough
                blocks.append("\n".join(current))
                current, size = [], 0
            continue
        if size and size + len(stripped) + 1 > max_chars:
            blocks.append("\n".join(current))
            current, size = [], 0
        current.append(stripped)
        size += len(stripped) + 1

    if size:
        blocks.append("\n".join(current))
    return [b for b in blocks if b.strip()]


def document_head(text: str, limit: int) -> str:
    """The opening of a document, whitespace-collapsed, as a provenance line.

    Documents name themselves once, at the top — `Employee Name …`,
    `Student Name : …`, the name line of a resume — and then never again.
    Chunking discards that for every chunk after the first, which is what lets a
    passage like "I, <NAME>, in the capacity of <TITLE> (designation) …" be read
    as the *reader's* title rather than the signatory's. Carrying the opening
    alongside each passage restores the one fact needed to tell those apart.

    `limit` is deliberately a setting and deliberately small. Too short and the
    owner's name falls off the end; too long and a payslip's head reaches its
    account-number line, which would copy an identifier onto every chunk of that
    file. It was measured on a real corpus, and it should be re-measured rather
    than trusted on a different one.
    """
    if limit <= 0:
        return ""
    return " ".join(text.split())[:limit]


def chunk_one(doc: dict, settings: Settings | None = None) -> list[dict]:
    """Chunk a single document into records carrying their provenance.

    Every record keeps `path` (so the indexer can drop a file's chunks when it
    changes or disappears) and `filename` (so an answer can cite its source),
    plus the two texts small-to-big needs:

      index_text — the small unit that gets embedded and matched
      text       — the surrounding window the model is actually handed

    With `child_size = 0` the two are identical and this degrades to plain fixed
    windows, which is the escape hatch if a corpus is better served that way.
    """
    settings = settings or get_settings()
    parents = chunk_text(doc["text"], settings.chunk_size, settings.chunk_overlap)
    head = document_head(doc["text"], settings.doc_head_chars)

    records = []
    for parent in parents:
        if settings.child_size:
            children = line_blocks(parent, settings.child_size, settings.child_min)
        else:
            children = [parent]
        for child in children:
            records.append({
                "filename": doc["filename"],
                "path": doc["path"],
                "chunk_index": len(records),
                "text": parent,
                "index_text": child,
                "doc_head": head,
            })
    return records


def chunk_documents(documents: list[dict], settings: Settings | None = None) -> list[dict]:
    """Flatten a list of documents into a single list of chunk records."""
    return [rec for doc in documents for rec in chunk_one(doc, settings)]
