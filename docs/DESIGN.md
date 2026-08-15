# How sift works

This is the long version. The [README](../README.md) tells you how to use sift;
this file explains how it is built and why.

sift is a RAG pipeline written from scratch. No LangChain, no vector database.
Search is one dot product against a matrix of unit vectors. That is not a corner
we cut — it is the point. The whole thing is small enough to read in an
afternoon.

---

## The three paths

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

## Read the code in this order

| File | What it covers |
|---|---|
| `config.py` | Every setting, and why they are read at call time instead of import time |
| `ingest.py` | Getting text out of pdf/docx/md/txt, and surviving a real Downloads folder |
| `chunk.py` | Splitting text with overlap, and the size/overlap tradeoff |
| `store.py` | The vector store: vectors and text kept together, cosine search, atomic saves |
| `index.py` | The incremental sync layer |
| `retrieve.py` | Embedding the query and searching |
| `find.py` | Turning chunk hits into ranked *files*, and why filenames get scored too |
| `generate.py` | Keeping the model grounded in retrieved text, and the refusal guardrail |
| `session.py` | What the interactive session means, with no terminal involved |
| `ui.py` | Drawing it: the inline prompt, streaming answers, result layout |

---

## Settings are read at call time, not import time

The obvious way to write settings is a constant at the top of a module, used as
a default argument:

```python
TOP_K = 5

def search(query, top_k=TOP_K):    # wrong
    ...
```

That reads nicely and it is a trap. Python evaluates the default **once**, when
the module is first imported. After that, nothing can change it. A `--top-k`
flag would run without error and do nothing at all.

So sift has one frozen `Settings` object, built once per process, and every
function that needs a setting asks for it when it is called:

```python
def search(query, top_k=None):     # right
    top_k = get_settings().top_k if top_k is None else top_k
```

Settings are resolved in this order, highest first: a CLI flag, then a `SIFT_*`
environment variable, then `.env`, then the built-in default.

One consequence worth knowing: `configure()` **replaces** the previous
overrides rather than merging into them. The CLI makes exactly one call with
everything the user typed. This was found the hard way — a test called
`configure(max_file_mb=0)`, quietly lost the temp directory it had been given,
and scanned the developer's real Downloads folder instead.

---

## Why the store looks the way it does

The index is **one list of records**. Each record holds its own vector, its own
text, and where it came from.

An earlier version kept two things on disk instead: a matrix of vectors, and a
separate JSON list of text. Row 5 of the matrix went with item 5 of the list.
Nothing enforced that. Any edit that touched one and forgot the other would
return the wrong text for the right vector — no crash, no error message, just a
wrong answer delivered confidently.

Keeping vectors inside their records makes that bug impossible to write. There
is no second structure to fall out of step with. The search matrix still exists,
but it is a cache built from the records, thrown away on every change, and never
treated as the truth.

**Saving is one atomic file.** Vectors, text and provenance are written together
to a temp file, then moved into place with `os.replace`. A crash can leave you
with the old index or the new one, never half of each. Loading uses
`allow_pickle=False`, so an index file can never run code, which matters if you
ever share one.

**The provenance header** records which embedding model built the index.
This one is subtle and worth the paragraph. Vectors made by different models are
not comparable. Compare them anyway and you do not get an error — you get
well-formed numbers that mean nothing. So the header stores the model name, and
loading an index with the wrong one refuses and tells you to rebuild.

See `tests/test_store.py` — the tests are written around exactly these two
failures, because both are silent.

---

## Why `find` scores filenames

`find` scores every file two ways and keeps the better score.

**By content.** Group the matching chunks by file and score each file by its
**best** chunk, not its average. Averaging punishes long documents, which is
backwards: one matching page out of a forty-page bank statement means the
statement is the file you want.

**By filename.** This is not a fallback. It is required for the tool to work at
all.

A scanned PDF is a picture of a document. There is no text in it to extract, so
it produces no chunks, so embedding search cannot see it — it is not ranked low,
it is absent. On the folder this was built against, `RentalAgreement.pdf` is
exactly that file. A tool that cannot find your rental agreement when you type
"rental agreement" is broken.

