# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

## 0.4.0 — 2026-08-19

**Getting started stopped being a four-tool errand, and Windows users can now
tell that sift supports Windows.**

Work on sift has come from two places: reading the code, and watching it get an
answer wrong. This release came from a third — watching what a stranger has to
do before sift does anything at all, which turned out to be: install a model
runner, look up two model names, pull them by hand, then find out whether it
worked.

**No rebuild.** The index format is unchanged from 0.2.0, so upgrading costs
nothing.

### `sift setup`

One command downloads what sift needs.

```
$ sift setup
sift needs these models:
  ollama/nomic-embed-text
  ollama_chat/llama3.1:8b

They come from ollama.com and are stored by Ollama, not by sift.
Sizes are shown as they download. Ctrl-C is safe — Ollama resumes where it stopped.

Download them now? [y/N]
```

It reads the same two facts `sift doctor` reads — is Ollama running, does it
have the configured models — and acts on them. Three lines are drawn on purpose:

- **It pulls models. It never installs Ollama.** Pulling into a server you
  already chose to run is reversible and stays inside Ollama's store.
  `brew install` and `curl | sh` write to your machine, and sift does not do
  that to you. With no server, setup prints the install command for your
  platform and stops.
- **It only touches models Ollama can answer for.** An LM Studio or cloud model
  is named and skipped rather than passed over quietly.
- **No download size is hardcoded.** A "5.2GB" in the prompt would go stale the
  next time a default model changes. Setup names the models and shows real byte
  totals as they arrive.

`--yes` skips the prompt for scripts. Without a terminal and without `--yes` it
refuses rather than prompting into the void.

### A bare `sift` offers to set itself up

Starting the session with nothing pulled used to end in a wall of litellm
connection errors — the exact failure `sift doctor` exists to prevent, on the
path a new user is most likely to take. The session now offers to fetch what it
needs before it tries to use it, and a decline, a failed download or a Ctrl-C
all leave a working session behind: without models, sift can still find files by
name.

### Windows was always supported. Now the README says so.

sift resolves your real Downloads folder on Windows through
`SHGetKnownFolderPath`, specifically because OneDrive moves it somewhere
`%USERPROFILE%\Downloads` will not find, and it honours `LOCALAPPDATA` for the
index. Windows has run in CI on every pull request since the beginning. The
README opened with `brew install ollama` and did not contain the word "Windows".

The install block now covers macOS, Linux and Windows, and is three lines on
each. The same fix reaches anyone whose desktop calls the folder
`Téléchargements`.

### Smaller things

- `sift doctor` now says `sift setup` for a missing model instead of one
  `ollama pull` line per model.
- Download progress redraws in place on a terminal and prints one line per step
  when it is not on one, so `sift setup --yes` in a script produces a log rather
  than one enormous line.

### Docs

The README leads with the interactive session, because that is the product; the
one-shot commands follow as what they are, the same features for pipes and cron.
The settings table moved to [`.env.example`](.env.example), which already
documented every variable in more detail, and the litellm price-list mechanism
moved to [`docs/DESIGN.md`](docs/DESIGN.md) — the privacy promise it supports
stays in the README, `lsof -i` invitation included.

591 unit tests and 17 answer-quality evals, on Linux, macOS and Windows across
Python 3.10 to 3.14.

## 0.3.0 — 2026-08-19

**A privacy leak, and the rest of what a line-by-line audit turned up.**

Every finding below was reproduced against a real folder before anything was
changed. Almost all of them produced a wrong result rather than an error, which
is why they lasted this long.

**No rebuild this time.** The index format is unchanged from 0.2.0, so
upgrading costs nothing.

### Your documents could go to Hugging Face without being asked

If you configured a `huggingface/` model, sift counted it as local. It is not.
litellm sends `huggingface/` to `router.huggingface.co` unless it is given a
base URL, and sift gave it none — so the text of every document in the folder
was uploaded with **no `--allow-cloud` gate, no warning, and `sift doctor`
reporting "fully local — no document text leaves this machine"**.

Locality is not a property of a model name. `huggingface/bge-small` is local
when `HF_API_BASE` points at your own server and a third-party upload when it
does not, and litellm decides which at call time. sift now asks that question
per model instead of matching a prefix. If you run Hugging Face against your own
server, set `HF_API_BASE` (or `HUGGINGFACE_API_BASE`) and sift counts it as
local again — that is a behaviour change for you, and closing the leak is what
forced it.

The other four prefixes on that list were re-checked the same way — by running
each one with the network blocked and watching where the request went.
`ollama/`, `ollama_chat/`, `lm_studio/` and `local/` all really do stay on your
machine.

