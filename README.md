# sift

**You downloaded it. You can't find it. And when you do, it's forty pages.**

sift searches your Downloads folder by meaning, not just by filename, and
answers questions about what's in there. Everything runs on your own machine —
no API keys, no accounts, no documents uploaded.

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

**1. A local model runner.** sift talks to [Ollama](https://ollama.com) over
`localhost`. It is free, and it is the only thing you install yourself.

```bash
brew install ollama && brew services start ollama    # macOS
curl -fsSL https://ollama.com/install.sh | sh        # Linux
```

```powershell
irm https://ollama.com/install.ps1 | iex             # Windows
```

Windows also has a plain installer at
[ollama.com/download](https://ollama.com/download), if you prefer one.

**2. sift.**

```bash
pip install "sift-downloads[watch]"
sift setup      # downloads the two models it needs (~5GB, once)
sift            # go
```

`sift setup` names what it is fetching before it starts, and Ctrl-C is safe —
Ollama resumes where it stopped. The models it downloads belong to Ollama; the
only thing sift itself writes is its own index. If anything looks wrong,
`sift doctor` checks each piece and prints the command that fixes it.

**Short on disk?** The 4.9GB model only writes the final answer. `sift setup
--chat-model ollama_chat/llama3.2:3b` pulls a 2GB one instead — answers get a
little blunter, searching is unaffected. The 274MB embedding model is what does
the searching, and it is not optional.

**All three platforms are supported and tested on every change.** sift finds
your real Downloads folder on each — including when OneDrive has moved it, and
when your desktop calls it `Téléchargements`.

*Installs as `sift-downloads` because plain `sift` was taken on PyPI. The
command you type is `sift`.*

---

## Using it

Run `sift` on its own. That is the whole tool.

| Type this | What happens |
|---|---|
| `rental agreement` | searches for it |
| `?what is my notice period` | asks a question, answered from your files |
| `/open 2` `/reveal 2` | opens result 2, or shows it in your file manager |
| `/find -r invoice` | searches, preferring recently downloaded files |
| `/sync` `/status` `/help` | update the index, see what's indexed, list commands |
| `ctrl-d` | quit |

The index updates itself when the session starts and whenever you `/sync`, so
what you downloaded five minutes ago is already searchable.

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
`sift index --rebuild` asks again. That is deliberate. So is telling you this:
unlocking copies that document's text into the index, which is **not**
encrypted, so a file you locked on purpose becomes readable in `index.npz`.

Some PDFs are locked only to stop printing, and open with an empty password.
sift tries that first, so those never reach you as a prompt.

### Also scriptable

Everything the session does is also a command, for pipes, cron and scripts.

| Command | What it does |
|---|---|
| `sift find "bank statement"` | ranked list of files; add `--open 1` to open one |
| `sift ask "what's my policy number?"` | one grounded answer with sources |
| `sift setup` | download the models sift needs; `--yes` to skip the prompt |
| `sift index` | update the index (usually under a second) |
| `sift index --rebuild` | start over; needed after changing models |
| `sift unlock` | read your password-protected PDFs (asks for each password) |
| `sift status` | what's indexed, and what was skipped and why |
| `sift search "query"` | raw passage scores, for tuning |
| `sift watch` | keep the index updated as the folder changes |
| `sift doctor` | check the setup and say how to fix it |
| `sift purge` | delete the index (your files are untouched) |

`find` and `ask` update the index before running, so results are never stale.
Use `--no-sync` to skip that. An upgrade that changes the index format
re-embeds itself on the next run.

`sift watch` keeps the index fresh while you work: it re-syncs a few seconds
after the folder goes quiet, and runs one sync at a time. Ctrl-C lets a running
sync finish — up to 30 seconds — rather than throwing the work away. For a
background service, [`contrib/`](contrib/) has ready-made launchd and systemd
files. They are documented, not installed for you.

---

## Privacy

Your Downloads folder holds bank statements, ID scans and contracts. So:

- **No document text leaves your machine.** Both models run locally through
  Ollama, so every byte of every file you index is read, embedded and answered
  on `localhost`.
- **Not even a phone-home.** A default run opens **no connection except to
  Ollama on `localhost`** — sift turns off the one background request its model
  library would otherwise make. Don't take our word for it: `lsof -i`, Little
  Snitch or `tcpdump` will tell you. Please check.
  ([how that is done](docs/DESIGN.md#where-the-privacy-gates-live))
- **The index holds the actual text of your documents.** It lives in your
  system's user-data folder — `sift status` prints the path. Don't commit it or
  share the `.npz`. `sift purge` deletes it. This includes anything you
  `sift unlock` — that text is stored in the clear like everything else.
- **Cloud models need explicit permission.** sift can use Anthropic, OpenAI or
  Gemini through [litellm](https://github.com/BerriAI/litellm), but naming a
  cloud model is not enough. It refuses without `--allow-cloud`, and once you
  have allowed it, every cloud model names itself before it is used. Sending
  your documents to someone else's server should be a decision, not a side
  effect of editing a config value.

```bash
export ANTHROPIC_API_KEY=sk-...
sift ask "..." --chat-model anthropic/claude-sonnet-4-5 --allow-cloud
```

The two models are separate settings. You can keep embeddings local, so your
whole folder stays home, and use a cloud model only to write the final answer
from the few passages retrieved.

**Permission is asked per command, for the models that command actually calls.**
`sift index` and `sift find` only ever use the embedding model, so a local
embedder plus a cloud chat model indexes and searches with no `--allow-cloud` at
all; `sift ask` is where the question reaches the cloud model, so that is where
it asks. The warning follows the same rule and names each cloud model once, so a
run that embeds with one provider and answers with another tells you about both.

**Running a model server yourself?** `ollama/` and `lm_studio/` always count as
local. `huggingface/` does not, because litellm sends it to
`router.huggingface.co` unless you point it at your own endpoint — see
[`.env.example`](.env.example) for the variable that fixes that.

---

## What it can't do

- **No OCR.** Scanned PDFs give up no text, so their contents can't be searched.
  They stay findable by filename. (A *locked* PDF is a different problem with a
  real fix — see `sift unlock` above.)
- **No re-ranker.** Ranking is by topic similarity, not by "does this answer the
  question", so a document merely *about* your query can outrank the one that
  answers it. A cross-encoder was built, measured, and not shipped: it did not
  reliably fix the worst case, and the real fix belonged at ingestion.
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
  make this less likely, not to make it impossible.

The [design notes](docs/DESIGN.md#limitations) go into why, and what would fix
each one.

---

## Settings

Nothing needs configuring. Everything can be. A CLI flag beats an environment
variable, which beats `.env`, which beats the default.

- `sift <command> --help` lists the flags.
- [`.env.example`](.env.example) is the full list of environment variables, with
  what each one is for.

sift is not limited to Downloads — `sift find "..." --source ~/Documents` works
fine. Downloads is just where this problem actually bites.

**A warning about `--min-score`.** The default 0.55 was measured for
`nomic-embed-text` on one particular set of documents. It does not transfer. If
you change embedding models, work out your own — see
[the calibration guide](docs/DESIGN.md#calibrating-the-relevance-bar).

---

## How it works

A RAG pipeline built from scratch — no LangChain, no vector database. Text is
split into overlapping chunks, each chunk becomes a vector, and search is one dot
product against a matrix of unit vectors.

**The text sift matches is not the text it reads to you.** One window can't do
both jobs: matching wants it small, so the embedding is *about* one thing;
answering wants it large, so the model can see enough to be right. Each passage
is indexed as a small unit and served as the wider window around it. Both sizes,
and the floor under them, were set by measuring real failures — a passage cut too
small stops being *about* anything, and a clause lifted from page 4 of a form
can't be attributed to anyone unless it carries the document's opening line.

**→ [Design notes](docs/DESIGN.md)** — the pipeline, which file to read first,
why the vector store is shaped the way it is, how to calibrate the relevance
bar, where the privacy gates live, and the full limitations.

## Development

```bash
pip install -e ".[watch,dev]"
pytest            # unit suite: ~3s. No Ollama, touches no real folder
ruff check .      # both of the above are required CI checks
pytest evals/     # answer quality: ~60s. Needs Ollama, never runs in CI
```

The unit suite proves the code does what it says. It deliberately cannot reach
a model — real embedding calls raise — so it cannot tell you whether the answers
are any good. [`evals/`](evals/) is that second suite: a synthetic corpus and a
fixed set of questions, scored against real models. It is honest about its own
limits — one of its two headline tests is labelled a guard rather than a proof,
because it does not fail when you revert the fix it guards.
[`evals/README.md`](evals/README.md) says which is which, and why that matters.

Releases go out by pushing a `vX.Y.Z` tag — the workflow checks the tag against
the packaged version, the changelog and `main` before it publishes anything, and
uploads to PyPI without an API token existing anywhere. See
[docs/RELEASING.md](docs/RELEASING.md).

## License

MIT — see [LICENSE](LICENSE).