So sift records **every** file it sees, including ones it could not read and
ones it does not support at all (a `.zip`, a `.dmg`, a video). All of them stay
findable by name. Filenames are split on separators and camelCase, so
`RentalAgreement2024.pdf` becomes `rental agreement 2024`. Common words like
"what" and "my" are dropped, so "what is my rental agreement" matches as well as
"rental agreement" does.

A filename containing every meaningful word of your query scores 0.98 and beats
any content match. A partial match lands in the same range as an ordinary cosine
score, so it competes fairly instead of steamrolling.

---

## Why `ask` refuses without calling the model

Prompts like "only answer from the context" are a request. A small local model
can talk itself out of one.

So the check happens before the model is involved at all. If nothing retrieved
clears the relevance bar, sift returns "I couldn't find that in your documents"
and **never makes the call**. That is not a well-behaved model, it is a
guarantee — the model cannot invent an answer about documents it was never
shown.

Both the streaming and one-shot paths go through the same `generate.prepare()`,
so there is exactly one copy of this rule and the two paths cannot drift apart.

---

## Calibrating the relevance bar

`min_score = 0.55` was **measured**, not chosen. It was measured for
`nomic-embed-text` on one particular set of documents, and it does not transfer.
If you change embedding models, or your documents look very different, work out
your own:

```bash
sift search "a question your documents genuinely answer"
sift search "something completely unrelated"
```

Look at the top score of each and put your bar between the two groups. From the
original run:

| | top-1 score |
|---|---|
| Questions the documents answer | 0.61 – 0.83 |
| Clearly irrelevant questions | 0.47 – 0.63 |

**Those ranges overlap, and that is the honest result.** "What is the capital of
France?" scored 0.63 against a folder containing nothing about France — higher
than several real questions scored. One cosine threshold cannot cleanly separate
relevant from irrelevant. It trims the obvious noise and no more. Fixing it
properly needs a re-ranker.

---

## Limitations

Real ones, not modesty.

- **No OCR.** Scanned and image-only PDFs give up no text, so their contents
  cannot be searched. They stay findable by filename, which is why `find` scores
  filenames.
- **No re-ranker.** Retrieval ranks by *topic similarity*, not by *does this
  answer the question*. On a big folder it shows: "what is my designation?" can
  rank a dozen employment documents above the payslip that states the answer
  outright. A cross-encoder re-ranker scores the question and the passage
  together and fixes exactly this.
- **Grounding is not bulletproof.** A small local model can still drift past the
  "only use the context" instruction. The relevance bar is the part it cannot
  argue with, because it stops the call from happening.
- **Brute-force search.** Every query scans the whole matrix. Fine into the
  hundreds of thousands of chunks, wrong at millions.
- **Top level only.** sift does not walk into subfolders. That is deliberate:
  one unzipped project folder would drag in thousands of files nobody wants.
- **Duplicate detection is size plus the first 8KB**, not a hash of the whole
  file. Good enough to collapse `Statement (1).pdf`. Not something to rely on
  for anything else.

---

## Roadmap

- Conversational follow-ups in the session — "find my lease", then "what's the
  notice period?". The session already records the turns; only the
  prompt-building needs to change.
- A cross-encoder re-ranker as an optional second stage. Highest-value fix on
  this list.
- OCR for scanned PDFs.
- [LanceDB](https://lancedb.com) as a drop-in backend for the store — embedded,
  on-disk, keeps vectors and metadata together behind the same `add`/`search`
  methods.

---

## Development

```bash
pip install -e ".[watch,dev]"
pytest
```

The tests need no Ollama and touch no real folder. They use a fake embedder that
turns text into vectors by hashing, and temp directories throughout, so they run
the same way on your laptop and in CI on Linux, macOS and Windows.

The interactive UI is tested in two halves. `session.py` is pure logic — what a
typed line means, which file `/open 2` refers to — and is tested directly.
Rendering is tested by writing to a `rich` console with a fixed width and
checking the output text, which is how the score-alignment and line-wrapping
bugs are caught. The event loop itself is not tested; it needs a real terminal.
