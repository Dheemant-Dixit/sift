# sift

**You downloaded it. You can't find it. And when you do, it's forty pages.**

`sift` indexes your Downloads folder on your own machine and answers two
questions about it:

```
$ sift find "rental agreement"

 1. ScannedRentalAgreement.pdf
    2.4MB · 3mo ago · 0.98 name
    /Users/you/Downloads/ScannedRentalAgreement.pdf
    (no extractable text (scanned or image-only?))

 2. lease-notes.md
    217B · 7mo ago · 0.64 ·
    /Users/you/Downloads/lease-notes.md
    "The notice period for terminating the lease is 60 days. The monthly rent is…"
```

```
$ sift ask "what is my notice period?"

The notice period for terminating the lease is 60 days. [lease-notes.md]

— retrieved from —
  • lease-notes.md (chunk 0, score 0.59)
```

Everything runs locally. No API keys, no accounts, nothing uploaded.

---

## Quickstart

```bash
# 1. a local model runner (free, ~2GB of models)
brew install ollama && brew services start ollama     # macOS
# curl -fsSL https://ollama.com/install.sh | sh       # Linux

ollama pull nomic-embed-text     # embeddings
ollama pull llama3.1:8b          # answering

# 2. sift itself
pip install -e ".[watch]"

# 3. index your Downloads (a few hundred files takes under a minute)
sift index

# 4. use it
sift find "tax return"
sift ask "how much was the security deposit?"
```

Something not working? `sift doctor` checks each piece and tells you the exact
command to fix it.

---

## The two commands

### `sift find` — where is it?

Ranks files by **what they contain** and **what they're called**, and can open
the one you wanted:

```bash
sift find "bank statement"
sift find "invoice" --recent          # favour recently downloaded files
sift find "tax return" --open 1       # open result 1
sift find "tax return" --reveal 1     # show it in Finder/Explorer instead
```

Each result shows size, age, score, and how it matched — `·` for contents,
`name` for the filename. Identical copies (`Statement (1).pdf`) collapse into
one result that mentions its siblings.

**Filename matching is not a fallback, it's essential.** A scanned PDF contains
no text at all — nothing to embed, nothing to search. A `.zip` or a `.dmg` can't
be read either. Those are precisely the files people lose. sift keeps a record
of *every* file it has seen, so all of them stay findable by name even when
their contents are unreadable.

### `sift ask` — what does it say?

```bash
sift ask "what's my policy number?"
sift ask                              # interactive
```

Retrieves the most relevant passages and answers **only** from them, citing the
file each fact came from.

If nothing retrieved clears the relevance bar, **no model is called at all** and
you get a plain "I couldn't find that in your documents." A prompt instruction
is a request that a small model can talk itself out of; refusing to call it is a
guarantee.

### Everything else

