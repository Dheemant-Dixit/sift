# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

## 0.1.0 — 2026-08-15

First public release.

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

Both models run on Ollama. No document text leaves your machine. Cloud models
work through litellm but refuse to run without an explicit `--allow-cloud`.

Note: litellm downloads a public model price list from `raw.githubusercontent.com`
on startup. It sends nothing about you, and `LITELLM_LOCAL_MODEL_COST_MAP=True`
turns it off.

**Built from scratch**

No LangChain, no vector database. Chunking, embedding, a hand-rolled vector
store with an alignment invariant, and cosine search as one dot product against
a matrix of unit vectors. See [docs/DESIGN.md](docs/DESIGN.md).

**Known limits**

No OCR, so scanned PDFs stay findable by name but not searchable by content.
No re-ranker, so retrieval ranks by topic similarity rather than by "does this
answer the question". Top-level files only. See the design notes for the rest.
