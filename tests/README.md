# Tests

```bash
pip install -e ".[watch,dev]"
pytest                                  # ~3s
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
| `test_doctor.py` | preflight — mostly asserting the *fix* string, not the failure; which models each command declares |
| `test_session.py` | parsing a typed line into a Request |
| `test_ui.py` | rendering and dispatch, via an injected `Console` |
| `test_session_runner.py` | what a key means, with no terminal involved |
| `test_terminal.py` | the rich→ANSI bridge, the live region, and the pinned box driven headlessly |
| `test_platform.py` | `open`/`xdg-open`/`explorer` per OS; watcher debouncing, one-sync-at-a-time, shutdown |
| `test_small_to_big.py` | the indexed unit vs. the served window, and the document head |
| `test_pr_template.py` | the `pr-template` check's rules, as a pure function |

## Deliberate coverage gaps

Coverage is gated at 92% on one job, and sits comfortably above it. The figure
is not written down here for the same reason the test count is not: it changes
on most pull requests and nothing checks it. What is left out, and why:

- **`ui.read_line`** — the one-shot box `_offer_setup` still uses for its
  yes/no prompt. Testing it would prove that prompt_toolkit works.
- **`config._windows_downloads`** — a ctypes call into `shell32`, unreachable
  off Windows. Its fallback path and both callers are covered.

Chasing these to 100% would add brittle tests, not confidence.

`terminal.py` is deliberately *not* on that list. prompt_toolkit ships
`create_pipe_input()`, `DummyOutput()` and `create_app_session()`, which drive a
real inline Application with no pty — so the key bindings are tested rather than
excused. What is left uncovered there is the paint call itself, two early
returns that guard against there being no Application yet, and the width
fallback for the same case — checked against `--cov-report=term-missing`, not
assumed.

## Conventions worth knowing before you add a test

**A test that cannot fail is worse than no test, because it reads as
protection.** This branch produced fourteen of them — assertions true for
reasons unrelated to the code beneath them. The recurring shapes: a value the
runtime guarantees anyway (`run_coroutine_threadsafe` is FIFO, so asserting
commits "arrive in order" passes with the ordering code deleted); an assertion
the test harness satisfies regardless (`drive()` always ends with ctrl-d, so
`result is None` cannot tell ctrl-c from ctrl-d); and a stub fast enough that
the state under test never occurs (a queue test whose first line finished before
the second arrived, leaving `start(nxt)` unreachable by the entire suite).
Break the thing your test guards and watch it fail before you trust it.

**No document states how many tests there are.** The count was written into
four files and every one of them was wrong: `README.md` had frozen at 0.4.0's
number, this file and `docs/DESIGN.md` at 0.3.0's, and they disagreed with each
other and with reality at the same time. A figure that changes on most pull
requests and is checked by nothing rots by default, so the figures are gone
rather than refreshed. `CHANGELOG.md` is the exception and keeps its counts:
there they are a frozen record of what a released version shipped, not a claim
about the suite today. Pinned by `test_no_document_claims_an_exact_test_count`.

**A test that proves an absence must first prove the path ran.** The freshness
guard defers a just-written file as "still being written", so a sync can report
nothing because it did nothing — not because the thing under test is broken.
`conftest.write_file` backdates 60 seconds for exactly this. Assert the
precondition (`stats.chunks_total > 0`) next to the absence, or the test goes
green on an empty run.

**Concurrency tests use `threading.Event` pairs, never `sleep`,** and run
anything that could deadlock on a thread with `join(timeout=…)`. Called
directly, a deadlock freezes pytest with no output at all — which is a test
that detects the bug by hanging CI, and is barely a test.

## Checks worth re-running when the suite changes

```bash
pytest --randomly-seed=1234   # order independence
```

The suite is order-independent by design; `pytest-randomly` shuffles on every
run by default, so a failure that only appears on some seeds means a test is
leaking state.
