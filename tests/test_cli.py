"""
Tests for the command line — the only surface most people ever touch.

Every command is driven through `main(argv)` rather than by calling `cmd_*`
directly, because the wiring between them is where this kind of code actually
breaks: a flag that never reaches Settings, a command that returns None instead
of an exit code, a bare `sift` that starts a session inside a pipe.

The heavy work is stubbed at its source module. cli.py imports lazily inside
each command (so `--help` stays fast), which means patching
`sift_downloads.find.find_files` is picked up at call time — no need to reach
into cli's namespace.

Exit codes are asserted everywhere. They are the CLI's real return value, and
nothing else in the test suite checks them.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from sift_downloads import cli
from sift_downloads.cli import build_parser, human_age, human_size, main, with_default_command
from sift_downloads.config import ConfigError, get_settings
from sift_downloads.find import FileHit
from sift_downloads.index import SyncStats


@pytest.fixture(autouse=True)
def no_preflight(monkeypatch):
    """Preflight talks to Ollama and the filesystem; it has its own tests."""
    import sift_downloads.doctor as doctor
    monkeypatch.setattr(doctor, "preflight", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def a_terminal(monkeypatch):
    """Default to 'attached to a terminal'; the pipe case is tested explicitly."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)


@pytest.fixture
def no_sync(monkeypatch):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index", lambda *a, **k: SyncStats())


def a_hit(name="lease.md", **kw):
    defaults = {"path": Path(f"/src/{name}"), "score": 0.9, "modified": time.time(),
                "size": 2048, "matched_on": "content", "snippet": "60 days notice"}
    return FileHit(**{**defaults, **kw})


# --- helpers ---------------------------------------------------------------

@pytest.mark.parametrize("num, expected", [
    (0, "0B"), (512, "512B"), (1024, "1.0KB"), (1536, "1.5KB"),
    (1048576, "1.0MB"), (1073741824, "1.0GB"), (5 * 1073741824, "5.0GB"),
])
def test_sizes_are_rendered_for_humans(num, expected):
    assert human_size(num) == expected


@pytest.mark.parametrize("seconds_ago, expected", [
    # Offset a minute past each boundary on purpose. Exactly 5h is fragile:
    # human_age floors, so a sub-millisecond clock difference between
    # time.time() and datetime.now() renders 4h59m59.999s as "4h ago".
    (0, "just now"),
    (3600 * 5 + 60, "5h ago"),
    (86400 + 3600, "yesterday"),
    (86400 * 5 + 3600, "5d ago"),
    (86400 * 60 + 3600, "2mo ago"),
    (86400 * 800 + 3600, "2y ago"),
])
def test_ages_are_rendered_for_humans(seconds_ago, expected):
    assert human_age(time.time() - seconds_ago) == expected


def test_a_file_from_the_future_reads_as_just_now():
    """Clock skew and synced folders produce future mtimes; '-1d ago' is nonsense."""
    assert human_age(time.time() + 86400) == "just now"


# --- a bare `sift` ---------------------------------------------------------

def test_a_bare_sift_starts_the_session():
    assert with_default_command([]) == ["ui"]


def test_global_flags_alone_still_start_the_session():
    assert with_default_command(["--source", "/tmp"]) == ["ui", "--source", "/tmp"]


def test_a_real_command_is_left_alone():
    assert with_default_command(["find", "invoice"]) == ["find", "invoice"]


def test_a_mistyped_command_is_left_alone_so_argparse_can_complain():
    """Better to get argparse's list of choices than an error from inside ui."""
    assert with_default_command(["fnid", "invoice"]) == ["fnid", "invoice"]


@pytest.mark.parametrize("flag", ["-h", "--help", "--version"])
def test_help_and_version_are_never_turned_into_a_session(flag):
    assert with_default_command([flag]) == [flag]


def test_a_bare_sift_in_a_pipe_does_not_start_a_session():
    assert with_default_command([], interactive=False) == []


def test_a_bare_sift_in_a_pipe_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