### Permission is asked per command, and asked everywhere

**The gate moved to where the text leaves.** It used to run in the CLI's
preflight, which covered every `sift` subcommand and none of the library entry
points — `update_index()` called from Python embedded the entire folder with a
cloud model and never asked.

**`sift index` no longer asks about a model it never loads.** The gate asked
"is this configuration cloudy?" rather than "will this operation send text
anywhere?", so a local embedder plus a cloud chat model — the split the README
recommends — could not index at all. It was refused twice over: once for
consent, and once for a chat model that had not been pulled, whose suggested
fix was a multi-gigabyte download indexing never needs. Seven commands were
affected. `sift ask` is still refused without `--allow-cloud`, because that is
the one that puts your question to the cloud model.

**Indexing says so now.** Asking one question warned you; indexing the whole
folder did not, which was the wrong way round. Every cloud model names itself
before the first byte leaves, and a run that embeds with one provider and
answers with another names both.

### Wrong answers, quietly

- **Vectors could be stored against the wrong text.** `embed_texts` ignored the
  index each provider returns alongside its reply, so a batch that came back
  complete but re-ordered was stored one chunk out of step. Nothing downstream
  can detect that: the store is internally consistent, the manifest records the
  count sift meant to write, and the sync reports success. The only symptom is
  a document answering with someone else's text.
- **A sync could lose a file to another sync.** `sift watch` fires one timer per
  burst of file events, and a sync that outlasted the next debounce window met
  its successor. Both are load-modify-save over one index: the one finishing
  last wrote a store built before the other's file existed, and that file lost
  its manifest entry too, so nothing ever re-read it. Watch mode now runs one
  sync at a time.
- **`sift unlock` could be undone by the next sync.** Through a symlinked source
  folder, unlock and the scanner disagreed about a file's key, so the decrypted
  chunks landed where the next scan never looked and were dropped as deleted.
  The password is never stored, so that work could not be recovered.
- **A file deleted mid-scan took the whole scan down.** sift reads every
  candidate in one pass and stats them in a second, and the gap between the two
  is the length of the read pass — during which `sift watch` is scanning while
  you delete things. `sift index` printed a raw traceback; `sift doctor` said it
  could not read your Downloads folder, which was false, and pointed you at
  `sift doctor`.
- **The interactive session went stale after `/sync`.** The loaded index was
  cached for the life of the process and nothing dropped it on write, so `/sync`
  reported "+1 added" and the next question answered "I couldn't find that in
  your documents" until you restarted. `/status` showed the new count next to
  the old results, on the same screen.
- **`--top-k 0` pasted the entire index into the prompt.** `[-0:]` is `[0:]` in
  Python, so the slice meant to take the best 5 took everything. A negative
  `--chunk-overlap` was accepted too, and made the chunker step past part of
  every document — 17% of one file silently unsearchable.
- **A short batch from an embedding provider dropped chunks in silence.** Chunks
  and vectors are now paired strictly, so a mismatch is an error.

### `sift doctor` and `sift watch`

`sift doctor` used to contradict itself in adjacent lines — "using non-local
models: lm_studio/…" directly above "fully local — no document text leaves this
machine" — because two checks asked different questions off different lists.
Worse, a model it did not recognise was not merely described wrongly, it was
never checked at all: doctor returned `[ok]` for a setup where `sift index` then
failed with the litellm traceback doctor exists to prevent. Every configured
model now gets its own line, and one that cannot be verified says `not checked`
rather than nothing.

**Ctrl-C in watch mode lets a running sync finish** — up to 30 seconds — and
says so while it waits. It used to kill the sync wherever it had got to, on a
daemon thread the interpreter never waits for, while printing "Stopping ..." and
exiting 0. Nothing was ever corrupted, but an interrupted save left an
`index.tmp.npz` in the data directory holding a second copy of your document
text, and no command reported it. A second Ctrl-C drops the sync immediately.
`SIGTERM` is still not caught, so none of this applies to `systemctl stop` —
see [`contrib/README.md`](contrib/README.md).

### Smaller things

- The watch-mode install hint said `pip install "sift[watch]"`, which is a
  different project on PyPI. It is `sift-downloads[watch]`.
- Both front ends share one formatter, so a file's size and age cannot render
  two ways.
- Every skip reason is a named constant. Retyping one of the six that were bare
  strings would have silently broken the counter that makes watch mode come back
  for a file still being downloaded.

### Docs