| Command | |
|---|---|
| `sift index` | sync the index (incremental — usually under a second) |
| `sift index --rebuild` | re-embed everything, needed after changing models |
| `sift status` | what's indexed, what was skipped and why |
| `sift doctor` | check the setup, with the fix for anything broken |
| `sift search "query"` | raw passage scores — for tuning, see [calibration](#calibrating-the-relevance-bar) |
| `sift watch` | re-index continuously as the folder changes |
| `sift purge` | delete the index (your documents are untouched) |

`find` and `ask` sync the index first, so it's never stale. Pass `--no-sync` to
skip that.

---

## Privacy

Your Downloads folder is probably the most sensitive folder on your computer —
bank statements, ID scans, contracts, medical letters. So:

- **Nothing leaves your machine by default.** Both models run locally via
  Ollama. sift makes no network requests other than to `localhost`.
- **The index contains the verbatim text of your documents.** It lives in your
  platform's user-data directory (`sift status` prints the path). Don't commit
  it, don't share the `.npz`. `sift purge` deletes it.
- **Cloud models require explicit consent.** sift can use Anthropic, OpenAI or
  Gemini through [litellm](https://github.com/BerriAI/litellm), but naming a
  cloud model isn't enough — it refuses without `--allow-cloud`, then warns you
  every session. Sending your documents to a third party should be a decision,
  not a side effect of editing a config value.

```bash
# opt in explicitly, if that's what you want:
export ANTHROPIC_API_KEY=sk-...
sift ask "..." --chat-model anthropic/claude-sonnet-4-5 --allow-cloud
```

Note the embedding model and the chat model are separate: you can keep
embeddings local (so your whole corpus stays home) and use a cloud model only
for composing the answer from the few passages retrieved.

---

## Configuration

Nothing needs configuring — but everything can be. Precedence is
**CLI flag → environment variable → `.env` → default**.

| Setting | Flag | Env var | Default |
|---|---|---|---|
| Folder to search | `--source` | `SIFT_SOURCE` | your Downloads folder |
| Index location | `--data-dir` | `SIFT_DATA_DIR` | platform user-data dir |
| Embedding model | `--embed-model` | `SIFT_EMBED_MODEL` | `ollama/nomic-embed-text` |
| Answering model | `--chat-model` | `SIFT_CHAT_MODEL` | `ollama_chat/llama3.1:8b` |
| Chunk size / overlap | `--chunk-size` / `--chunk-overlap` | `SIFT_CHUNK_SIZE` / `SIFT_CHUNK_OVERLAP` | 1000 / 150 |
| Passages per answer | `--top-k` | `SIFT_TOP_K` | 5 |
| Relevance bar (`ask`) | `--min-score` | `SIFT_MIN_SCORE` | 0.55 |
| Candidate bar (`find`) | — | `SIFT_FIND_MIN_SCORE` | 0.40 |
| Max file size | `--max-file-mb` | `SIFT_MAX_FILE_MB` | 50 |
| Allow cloud models | `--allow-cloud` | `SIFT_ALLOW_CLOUD` | off |

sift isn't limited to Downloads — `sift find "..." --source ~/Documents` works
fine. Downloads is just the folder this problem actually bites in.

See [`.env.example`](.env.example).

---

## Keeping it fresh

The index is incremental. A manifest fingerprints every file (size + mtime), so
a sync only re-embeds what actually changed. On a real folder that's the
difference between ~40 seconds and ~1 second, which is why `find` and `ask` can
afford to sync before every query.

For anything more, [`contrib/`](contrib/) has ready-made launchd and systemd
units — documented, not auto-installed. sift never writes to your system.

---

## How it works

```
INDEXING  (sift index)
  Downloads/*  ─►  extract text  ─►  chunk  ─►  embed  ─►  index.npz
                    ingest.py       chunk.py    index.py    store.py

FINDING   (sift find)
  query  ─►  embed  ─►  cosine top-50  ─►  group by file  ─┐
                                                            ├─►  ranked files
         └─►  tokenize  ─►  filename match  ────────────────┘        find.py

ASKING    (sift ask)
  query  ─►  embed  ─►  cosine top-5  ─►  filter by score  ─►  grounded prompt
                                          └─ nothing relevant? refuse, no LLM call
                                                                    generate.py
```

Read the files in this order:

| File | What it's about |
|---|---|
| `config.py` | Every knob, and why it's resolved at call time instead of import time |
| `ingest.py` | Extracting text from pdf/docx/md/txt — and surviving a real Downloads folder |
| `chunk.py` | Splitting with overlap; the size/overlap tradeoff |
| `store.py` | The vector store: co-located vectors + metadata, cosine search, atomic persistence |
| `index.py` | The incremental sync layer |
| `retrieve.py` | Embedding the query and searching |
| `find.py` | Chunk hits → ranked *files*; why filenames are scored too |
| `generate.py` | Grounding the model in retrieved text; the refusal guardrail |

This is a hand-rolled RAG pipeline: no LangChain, no vector database. Search is
one dot product against a normalized matrix. That's not a limitation to work
around, it's the point — the machinery is small enough to read in an afternoon.

### Why the store looks the way it does

The index is a **single source of truth**: one list of records, each owning its
own vector, so vectors and their text *cannot* fall out of alignment. (An
earlier two-parallel-arrays design could silently return the wrong text for the
right vector — a bug with no crash and no error message.) The search matrix is a
derived cache, rebuilt from the records.

Persistence is **one atomic file** — vectors, metadata and provenance written
together via temp-file + `os.replace`, so a crash can't leave them half-updated.
It loads with `allow_pickle=False`, so an index file can never execute code, and
`load()` re-checks both invariants and refuses a corrupt index rather than
serving wrong answers.

The **provenance header** records which embedding model built the index.
Vectors from different models aren't comparable, and comparing them anyway
doesn't produce an error — it produces confident nonsense. So sift refuses, and
tells you to rebuild. See `tests/test_store.py`.

---

## Calibrating the relevance bar

`MIN_SCORE = 0.55` was **measured**, not chosen — and measured for
`nomic-embed-text` on one particular corpus. It is not transferable. If you
change embedding models or your documents look very different, re-derive it:

```bash
sift search "a question your documents genuinely answer"
sift search "something completely unrelated"
```

Note the top-1 scores of each and put the bar between the two clusters. From the
original calibration run:

| | top-1 score |
|---|---|
| Questions the documents answer | 0.61 – 0.83 |
| Clearly irrelevant questions | 0.47 – 0.63 |

**Those ranges overlap, and that's the honest finding.** "What is the capital of
France?" scored 0.63 against a corpus containing nothing of the sort — higher
than several legitimate questions. A single cosine threshold cannot fully
separate relevant from irrelevant; it only trims the obvious noise. Properly
fixing this needs a re-ranker (see below).

---

## Limitations

Real ones, not modesty:

- **No OCR.** Scanned and image-only PDFs yield no text, so their *contents* are
  unsearchable. They remain findable by filename, which is exactly why `find`
  scores filenames.
- **No re-ranker.** Retrieval ranks by *topical similarity*, not
  *does-this-answer-the-question*. On a large corpus this shows: "what is my
  designation?" can rank a dozen employment-related documents above the payslip
  that states the literal answer. A cross-encoder re-ranker re-scores the top
  candidates jointly and fixes precisely this.
- **Grounding isn't bulletproof.** A small local model can still talk past the
  "answer only from context" instruction. The relevance bar is the part that
  can't be talked around, because it prevents the call entirely.
- **Brute-force search.** The whole matrix is scanned per query — fine into the
  hundreds of thousands of chunks, wrong at millions.
- **Non-recursive.** Only the top level of the folder. Deliberate: descending
  into an unzipped project would drag in thousands of irrelevant files.
- **Duplicate detection is size + first 8KB**, not a full content hash. Right
  for collapsing `Statement (1).pdf`; not something to trust for anything else.

## Roadmap

- Cross-encoder re-ranker as an opt-in second stage (the highest-value fix)
- OCR for scanned PDFs
- [LanceDB](https://lancedb.com) as a drop-in store backend — embedded, on-disk,
  co-locates vectors and metadata behind the same `add`/`search` surface

## Development

```bash
pip install -e ".[watch,dev]"
pytest
```

The test suite needs no Ollama and touches no real folder: it uses a
deterministic fake embedder and temp directories throughout.

## License

MIT — see [LICENSE](LICENSE).