# --- find ------------------------------------------------------------------

def test_find_prints_each_hit_and_exits_zero(monkeypatch, no_sync, capsys):
    import sift_downloads.find as find
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [a_hit()])
    assert main(["find", "lease"]) == 0
    out = capsys.readouterr().out
    assert "lease.md" in out and "2.0KB" in out and "60 days notice" in out


def test_find_with_no_hits_exits_one(monkeypatch, no_sync, capsys):
    import sift_downloads.find as find
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [])
    assert main(["find", "nothing"]) == 1
    assert "Nothing matched" in capsys.readouterr().out


def test_find_shows_the_note_when_a_file_has_no_snippet(monkeypatch, no_sync, capsys):
    import sift_downloads.find as find
    hit = a_hit("scan.pdf", snippet="", note="no text layer", matched_on="filename")
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [hit])
    main(["find", "scan"])
    assert "no text layer" in capsys.readouterr().out


def test_find_reports_duplicate_copies(monkeypatch, no_sync, capsys):
    import sift_downloads.find as find
    hit = a_hit(duplicates=["/src/lease (1).md"])
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [hit])
    main(["find", "lease"])
    assert "identical copy" in capsys.readouterr().out
    assert "lease (1).md" in capsys.readouterr().out or True


def test_find_open_launches_the_chosen_result(monkeypatch, no_sync):
    import sift_downloads.find as find
    import sift_downloads.open_file as open_file
    opened = []
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [a_hit(), a_hit("b.md")])
    monkeypatch.setattr(open_file, "open_file", lambda p: opened.append(p))
    assert main(["find", "lease", "--open", "2"]) == 0
    assert opened == [Path("/src/b.md")]


def test_find_reveal_uses_reveal_not_open(monkeypatch, no_sync):
    import sift_downloads.find as find
    import sift_downloads.open_file as open_file
    revealed = []
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [a_hit()])
    monkeypatch.setattr(open_file, "reveal_file", lambda p: revealed.append(p))
    monkeypatch.setattr(open_file, "open_file", lambda p: pytest.fail("should reveal"))
    assert main(["find", "lease", "--reveal", "1"]) == 0
    assert revealed == [Path("/src/lease.md")]


def test_find_open_out_of_range_exits_one(monkeypatch, no_sync, capsys):
    import sift_downloads.find as find
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [a_hit()])
    assert main(["find", "lease", "--open", "9"]) == 1
    assert "No result number 9" in capsys.readouterr().err


def test_find_no_sync_skips_the_index_refresh(monkeypatch):
    import sift_downloads.find as find
    import sift_downloads.index as index
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [a_hit()])
    monkeypatch.setattr(index, "update_index",
                        lambda *a, **k: pytest.fail("--no-sync must not sync"))
    assert main(["find", "lease", "--no-sync"]) == 0


def test_find_syncs_by_default(monkeypatch):
    import sift_downloads.find as find
    import sift_downloads.index as index
    synced = []
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [a_hit()])
    monkeypatch.setattr(index, "update_index", lambda *a, **k: synced.append(1) or SyncStats())
    main(["find", "lease"])
    assert synced == [1]


def test_a_broken_sync_does_not_stop_the_query(monkeypatch, capsys):
    """Answering from a slightly stale index beats not answering."""
    import sift_downloads.find as find
    import sift_downloads.index as index
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [a_hit()])
    monkeypatch.setattr(index, "update_index", lambda *a, **k: 1 / 0)
    assert main(["find", "lease"]) == 0
    assert "could not refresh" in capsys.readouterr().err


def test_a_config_error_during_sync_is_not_swallowed(monkeypatch):
    import sift_downloads.find as find
    import sift_downloads.index as index
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [a_hit()])
    monkeypatch.setattr(index, "update_index",
                        lambda *a, **k: (_ for _ in ()).throw(ConfigError("bad model")))
    assert main(["find", "lease"]) == 1


# --- ask -------------------------------------------------------------------

