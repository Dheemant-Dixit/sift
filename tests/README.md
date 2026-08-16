# Tests

```bash
pip install -e ".[watch,dev]"
pytest                                  # ~1.6s
pytest --cov=sift_downloads --cov-report=term-missing
```

**No Ollama. No network. Never reads your real Downloads folder.** Two autouse
fixtures in `conftest.py` enforce that rather than trusting it: `isolated_settings`
points every path at `tmp_path`, and `no_model_server` makes `litellm.embedding`
and `litellm.completion` raise. Six tests were written that quietly needed a
running Ollama before the second fixture existed — they passed locally and would
have failed in CI.

If a test fails with *"this test reached litellm"*, it needs a fake embedder:
pass `embedder=fake_embed`, or monkeypatch `index.embed_texts`.

## Layout

One file per concern; sections inside marked with `# --- name ---`.

| file | what it pins down |
|---|---|
| `test_generate.py` | **the refusal guarantee** — that no model call happens when nothing clears the bar |
| `test_store.py` | the vector/metadata alignment invariant, corrupt-index rejection |
| `test_index.py` | incremental sync, provenance, `embed_texts` batching |
| `test_ingest.py` | extraction, locked vs. scanned PDFs, the reason vocabulary |
| `test_ingest_edges.py` | directories, dotfiles, empty/huge/unreadable files, duplicate ranking |
| `test_chunk.py` | overlap, boundary snapping, the anti-infinite-loop guard |
| `test_find.py` | file-level ranking, filename matching, duplicate collapsing |
| `test_retrieve.py` | the store cache and its invalidation; the package's public surface |
| `test_config.py` | precedence: flag > env > `.env` > default |
| `test_config_platform.py` | per-OS folder resolution, including localized Linux Downloads |
| `test_cli.py` | every subcommand through `main(argv)`, and its exit code |
| `test_cli_unlock_status.py` | `sift unlock` (passwords) and `sift status` detail |
| `test_doctor.py` | preflight — mostly asserting the *fix* string, not the failure |
| `test_session.py` | parsing a typed line into a Request |
| `test_ui.py` | rendering and dispatch, via an injected `Console` |
| `test_platform.py` | `open`/`xdg-open`/`explorer` per OS; watcher debouncing |

## Deliberate coverage gaps

Coverage is ~96%. What is left out, and why:

- **`ui.read_line` and `ui.run`** — prompt_toolkit key bindings and the event
  loop. Testing them needs a pty, and what it would prove is that
  prompt_toolkit works. Everything they call into is covered.
- **`config._windows_downloads`** — a ctypes call into `shell32`, unreachable
  off Windows. Its fallback path and both callers are covered.

Chasing these to 100% would add brittle tests, not confidence.

## Checks worth re-running when the suite changes

```bash
pytest -p randomly --randomly-seed=1234   # order independence
```

The suite is order-independent by design; `pytest-randomly` shuffles on every
run by default, so a failure that only appears on some seeds means a test is
leaking state.
