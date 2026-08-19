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
| `ingest.py` | Getting text out of pdf/docx/md/txt, surviving a real Downloads folder, and reporting *why* a file yielded nothing |
| `chunk.py` | Splitting text, and why each passage is cut twice — small to match, large to read |
| `store.py` | The vector store: vectors and text kept together, cosine search, and exactly how far "atomic" goes |
| `index.py` | The incremental sync layer, and the batch that talks to the embedding model |
| `retrieve.py` | Embedding the query and searching |
| `find.py` | Turning chunk hits into ranked *files*, and why filenames get scored too |
| `generate.py` | Keeping the model grounded in retrieved text, and the refusal guardrail |
| `doctor.py` | The setup checks, and why a check that cannot verify something has to say so |
| `watch.py` | Optional background syncing: debouncing, one sync at a time, and shutdown |
| `session.py` | What the interactive session means, with no terminal involved |
| `ui.py` | Drawing it: the inline prompt, streaming answers, result layout |
| `humanize.py` | The formatting both front ends share, so they cannot drift apart |

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

## The text sift matches is not the text it reads to you

One window cannot do both jobs. Matching wants it **small**, so the embedding is
*about* one thing. Answering wants it **large**, so the model can see enough
context to be right. A single 1000-character chunk is a compromise that is bad
at both.

So every passage is cut twice. It is **indexed** as a small unit — `child_size`,
about 300 characters, cut on line boundaries — and **served** as the
~1000-character window around it. A question matches something precise; the
model reads the whole passage it came from. `store.py` keeps both strings on the
record: `index_text` is what was embedded, `text` is what gets sent.

Two limits on that, both from measured failures rather than taste.

**An indexed unit has a floor** (`child_min`, 200). Cut smaller and a passage
stops being about anything. A payslip row reading only `Bank A/C No 12345…`
embeds as *an account number*, so a question about somebody else's bank account
retrieves your payslip and the model answers from it. At 200 the same text
embeds as *a payslip*, and stops matching.

**Each served passage carries its document's opening line** (`doc_head_chars`,
120). Documents name their owner once, at the top, and never again — so a clause
lifted from page 4 cannot be attributed to anyone. Given a tax form holding both
the employee's job title and the HR signatory's, every model tested answered
"what is my designation?" with the *signatory's* title. With the opening line
attached, they answer correctly.

The head goes on the **served** passage only, never on the indexed child. A
120-character header on a ~72-character child is 63% of the embedded text: every
child in a document would collapse toward one vector and intra-document
precision — the entire point of this split — would be destroyed.

`line_blocks` cuts only on line boundaries, so a document extracted as one
unbroken line yields child == parent and gets none of this. That is the right
trade for forms, where cutting mid-line is exactly what separated a value from
its key in the first place, but the benefit is genre-dependent.

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

**Saving the store is atomic. A whole sync is not.** Vectors, text and
provenance are written together to a temp file, then moved into place with
`os.replace`, so a crash leaves you with the whole old index or the whole new
one, never half of each. Loading uses `allow_pickle=False`, so an index file can
never run code, which matters if you ever share one.

That is a claim about **one write**, and a sync makes two: the store, then the
manifest. It was read here as a claim about the whole operation for a while, so
it is worth being exact. Killed mid-sync, sift is left in one of three states,
and none of them is a corrupt index:

| killed during | what is on disk | how it heals |
|---|---|---|
| embedding | nothing new written | the changed files are re-read next sync |
| `store.save()`, between the temp write and the rename | a leftover `index.tmp.npz`, holding document text | overwritten by the next sync that saves |
| between `store.save()` and `manifest.save()` | store updated, manifest one behind | a file with no manifest entry counts as changed |

All three heal, because "no manifest entry" and "changed" are the same thing to
the sync. The middle one is still worth knowing: nothing reports that temp file,
and it is a second copy of your document text sitting in the data directory.
This is why `sift watch` waits for a running sync on Ctrl-C instead of letting
the interpreter drop it — see `watch.py`.

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