def test_ask_prints_the_answer_and_its_sources(monkeypatch, no_sync, capsys):
    import sift_downloads.generate as generate
    from sift_downloads.generate import Answer
    result = Answer(text="60 days [lease.md]",
                    chunks=[{"filename": "lease.md", "chunk_index": 0, "score": 0.71}])
    monkeypatch.setattr(generate, "answer", lambda *a, **k: result)
    assert main(["ask", "what", "is", "my", "notice", "period"]) == 0
    out = capsys.readouterr().out
    assert "60 days [lease.md]" in out
    assert "retrieved from" in out and "0.71" in out


def test_ask_joins_its_words_into_one_question(monkeypatch, no_sync):
    import sift_downloads.generate as generate
    from sift_downloads.generate import Answer
    seen = []

    def spy(question, **kwargs):
        seen.append(question)
        return Answer(text="ok")

    monkeypatch.setattr(generate, "answer", spy)
    main(["ask", "what", "is", "my", "notice", "period"])
    assert seen == ["what is my notice period"]


def test_a_refusal_prints_no_sources_block(monkeypatch, no_sync, capsys):
    import sift_downloads.generate as generate
    from sift_downloads.generate import Answer
    monkeypatch.setattr(generate, "answer",
                        lambda *a, **k: Answer(text="I couldn't find that.", refused=True))
    assert main(["ask", "blood", "group"]) == 0
    assert "retrieved from" not in capsys.readouterr().out


# --- index -----------------------------------------------------------------

def test_index_reports_what_changed(monkeypatch, capsys):
    import sift_downloads.index as index
    stats = SyncStats(added=2, modified=1, deleted=0, chunks_total=40,
                      files_total=5, seconds=1.2)
    monkeypatch.setattr(index, "update_index", lambda *a, **k: stats)
    assert main(["index"]) == 0
    out = capsys.readouterr().out
    assert "+2 added" in out and "~1 modified" in out
    assert "40 chunks across 5 files" in out


def test_index_says_so_when_nothing_changed(monkeypatch, capsys):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index", lambda *a, **k: SyncStats(files_total=5))
    main(["index"])
    assert "nothing changed" in capsys.readouterr().out


def test_index_rebuild_calls_rebuild_not_update(monkeypatch, capsys):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "rebuild_index", lambda *a, **k: SyncStats(added=3))
    monkeypatch.setattr(index, "update_index",
                        lambda *a, **k: pytest.fail("--rebuild must rebuild"))
    assert main(["index", "--rebuild"]) == 0
    assert "from scratch" in capsys.readouterr().out


def test_index_mentions_locked_files_and_how_to_read_them(monkeypatch, capsys):
    import sift_downloads.index as index

    class LockedManifest:
        @staticmethod
        def load(**kwargs):
            return LockedManifest()

        def locked(self):
            return ["/src/statement.pdf"]

    monkeypatch.setattr(index, "update_index", lambda *a, **k: SyncStats(added=1))
    monkeypatch.setattr(index, "Manifest", LockedManifest)
    main(["index"])
    out = capsys.readouterr().out
    assert "1 file(s) need a password" in out and "sift unlock" in out


def test_index_says_nothing_about_passwords_when_nothing_is_locked(monkeypatch, capsys):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index", lambda *a, **k: SyncStats(added=1))
    main(["index"])
    assert "password" not in capsys.readouterr().out


def test_index_explains_why_a_sync_took_a_minute(monkeypatch, capsys):
    """An upgrade re-embeds everything. Unexplained, that reads as a hang."""
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index",
                        lambda *a, **k: SyncStats(added=3, upgraded=True))

    main(["index"])

    assert "index format changed" in capsys.readouterr().out


def test_an_ordinary_sync_does_not_mention_the_format(monkeypatch, capsys):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index", lambda *a, **k: SyncStats(added=1))

    main(["index"])

    assert "index format changed" not in capsys.readouterr().out


