# sift

**You downloaded it. You can't find it. And when you do, it's forty pages.**

sift searches your Downloads folder by meaning, not just by filename, and
answers questions about what's in there. Everything runs on your own machine —
no API keys, no accounts, nothing uploaded.

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
# 1. a local model runner (free, ~2GB of models)
brew install ollama && brew services start ollama     # macOS
# curl -fsSL https://ollama.com/install.sh | sh       # Linux

ollama pull nomic-embed-text     # turns text into vectors
ollama pull llama3.1:8b          # writes the answers

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

- **Nothing leaves your machine.** Both models run locally through Ollama. The
  only network traffic is to `localhost`.
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
  question". Ask "what is my designation?" and a dozen employment documents can
  outrank the payslip that says it outright.
- **Top level only.** sift doesn't walk into subfolders, on purpose — one
  unzipped project would drag in thousands of files.
- **Answers aren't guaranteed correct.** A small local model can still drift past
  its instructions. The one hard rule is that if nothing relevant is found, sift
  refuses without calling the model at all.

The [design notes](docs/DESIGN.md#limitations) go into why, and what would fix
each one.

---

## How it works

A RAG pipeline built from scratch — no LangChain, no vector database. Text is
split into overlapping chunks, each chunk becomes a vector, and search is one dot
product against a matrix of unit vectors.

**→ [Design notes](docs/DESIGN.md)** — the pipeline, which file to read first,
why the vector store is shaped the way it is, how to calibrate the relevance
bar, and the full limitations.

## Development

```bash
pip install -e ".[watch,dev]"
pytest
```

The tests need no Ollama and touch no real folder.

## License

MIT — see [LICENSE](LICENSE).