## Why "it produced no text" is not a reason

Three files in the folder this was built against produced no chunks. All three
were reported to the user the same way: *"no extractable text (scanned or
image-only?)"*.

Two of them were not scanned. They were password-protected bank statements. The
advice implied by that message — get OCR — would never have worked on them, and
the advice that would have worked was never offered.

The cause was a lossy boundary. `load_document()` returned `dict | None`, so a
locked file and a scanned file both arrived downstream as the same thing: a file
with `num_chunks == 0`. The only fact that survived was *that* nothing came out,
never *why*. With one fact available, the UI had to guess, and it guessed the
more common case.

The fix is to stop discarding the reason:

```python
def load_document(path, password=None) -> tuple[dict | None, str]:
    ...                       # (record, "") or (None, why-not)
```

which matches the `(ok, reason)` shape `is_indexable()` already used in the same
file. The reason goes into the manifest next to `num_chunks`, and `find` and
`status` read it instead of inferring. Manifests written before this existed have
no `reason` key, so both fall back to the old assumption — right for everything
except the case that prompted the change, which is the correct way round.

**Locked PDFs are handled explicitly, not by catching the error.** pypdf raises
`FileNotDecryptedError` lazily, when you touch `.pages` — far from the cause, and
at that point indistinguishable from an ordinary malformed file. So `extract_pdf`
checks `reader.is_encrypted` up front and raises its own `PdfLocked`.

It tries the empty password first. A lot of PDFs are encrypted only to forbid
printing or copying and open with no password at all; that costs one call and
spares the user a prompt they don't need.

**`sift unlock` sits outside the sync path.** Two reasons. A sync that could
block on a prompt is unusable from `sift watch` or a background service. And
unlocking doesn't change the file, so its fingerprint is unchanged and the next
sync correctly sees nothing to do — the text stays put until `--rebuild`. That
re-prompt is the direct cost of never storing the password, which is the
trade this project chose: the index already holds your documents in the clear,
and adding a credential store to it would be a second, worse secret to keep.

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

## Where the privacy gates live

A cloud model needs `--allow-cloud`. Getting that right turned out to be three
separate decisions, and each was got wrong once first.

**The gate sits where the text leaves, not at the CLI.** It used to run in
`preflight()`, which covers every `sift` subcommand and none of the library
entry points — `update_index()` called from Python embedded the whole folder
with a cloud model and never asked. It now runs at the top of `embed_texts`,
the one point both paths cross (documents via `update_index`, the question via
`retrieve.embed_query`), and in `generate.prepare` for the chat model. A
caller-supplied `embedder=` stays ungated on purpose: that is the caller's own
code, and it is what lets the test suite run with no model server.

**A gate asks about the operation; a report asks about the setup.** `Settings`
knows both configured models and cannot know which command is running, so
`uses_cloud()` and `cloud_models()` default to both. That is right for
`sift doctor`, which describes a configuration, and wrong for a gate. Asked the
wide question, `sift index` was refused over a chat model indexing never loads
— so a local embedder plus a cloud chat model, the split this project
recommends, could not index at all. Every gate is now handed the models its
operation will actually call, and `preflight` *requires* the list: "both" is
the bug and "none" would skip the gate silently, so there is no honest default.

**Locality is not a property of the model string.** `huggingface/bge-small` is
local when `HF_API_BASE` points at your own server and a third-party upload
when it does not, and litellm decides which at call time. On a flat list of
"local" prefixes it counted as local, so a folder of documents went to
`router.huggingface.co` with no gate, no warning, and `sift doctor` reporting
"fully local". `model_is_local()` now splits the question in two: prefixes that
are local unconditionally, and prefixes that are local once pointed at a
server. Membership is decided by running the provider with the network blocked
and watching where the request goes — never by reading the name.