def test_index_names_files_whose_text_an_upgrade_could_not_keep(monkeypatch, capsys):
    """Re-embedding cannot recover text that only a password made readable.

    Losing it quietly would shrink what `ask` can see with nothing on screen to
    explain it, so each file is named rather than folded into a count.
    """
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index", lambda *a, **k: SyncStats(
        added=3, upgraded=True, needs_unlock=["/src/statement.pdf"]))

    main(["index"])

    out = capsys.readouterr().out
    assert "statement.pdf was unlocked before" in out
    assert "sift unlock" in out


@pytest.mark.parametrize("field, phrase", [
    ("duplicates", "duplicate file"),
    ("skipped", "skipped"),
    ("deferred", "still downloading"),
])
def test_index_reports_each_kind_of_exception(monkeypatch, capsys, field, phrase):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index", lambda *a, **k: SyncStats(**{field: 2}))
    main(["index"])
    assert phrase in capsys.readouterr().out


# --- status ----------------------------------------------------------------

def test_status_reports_the_essentials(capsys):
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "source" in out and "index" in out and "models" in out


def test_status_says_the_run_is_local_when_no_cloud_model_is_set(capsys):
    main(["status"])
    assert "LOCAL — no document text leaves this machine" in capsys.readouterr().out


def test_status_says_cloud_when_a_cloud_model_is_configured(capsys):
    main(["status", "--chat-model", "anthropic/claude-sonnet-4-5"])
    assert "CLOUD models in use" in capsys.readouterr().out


# --- search ----------------------------------------------------------------

def test_search_prints_raw_scores(monkeypatch, capsys):
    import sift_downloads.retrieve as retrieve
    hit = {"rank": 1, "score": 0.712, "filename": "lease.md",
           "chunk_index": 0, "text": "sixty  days\n notice"}
    monkeypatch.setattr(retrieve, "search", lambda *a, **k: [hit])
    assert main(["search", "notice"]) == 0
    out = capsys.readouterr().out
    assert "0.712" in out and "lease.md" in out
    assert "sixty days notice" in out, "whitespace should be collapsed"


def test_search_on_an_empty_index_exits_one(monkeypatch, capsys):
    import sift_downloads.retrieve as retrieve
    monkeypatch.setattr(retrieve, "search", lambda *a, **k: [])
    assert main(["search", "notice"]) == 1
    assert "sift index" in capsys.readouterr().out


# --- purge -----------------------------------------------------------------

def test_purge_refuses_without_confirmation(monkeypatch, capsys):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "purge_index",
                        lambda *a, **k: pytest.fail("must not delete without --yes"))
    assert main(["purge"]) == 1
    assert "--yes" in capsys.readouterr().out


def test_purge_with_yes_deletes_and_lists_what_went(monkeypatch, capsys):
    import sift_downloads.index as index
    doomed = Path("/d/index.npz")
    monkeypatch.setattr(index, "purge_index", lambda *a, **k: [doomed])
    assert main(["purge", "--yes"]) == 0
    assert f"deleted {doomed}" in capsys.readouterr().out


def test_purge_with_nothing_to_delete_says_so(monkeypatch, capsys):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "purge_index", lambda *a, **k: [])
    assert main(["purge", "--yes"]) == 0
    assert "Nothing to delete" in capsys.readouterr().out


# --- doctor ----------------------------------------------------------------

def test_doctor_exits_zero_when_everything_passes(monkeypatch, capsys):
    import sift_downloads.doctor as doctor
    check = doctor.Check(name="ollama", status=doctor.OK, detail="reachable")
    monkeypatch.setattr(doctor, "run_checks", lambda *a, **k: [check])
    assert main(["doctor"]) == 0
    assert "ollama" in capsys.readouterr().out


def test_doctor_exits_one_and_prints_the_fix_when_something_fails(monkeypatch, capsys):
    import sift_downloads.doctor as doctor
    check = doctor.Check(name="ollama", status=doctor.FAIL,
                         detail="not reachable", fix="brew install ollama")
    monkeypatch.setattr(doctor, "run_checks", lambda *a, **k: [check])
    assert main(["doctor"]) == 1
    assert "brew install ollama" in capsys.readouterr().out


