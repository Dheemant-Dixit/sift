# sift

**You downloaded it. You can't find it. And when you do, it's forty pages.**

sift searches your Downloads folder by meaning, not just by filename, and
answers questions about what's in there. Everything runs on your own machine —
no API keys, no accounts, no documents uploaded.

*Installs as `sift-downloads` (plain `sift` was taken on PyPI). The command you
type is `sift`.*

```
● sift  /Users/you/Downloads

> rental agreement

   1. ScannedRentalAgreement.pdf                    0.98 name
      2.4MB · 3mo ago
      (no extractable text (scanned or image-only?))
   2. lease-notes.md                                0.64 ·
      217B · 7mo ago
      # Lease terms The notice period for terminating this lease is 60…

> ?what is the notice period and the deposit

  Notice period: 60 days [lease-notes.md]
  Security deposit: 100,000 INR, refundable within 30 days [lease-notes.md]

┌──────────────────────────| sift |──────────────────────────┐
│>                                                           │
└────────────────────────────────────────────────────────────┘
```

Type anything to search. Start with `?` to ask a question instead. The prompt
stays at the bottom and results scroll up into your normal terminal history, so
nothing takes over your screen and you keep your scrollback.

---

## Install

```bash
# 1. a local model runner (free, but ~5GB of models to download)
brew install ollama && brew services start ollama     # macOS
# curl -fsSL https://ollama.com/install.sh | sh       # Linux

ollama pull nomic-embed-text     # 274MB — turns text into vectors
ollama pull llama3.1:8b          # 4.9GB — writes the answers

# 2. sift  (the command is `sift`; the package name has a suffix because
#           plain `sift` was already taken on PyPI)
pip install "sift-downloads[watch]"

# 3. read your Downloads folder (a few hundred files takes under a minute)
sift index

# 4. go
sift
```

If anything looks wrong, run `sift doctor`. It checks each piece and prints the
exact command to fix whatever is broken.

**On a small disk or a slow connection?** The 4.9GB one is only used to write
the final answer. Swap it for something smaller — `sift` still finds files just
as well, and answers get a little blunter:

```bash
ollama pull llama3.2:3b
sift ask "..." --chat-model ollama_chat/llama3.2:3b    # or SIFT_CHAT_MODEL
```

The 274MB embedding model is the one that does the searching, and it is not
optional.

---

## Using it

Run `sift` on its own for the interactive session above:

| Type this | What happens |
|---|---|
| `rental agreement` | searches for it |
| `?what is my notice period` | asks a question, answered from your files |
| `/open 2` `/reveal 2` | opens result 2, or shows it in your file manager |
| `/find -r invoice` | searches, preferring recently downloaded files |
| `/sync` `/status` `/help` | update the index, see what's indexed, list commands |
| `ctrl-d` | quit |

Or run single commands without the session:

| Command | What it does |
|---|---|
| `sift find "bank statement"` | ranked list of files; add `--open 1` to open one |
| `sift ask "what's my policy number?"` | one grounded answer with sources |
| `sift index` | update the index (usually under a second) |
| `sift index --rebuild` | start over; needed after changing models |
| | (an upgrade that changes the index format re-embeds itself on the next `sift index`) |
| `sift unlock` | read your password-protected PDFs (asks for each password) |
| `sift status` | what's indexed, and what was skipped and why |
| `sift search "query"` | raw passage scores, for tuning |
| `sift watch` | keep the index updated as the folder changes |
| `sift purge` | delete the index (your files are untouched) |

`find` and `ask` update the index before running, so results are never stale.
Use `--no-sync` to skip that.

### Why it finds files it can't read

A scanned PDF is a picture. There is no text inside to search. Same for a `.zip`
or a `.dmg`.

Those are exactly the files people lose, so sift keeps a record of **every** file
it sees and matches on filenames as well as contents. That is why
`ScannedRentalAgreement.pdf` is the top result above: sift cannot read a word of
it, and still finds it.

Each result shows how it matched — `·` means the contents matched, `name` means
the filename did. Identical copies like `Statement (1).pdf` collapse into one
result that tells you about its twins.

### Password-protected PDFs