The warning follows the gate. It names each cloud model once per process rather
than warning once overall, because a run that embeds with one provider and
answers with another has to mention both; a single latch named whichever came
first and let the second one take the text in silence.

**The one background request, and why it is off.** litellm downloads a public
price list of known models from `raw.githubusercontent.com` when it is imported.
That request carries nothing about you, but it is still a request, and the
README invites people to verify with `lsof -i` that a default run opens no
connection except Ollama on `localhost`. So sift sets
`LITELLM_LOCAL_MODEL_COST_MAP` and uses the copy shipped inside the package.
sift does no cost accounting and never reads that list.

It is set in `sift_downloads/__init__.py` and has to stay there. `import litellm`
sits above the config import inside `index.py`, and a package's `__init__` always
runs first — set anywhere else it is too late, the request goes out, and the only
symptom is that a claim in the README quietly stops being true.

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
  filenames. Password-protected PDFs used to be lumped in here; they are a
  separate problem with a real fix, and `sift unlock` is it.
- **Unlocked text is stored in the clear.** `sift unlock` puts a
  password-protected document's text into `index.npz` alongside everything else.
  The file stays protected on disk; its contents no longer are.
- **No re-ranker.** Retrieval ranks by *topic similarity*, not by *does this
  answer the question*, so a document merely **about** your query can outrank
  the one that answers it. A cross-encoder was built and measured against the
  worst case in this folder. It did not reliably fix it, and the defect it was
  aimed at turned out to be a chunking problem — see [the text sift
  matches](#the-text-sift-matches-is-not-the-text-it-reads-to-you). It is not
  shipped.
- **Ctrl-C is the only interrupt that is handled.** `sift watch` gives a
  running sync 30 seconds to finish on Ctrl-C. A `SIGTERM` — which is what
  `systemctl stop` and `launchctl unload` send — is not caught, so a sync in
  progress is dropped exactly as it used to be. Nothing is corrupted either
  way; see the three states above.
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

- OCR for scanned PDFs. The largest gap left — it is the difference between
  finding a file and reading it.
- Conversational follow-ups in the session — "find my lease", then "what's the
  notice period?". The session already records the turns; only the
  prompt-building needs to change.
- Handle `SIGTERM` the way Ctrl-C is handled, so the grace period applies to
  `sift watch` under launchd and systemd too.
- [LanceDB](https://lancedb.com) as a drop-in backend for the store — embedded,
  on-disk, keeps vectors and metadata together behind the same `add`/`search`
  methods.

**A cross-encoder re-ranker used to be top of this list.** It was built,
measured against the failure it was meant to fix, and did not reliably fix it;
the real cause was in how documents were split. Re-ranking may be worth having
one day. Calling it the highest-value fix was a guess, and it was wrong.

---

## Development

```bash
pip install -e ".[watch,dev]"
pytest            # 560 tests, ~3s
pytest evals/     # 17 tests, ~60s — needs Ollama and both default models
ruff check .
```

Two suites, because they prove different things. `pytest` proves the code does
what it says, and **cannot** tell you whether the answers are any good: an
autouse fixture makes real embedding calls raise, so nothing in `tests/` ever
sees a real vector. That leaves every retrieval constant unprotected by it,
which is what [`evals/`](../evals/) is for — a fixed set of questions scored
against real models over a synthetic corpus. It is deliberately not part of
`pytest` and not in CI: it needs a model server and is not deterministic enough
to gate a merge. Run it after touching anything in the retrieval path.

The unit tests need no Ollama and touch no real folder. They use a fake
embedder that turns text into vectors by hashing, and temp directories
throughout, so they run the same way on your laptop and in CI on Linux, macOS
and Windows.

The interactive UI is tested in two halves. `session.py` is pure logic — what a
typed line means, which file `/open 2` refers to — and is tested directly.
Rendering is tested by writing to a `rich` console with a fixed width and
checking the output text, which is how the score-alignment and line-wrapping
bugs are caught. The event loop itself is not tested; it needs a real terminal.
