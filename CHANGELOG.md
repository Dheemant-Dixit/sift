# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

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