Re-checked against the code for this release. The design notes claimed a crash
"can leave you with the old index or the new one, never half of each" — true of
one write, and read as a claim about the whole sync, which makes two.
[`docs/DESIGN.md`](docs/DESIGN.md) now states both halves and lists the three
states an interrupted sync can leave, none of them corrupt. It also covers the
small-to-big split that shipped in 0.2.0 and was never written up, and where the
privacy gates live. The roadmap no longer calls a cross-encoder re-ranker the
highest-value fix: it was built, measured, and did not fix what it was aimed at.

### For developers

- **ruff** is in CI. Its first pass found five unused imports, a dead local and
  a `# noqa` for a rule that no longer fired.
- **Every pull request has to say how it was verified** — `pr-template` is a
  required check.

560 unit tests and 17 answer-quality evals, on Linux, macOS and Windows across
Python 3.10 to 3.14.

## 0.2.0 — 2026-08-16

**Answers about the right person.**

Given a tax form containing both your job title and the HR signatory's, sift used
to answer "what is my designation?" with the *signatory's* title — every time, on
every model tested. Nothing was invented and the citation was correct; it was the
wrong party's real value. Asked for a landlord's bank account number, it could
reach for a payslip and offer yours.

Both were chunking problems, and both are fixed at the point where documents are
split rather than by ranking or prompting.

- **The text sift matches is no longer the text it reads to you.** Passages are
  indexed as small units (~300 characters, cut on line boundaries) and served as
  the ~1000-character window around them. Matching wants small, answering wants
  large; splitting the two lets each have what it needs.
- **An indexed unit now has a floor** (`SIFT_CHILD_MIN`, 200). Below it a passage
  stops being about anything — a payslip row reading only `Bank A/C No …` embeds
  as *an account number*, which is why a question about someone else's account
  retrieved it. At 200 it embeds as *a payslip* and stops matching.
- **Each passage carries its document's opening line** (`SIFT_DOC_HEAD_CHARS`,
  120), because documents name their owner once, at the top, and never again.
  This is what fixed the designation answer, on both models, with no re-ranker.

Measured across 11 questions and 4 negative controls on both `llama3.1:8b` and
`llama3.2:3b`: correct answers went 10/11 → **11/11**, confident falsehoods
**1 → 0**, and clean refusals on the hardest negative 9/16 → **16/16**.

**The index rebuilds itself.** Vectors now describe different text than they did
in 0.1.x, so an old index cannot be reused — it isn't corrupt, it answers a
different question, and searching it would give plausible scores for the wrong
reasons. The next `sift index` (or any `find` / `ask`, which sync first) detects
this and re-embeds everything once. Expect one slow sync, about a minute for a
few dozen files.

One thing a rebuild cannot recover: text from PDFs you unlocked with a password,
because the password was never stored. Those files are named individually when it
happens, so you can `sift unlock` them again rather than discovering later that
`ask` stopped seeing them.

**Known limit:** cuts land only on line boundaries, so a document extracted as
one unbroken line is served whole and gets no benefit from any of this. That
trade is deliberate — cutting mid-line is what separated a form's value from its
key in the first place.

## 0.1.1 — 2026-08-16

First release on PyPI: `pip install sift-downloads`.

(`v0.1.0` was tagged a day earlier but never published. Two claims in it did
not survive checking — the README said the only network traffic was to
`localhost`, which was untrue, and it understated the model download as ~2GB
against a real 5.2GB. Rather than move a tag that was already pushed, the
corrected code ships as 0.1.1.)

**Find a file, or ask what's in it**

- `sift find "rental agreement"` — ranks files by meaning and by filename, so a
  scanned PDF with no readable text is still findable.
- `sift ask "what is my notice period?"` — answers from your own documents, with
  citations. Refuses without calling the model when nothing relevant is found.
- `sift` on its own — an interactive session with results scrolling into your
  normal terminal history.
- `sift unlock` — reads password-protected PDFs. The password is never stored.
- `sift index`, `watch`, `status`, `search`, `doctor`, `purge`.

**Local by default**

Both models run on Ollama, and a default run opens no connection except to
Ollama on `localhost` — litellm's model-price download is switched off in
favour of the copy bundled in the package. Cloud models work through litellm
but refuse to run without an explicit `--allow-cloud`.

**Built from scratch**

No LangChain, no vector database. Chunking, embedding, a hand-rolled vector
store with an alignment invariant, and cosine search as one dot product against
a matrix of unit vectors. See [docs/DESIGN.md](docs/DESIGN.md).

**Known limits**

No OCR, so scanned PDFs stay findable by name but not searchable by content.
No re-ranker, so retrieval ranks by topic similarity rather than by "does this
answer the question". Top-level files only. See the design notes for the rest.