Banks send statements locked with a password. sift tells you which files those
are, instead of guessing that they're scanned:

```
$ sift status
2 file(s) — password-protected (findable by name only):
  · AccountStatement_40871876782.pdf
  · lony3005_00000040871876782_E.pdf
  → sift unlock     to read these
```

`sift unlock` asks for each password, reads the file, and adds it to the index.
**The password is never stored** — not in a file, not in your keychain — so
`sift index --rebuild` will ask again. That is deliberate. Two things worth
knowing before you run it:

- Unlocking puts that document's text into the index, which is **not** encrypted.
  A file you locked on purpose becomes readable in `index.npz`.
- Some PDFs are locked only to stop printing or copying, and open with an empty
  password. sift tries that first, so those never reach you as a prompt.

---

## Privacy

Your Downloads folder holds bank statements, ID scans and contracts. So:

- **No document text leaves your machine.** Both models run locally through
  Ollama, so every byte of every file you index is read, embedded and answered
  on `localhost`.
- **Not even a phone-home.** sift talks to models through
  [litellm](https://github.com/BerriAI/litellm), which by default downloads a
  public price list of known models from `raw.githubusercontent.com` when it
  loads. That request carries nothing about you, but it is still a request, so
  sift turns it off (`LITELLM_LOCAL_MODEL_COST_MAP`) and uses the copy shipped
  inside the package. sift does no cost accounting and never reads that list.

  The result is that a default run opens **no connection except to Ollama on
  `localhost`**. Don't take our word for it — `lsof -i`, Little Snitch or
  `tcpdump` will tell you. Please check.
- **The index holds the actual text of your documents.** It lives in your
  system's user-data folder — `sift status` prints the path. Don't commit it or
  share the `.npz`. `sift purge` deletes it. This includes anything you
  `sift unlock` — that text is stored in the clear like everything else.
- **Cloud models need explicit permission.** sift can use Anthropic, OpenAI or
  Gemini through [litellm](https://github.com/BerriAI/litellm), but naming a
  cloud model is not enough. It refuses without `--allow-cloud` and then warns
  you each session. Sending your documents to someone else's server should be a
  decision, not a side effect of editing a config value.

```bash
export ANTHROPIC_API_KEY=sk-...
sift ask "..." --chat-model anthropic/claude-sonnet-4-5 --allow-cloud
```

The two models are separate settings. You can keep embeddings local, so your
whole folder stays home, and use a cloud model only to write the final answer
from the few passages retrieved.

---

## Settings

Nothing needs configuring. Everything can be. A CLI flag beats an environment
variable, which beats `.env`, which beats the default.

| Setting | Flag | Env var | Default |
|---|---|---|---|
| Folder to search | `--source` | `SIFT_SOURCE` | your Downloads folder |
| Where the index lives | `--data-dir` | `SIFT_DATA_DIR` | system user-data folder |
| Embedding model | `--embed-model` | `SIFT_EMBED_MODEL` | `ollama/nomic-embed-text` |
| Answering model | `--chat-model` | `SIFT_CHAT_MODEL` | `ollama_chat/llama3.1:8b` |
| Chunk size / overlap | `--chunk-size` / `--chunk-overlap` | `SIFT_CHUNK_SIZE` / `SIFT_CHUNK_OVERLAP` | 1000 / 150 |
| Indexed unit, max / min | — | `SIFT_CHILD_SIZE` / `SIFT_CHILD_MIN` | 300 / 200 |
| Document opening kept per passage | — | `SIFT_DOC_HEAD_CHARS` | 120 |
| Passages per answer | `--top-k` | `SIFT_TOP_K` | 5 |
| Relevance bar for `ask` | `--min-score` | `SIFT_MIN_SCORE` | 0.55 |
| Candidate bar for `find` | — | `SIFT_FIND_MIN_SCORE` | 0.40 |
| Largest file to read | `--max-file-mb` | `SIFT_MAX_FILE_MB` | 50 |
| Allow cloud models | `--allow-cloud` | `SIFT_ALLOW_CLOUD` | off |

sift is not limited to Downloads — `sift find "..." --source ~/Documents` works
fine. Downloads is just where this problem actually bites.

See [`.env.example`](.env.example).

**A warning about `--min-score`.** The default 0.55 was measured for
`nomic-embed-text` on one particular set of documents. It does not transfer. If
you change embedding models, work out your own — see
[the calibration guide](docs/DESIGN.md#calibrating-the-relevance-bar).

---

## Keeping the index fresh

The index updates incrementally. sift records each file's size and modification
time, so a sync only re-reads what actually changed — about a second, against
forty for a full rebuild. That is why `find` and `ask` can afford to sync before
every query.

If you want it updated in the background, [`contrib/`](contrib/) has ready-made
launchd and systemd files. They are documented, not installed for you. sift
never writes to your system.

---

## What it can't do

- **No OCR.** Scanned PDFs give up no text, so their contents can't be searched.
  They stay findable by filename. (A *locked* PDF is a different problem with a
  real fix — see `sift unlock` above.)
- **No re-ranker.** Ranking is by topic similarity, not by "does this answer the
  question", so a document merely *about* your query can outrank the one that
  answers it. A cross-encoder was built and measured against the worst case in
  this folder; it did not reliably fix it, and the fix turned out to belong at
  ingestion instead. It isn't shipped.
- **Top level only.** sift doesn't walk into subfolders, on purpose — one
  unzipped project would drag in thousands of files.
- **Answers aren't guaranteed correct.** A small local model can still drift past
  its instructions. The one hard rule is that if nothing relevant is found, sift
  refuses without calling the model at all.
- **Grounded is not the same as correct.** The failure worth knowing about isn't
  invention — it's *attribution*. If your documents contain two people's job
  titles, or two people's account numbers, a model can hand you one person's
  real, correctly cited value as the other's. Nothing is fabricated, so a
  "check it against the sources" pass sees nothing wrong. Chunking is set up to
  make this less likely (above), not to make it impossible.

The [design notes](docs/DESIGN.md#limitations) go into why, and what would fix
each one.

---

## How it works

A RAG pipeline built from scratch — no LangChain, no vector database. Text is
split into overlapping chunks, each chunk becomes a vector, and search is one dot
product against a matrix of unit vectors.

**The text sift matches is not the text it reads to you.** One window can't do
both jobs: matching wants it small, so the embedding is *about* one thing;
answering wants it large, so the model can see enough to be right. So each
passage is indexed as a small unit (≈300 characters, cut on line boundaries) and
served as the ~1000-character window around it. A question matches something
precise; the model reads the whole passage it came from.

Two limits on that, both of which exist because of measured failures rather than
taste:

- **An indexed unit has a floor** (`SIFT_CHILD_MIN`, 200). Cut smaller and a
  passage stops being about anything: a payslip row reading only
  `Bank A/C No 12345…` embeds as *an account number*, so a question about
  somebody else's bank account retrieves your payslip. At 200 the same text
  embeds as *a payslip*, and stops matching.
- **Each passage carries its document's opening line** (`SIFT_DOC_HEAD_CHARS`,
  120). Documents name their owner once, at the top, and never again — so a
  clause lifted from page 4 of a form cannot be attributed to anyone. Given a
  tax form containing both the employee's job title and the HR signatory's,
  every model tested answered "what is my designation?" with the *signatory's*
  title. With the opening line attached, they answer correctly.

Both numbers were measured on one folder of real documents. Like `--min-score`,
they are starting points, not constants.

**→ [Design notes](docs/DESIGN.md)** — the pipeline, which file to read first,
why the vector store is shaped the way it is, how to calibrate the relevance
bar, and the full limitations.

## Development

```bash
pip install -e ".[watch,dev]"
pytest            # unit suite: no Ollama, touches no real folder
pytest evals/     # answer quality: needs Ollama, ~40s
```

The unit suite proves the code does what it says. It deliberately cannot reach
a model — real embedding calls raise — so it can't tell you whether the answers
are any good. [`evals/`](evals/) is that second suite: a small synthetic corpus
that reproduces the two wrong-entity failures 0.2.0 fixed, so they can't come
back unnoticed. It isn't run in CI, because it needs a model server.

## License

MIT — see [LICENSE](LICENSE).
