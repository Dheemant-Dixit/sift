# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

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