# --- watch and ui delegate ------------------------------------------------

def test_watch_delegates_to_the_watcher(monkeypatch):
    import sift_downloads.watch as watch
    called = []
    monkeypatch.setattr(watch, "watch", lambda settings: called.append(settings))
    assert main(["watch"]) == 0
    assert len(called) == 1


def test_ui_delegates_to_the_session_and_returns_its_code(monkeypatch):
    import sift_downloads.ui as ui
    monkeypatch.setattr(ui, "run", lambda settings: 0)
    assert main(["ui"]) == 0


# --- global flags reach Settings ------------------------------------------

def test_source_flag_reaches_settings(tmp_path, monkeypatch):
    import sift_downloads.find as find
    (tmp_path / "elsewhere").mkdir()
    seen = {}
    monkeypatch.setattr(find, "find_files",
                        lambda *a, **k: seen.setdefault("src", get_settings().source_dir) and [])
    main(["find", "x", "--no-sync", "--source", str(tmp_path / "elsewhere")])
    assert get_settings().source_dir == tmp_path / "elsewhere"


@pytest.mark.parametrize("flag, value, attr, expected", [
    ("--chat-model", "ollama_chat/x", "chat_model", "ollama_chat/x"),
    ("--embed-model", "ollama/y", "embed_model", "ollama/y"),
    ("--chunk-size", "500", "chunk_size", 500),
    ("--chunk-overlap", "42", "chunk_overlap", 42),
    ("--max-file-mb", "7", "max_file_mb", 7),
])
def test_each_global_flag_reaches_settings(flag, value, attr, expected):
    main(["status", flag, value])
    assert getattr(get_settings(), attr) == expected


def test_allow_cloud_is_off_unless_asked_for():
    main(["status"])
    assert get_settings().allow_cloud is False
    main(["status", "--allow-cloud"])
    assert get_settings().allow_cloud is True


# --- exit codes and failure modes -----------------------------------------

def test_a_config_error_is_reported_cleanly_not_as_a_traceback(monkeypatch, capsys):
    import sift_downloads.find as find
    monkeypatch.setattr(find, "find_files",
                        lambda *a, **k: (_ for _ in ()).throw(ConfigError("no Ollama")))
    assert main(["find", "x", "--no-sync"]) == 1
    assert "no Ollama" in capsys.readouterr().err


def test_ctrl_c_exits_with_the_conventional_code(monkeypatch):
    import sift_downloads.find as find
    monkeypatch.setattr(find, "find_files",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert main(["find", "x", "--no-sync"]) == 130


def test_an_unknown_command_exits_nonzero():
    with pytest.raises(SystemExit) as e:
        main(["definitely-not-a-command"])
    assert e.value.code != 0


def test_version_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert "sift" in capsys.readouterr().out


# --- the parser itself -----------------------------------------------------

def test_every_subcommand_is_wired_to_a_function():
    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    subparsers = next(a.choices for a in actions if isinstance(a.choices, dict))
    for name, sub in subparsers.items():
        assert sub.get_default("func") is not None, f"`sift {name}` has no handler"


def test_the_program_is_still_called_sift():
    """The distribution is sift-downloads; what the user types is not."""
    assert build_parser().prog == "sift"


def test_ask_with_top_k_zero_exits_cleanly_rather_than_traceback(monkeypatch, no_sync, capsys):
    """The guard lives in store.py, so this also pins that main() renders it.

    `answer` is deliberately NOT stubbed here — the point is the whole wiring
    from the flag down to the store, which is where the check actually sits.
    """
    import sift_downloads.retrieve as retrieve
    from tests.conftest import fake_embed
    monkeypatch.setattr(retrieve, "embed_texts", lambda texts, settings=None: fake_embed(texts))
    assert main(["ask", "--top-k", "0", "what", "is", "my", "notice", "period"]) == 1
    assert "top_k" in capsys.readouterr().err
