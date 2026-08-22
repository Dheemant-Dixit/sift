"""
Tests for the interactive session's screen and its dispatch table.

`Ui` takes an injectable Console, which is the seam that makes this testable
without asserting on escape sequences: a Console pointed at a StringIO with
colour off renders plain text, so these tests read like what a user sees.

Deliberately NOT tested here: `read_line`, which is prompt_toolkit key bindings
and a full-screen Application, and `run()`, which is the event loop around it.
Both would need a pty to say anything real, and what they'd prove is that
prompt_toolkit works. Everything they call into is covered below.
"""
from __future__ import annotations

import time
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from sift_downloads import ui as ui_module
from sift_downloads.find import FileHit
from sift_downloads.index import SyncStats
from sift_downloads.session import Request, Session, UiCommand
from sift_downloads.ui import Ui, _clip, dispatch


@pytest.fixture
def screen():
    """A Ui writing plain text into a buffer, plus a reader for it."""
    buffer = StringIO()
    # Wide on purpose: tmp_path is long, and rich would wrap it mid-string,
    # making assertions fail on line breaks rather than on content.
    console = Console(file=buffer, width=400, force_terminal=False,
                      no_color=True, highlight=False)
    ui = Ui(Session(), console=console)
    ui.read = lambda: buffer.getvalue()
    return ui


def a_hit(name="lease.md", **kw):
    defaults = {"path": Path(f"/src/{name}"), "score": 0.9, "modified": time.time(),
                "size": 2048, "matched_on": "content", "snippet": "60 days notice"}
    return FileHit(**{**defaults, **kw})


# --- clipping ---------------------------------------------------------------

def test_short_text_is_left_alone():
    assert _clip("hello", 20) == "hello"


def test_whitespace_is_collapsed():
    assert _clip("a\n\n  b\tc", 20) == "a b c"


def test_long_text_is_cut_with_an_ellipsis():
    out = _clip("x" * 50, 10)
    assert len(out) == 10 and out.endswith("…")


def test_clipping_never_leaves_a_trailing_space():
    assert not _clip("word " * 20, 12).rstrip("…").endswith(" ")


def test_clipping_to_a_tiny_room_still_returns_something():
    assert _clip("a very long line indeed", 1)


# --- the region seam --------------------------------------------------------

def test_a_plain_status_stays_out_of_scrollback(screen):
    """A status is transient and scrollback cannot be erased. The startup sync
    is quiet so it does not push the banner off the screen — printing here
    breaks that, and the spinner is LiveRegion's job anyway."""
    before = screen.read()
    with screen.region.status("searching..."):
        pass
    assert screen.read() == before


def test_a_plain_region_does_not_accumulate_progress_updates(screen):
    """`_offer_setup` calls update() once per chunk of a model download. One
    line each would turn a single pull into hundreds."""
    before = screen.read()
    with screen.region.status("downloading...") as status:
        for done in range(0, 500, 50):
            status.update(f"llama3.1 — pulling {done}MB / 500MB")
    assert screen.read() == before


def test_a_plain_region_holds_streamed_text_until_it_is_flushed(screen):
    screen.region.append("60 ")
    screen.region.append("days")
    assert "60 days" not in screen.read()
    screen.region.flush()
    assert "60 days" in screen.read()


def test_flushing_an_empty_region_prints_nothing(screen):
    before = screen.read()
    screen.region.flush()
    assert screen.read() == before


def test_streamed_text_keeps_its_indent(screen):
    """Live was chosen over raw deltas because a wrapped line reflows to column 0
    and breaks the indent. Whatever replaces it has to keep that."""
    screen.region.append("a line")
    screen.region.flush()
    assert "  a line" in screen.read()


# --- chrome -----------------------------------------------------------------

def test_the_banner_names_the_folder_and_the_model(screen):
    screen.banner()
    out = screen.read()
    assert str(screen.session.settings.source_dir) in out
    assert screen.session.settings.chat_model in out


def test_the_banner_says_local_when_nothing_leaves(screen):
    screen.banner()
    assert "local" in screen.read()


def test_the_banner_flags_cloud_models(screen):
    from sift_downloads.config import configure
    configure(chat_model="anthropic/claude-sonnet-4-5")
    screen.session.settings = configure(chat_model="anthropic/claude-sonnet-4-5")
    screen.banner()
    assert "cloud models in use" in screen.read()


def test_the_submitted_line_is_echoed_into_scrollback(screen):
    """The input box erases itself, so without this the query vanishes."""
    screen.echo("rental agreement")
    assert "rental agreement" in screen.read()


def test_help_lists_the_commands(screen):
    screen.help()
    out = screen.read()
    assert "/open" in out and "/help" in out


# --- results ----------------------------------------------------------------

def test_no_hits_says_so_and_suggests_a_next_step(screen):
    screen.results([], "unicorn")
    out = screen.read()
    assert "nothing matched" in out and "/sync" in out


def test_a_hit_shows_name_size_and_snippet(screen):
    screen.results([a_hit()], "lease")
    out = screen.read()
    assert "lease.md" in out and "2.0KB" in out and "60 days notice" in out


def test_results_are_numbered_so_open_has_something_to_refer_to(screen):
    screen.results([a_hit("a.md"), a_hit("b.md")], "x")
    out = screen.read()
    assert "1." in out and "2." in out


@pytest.mark.parametrize("matched_on, marker", [
    ("content", "·"), ("filename", "name"), ("both", "·+name"),
])
def test_each_kind_of_match_is_marked(screen, matched_on, marker):
    screen.results([a_hit(matched_on=matched_on)], "x")
    assert marker in screen.read()


def test_a_file_with_no_text_shows_why_instead_of_a_snippet(screen):
    screen.results([a_hit("scan.pdf", snippet="", note="no text layer")], "scan")
    assert "no text layer" in screen.read()


def test_a_source_shows_its_file_chunk_and_score(screen):
    screen.sources([{"filename": "lease.md", "chunk_index": 0, "score": 0.71}])
    assert "lease.md (chunk 0, 0.71)" in screen.read()


def test_the_sources_heading_can_be_reworded(screen):
    """`ask` says "reading from" because it prints the block before the answer."""
    screen.sources([{"filename": "lease.md", "chunk_index": 0, "score": 0.71}],
                   heading="reading from")
    assert "— reading from —" in screen.read()


def test_no_sources_block_when_there_are_no_chunks(screen):
    screen.sources([])
    assert screen.read().strip() == ""


# --- dispatch ---------------------------------------------------------------

def test_quit_ends_the_session(screen):
    assert dispatch(screen, Request(UiCommand.QUIT)) is False


def test_every_other_command_continues_the_session(screen):
    assert dispatch(screen, Request(UiCommand.NOTHING)) is True


def test_an_error_request_is_shown(screen):
    dispatch(screen, Request(UiCommand.ERROR, message="unknown command /frobnicate"))
    assert "unknown command /frobnicate" in screen.read()


def test_help_is_dispatched(screen):
    dispatch(screen, Request(UiCommand.HELP))
    assert "/open" in screen.read()


def test_find_is_dispatched_and_remembers_the_hits(screen, monkeypatch):
    import sift_downloads.find as find
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [a_hit()])
    dispatch(screen, Request(UiCommand.FIND, argument="lease"))
    assert "lease.md" in screen.read()
    assert len(screen.session.last_hits) == 1


def test_find_records_the_query_in_history(screen, monkeypatch):
    import sift_downloads.find as find
    monkeypatch.setattr(find, "find_files", lambda *a, **k: [])
    dispatch(screen, Request(UiCommand.FIND, argument="lease"))
    assert ("user", "lease") in screen.session.history


# --- asking -----------------------------------------------------------------

class FakeStream:
    def __init__(self, deltas=(), refusal=None, chunks=()):
        self._deltas, self.refusal, self.chunks = deltas, refusal, list(chunks)
        self.text = "".join(deltas)

    def __iter__(self):
        return iter(self._deltas)

    def finish(self):
        from sift_downloads.generate import Answer
        if self.refusal is not None:
            return self.refusal
        return Answer(text=self.text, chunks=self.chunks)


def test_a_refusal_is_shown_and_nothing_is_streamed(screen, monkeypatch):
    import sift_downloads.generate as generate
    from sift_downloads.generate import Answer
    refusal = Answer(text="I couldn't find that in your documents.", refused=True)
    monkeypatch.setattr(generate, "AnswerStream",
                        lambda *a, **k: FakeStream(refusal=refusal))
    dispatch(screen, Request(UiCommand.ASK, argument="blood group"))
    assert "couldn't find that" in screen.read()


def test_an_answer_is_streamed_and_its_sources_listed(screen, monkeypatch):
    import sift_downloads.generate as generate
    chunks = [{"filename": "lease.md", "chunk_index": 0, "score": 0.71}]
    monkeypatch.setattr(generate, "AnswerStream",
                        lambda *a, **k: FakeStream(["60 ", "days"], chunks=chunks))
    dispatch(screen, Request(UiCommand.ASK, argument="notice period"))
    out = screen.read()
    assert "60 days" in out and "lease.md" in out


def test_sources_are_shown_before_the_answer_not_after(screen, monkeypatch):
    """The filenames are known before the model is called, so they go first.

    Asserting order, not presence: the whole point of the change is *where*
    the block lands, and a presence check goes green either way.
    """
    import sift_downloads.generate as generate
    chunks = [{"filename": "lease.md", "chunk_index": 0, "score": 0.71}]
    monkeypatch.setattr(generate, "AnswerStream",
                        lambda *a, **k: FakeStream(["60 ", "days"], chunks=chunks))
    dispatch(screen, Request(UiCommand.ASK, argument="notice period"))
    out = screen.read()
    assert out.count("lease.md") == 1, "the sources block is printed twice"
    assert out.index("lease.md") < out.index("60 days")


def test_an_empty_answer_is_called_out_rather_than_shown_as_blank(screen, monkeypatch):
    import sift_downloads.generate as generate
    monkeypatch.setattr(generate, "AnswerStream", lambda *a, **k: FakeStream([""]))
    dispatch(screen, Request(UiCommand.ASK, argument="q"))
    assert "returned nothing" in screen.read()


def test_the_answer_is_remembered(screen, monkeypatch):
    import sift_downloads.generate as generate
    monkeypatch.setattr(generate, "AnswerStream", lambda *a, **k: FakeStream(["hi"]))
    dispatch(screen, Request(UiCommand.ASK, argument="q"))
    assert ("assistant", "hi") in screen.session.history


# --- opening ----------------------------------------------------------------

def test_open_launches_the_resolved_file(screen, monkeypatch):
    import sift_downloads.open_file as open_file
    opened = []
    screen.session.last_hits = [a_hit()]
    monkeypatch.setattr(open_file, "open_file", lambda p: opened.append(p))
    dispatch(screen, Request(UiCommand.OPEN, index=1))
    assert opened == [Path("/src/lease.md")]
    assert "opened lease.md" in screen.read()


def test_reveal_uses_reveal(screen, monkeypatch):
    import sift_downloads.open_file as open_file
    revealed = []
    screen.session.last_hits = [a_hit()]
    monkeypatch.setattr(open_file, "reveal_file", lambda p: revealed.append(p))
    dispatch(screen, Request(UiCommand.REVEAL, index=1))
    assert revealed and "revealed lease.md" in screen.read()


def test_opening_an_out_of_range_result_reports_the_problem(screen):
    screen.session.last_hits = [a_hit()]
    dispatch(screen, Request(UiCommand.OPEN, index=9))
    assert screen.read().strip(), "a bad index must say something"


def test_a_failing_open_is_reported_not_raised(screen, monkeypatch):
    import sift_downloads.open_file as open_file
    screen.session.last_hits = [a_hit()]
    monkeypatch.setattr(open_file, "open_file",
                        lambda p: (_ for _ in ()).throw(OSError("no application")))
    dispatch(screen, Request(UiCommand.OPEN, index=1))
    assert "could not open lease.md" in screen.read()


# --- sync and status --------------------------------------------------------

def test_sync_reports_what_changed(screen, monkeypatch):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index",
                        lambda s: SyncStats(added=2, chunks_total=30))
    dispatch(screen, Request(UiCommand.SYNC))
    assert "+2 added" in screen.read()
    assert screen.session.synced is True


def test_sync_says_up_to_date_when_nothing_changed(screen, monkeypatch):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index",
                        lambda s: SyncStats(chunks_total=30, files_total=4))
    dispatch(screen, Request(UiCommand.SYNC))
    assert "up to date" in screen.read()


def test_a_quiet_sync_with_no_changes_prints_nothing(screen, monkeypatch):
    """The startup sync must not push the banner off the screen."""
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index", lambda s: SyncStats(chunks_total=30))
    ui_module._do_sync(screen, quiet=True)
    assert screen.read().strip() == ""


def test_a_quiet_sync_still_announces_an_upgrade(screen, monkeypatch):
    """Quiet mode exists to protect the banner, not to hide a minute of work.

    The startup sync is normally silent, but an upgrade re-embeds everything —
    so staying quiet through it would look exactly like a hang.
    """
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index",
                        lambda s: SyncStats(chunks_total=30, upgraded=True))

    ui_module._do_sync(screen, quiet=True)

    assert "index format changed" in screen.read()


def test_an_upgrade_names_files_it_could_not_keep(screen, monkeypatch):
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index", lambda s: SyncStats(
        chunks_total=30, upgraded=True, needs_unlock=["/src/statement.pdf"]))

    ui_module._do_sync(screen, quiet=True)

    assert "statement.pdf was unlocked before" in screen.read()


def test_a_broken_sync_reports_one_line_not_a_traceback(screen, monkeypatch):
    import sift_downloads.index as index
    from sift_downloads.config import ConfigError
    monkeypatch.setattr(index, "update_index",
                        lambda s: (_ for _ in ()).throw(ConfigError("no Ollama\nsecond line")))
    dispatch(screen, Request(UiCommand.SYNC))
    out = screen.read()
    assert "no Ollama" in out and "second line" not in out


def test_a_model_that_is_not_pulled_does_not_end_the_session(screen, monkeypatch):
    """Reproduced against a real Ollama: with the server up but the model not
    pulled, litellm raises APIConnectionError — which is neither ConfigError nor
    IndexProblem, so it escaped `run()`'s startup sync as a traceback. That is
    the wall of litellm errors doctor.py exists to prevent, on the one path a
    declined setup offer lands in. cli._quiet_sync already handled it; this did not.
    """
    import sift_downloads.index as index
    monkeypatch.setattr(index, "update_index",
                        lambda s: (_ for _ in ()).throw(
                            RuntimeError("OllamaException - 404 Not Found\n"
                                         "Provider List: https://litellm...")))
    ui_module._do_sync(screen, quiet=True)          # must not raise
    out = screen.read()
    assert "404 Not Found" in out
    # One line, like the ConfigError branch above it. Measured on a real cold
    # run: litellm's message is five lines of links and debug advice.
    assert "Provider List" not in out


def test_status_without_an_index_says_how_to_build_one(screen):
    dispatch(screen, Request(UiCommand.STATUS))
    assert "/sync" in screen.read()


def test_status_reports_chunks_and_location(screen, embedder, make_file):
    from sift_downloads.index import update_index
    make_file("notes.md", "some text")
    update_index(embedder=embedder)
    dispatch(screen, Request(UiCommand.STATUS))
    out = screen.read()
    assert "chunks from 1 files" in out
    assert str(screen.session.settings.data_dir) in out


def test_status_mentions_files_with_no_readable_text(screen, embedder, make_pdf):
    from sift_downloads.index import update_index
    make_pdf("scan.pdf", text="")
    update_index(embedder=embedder)
    dispatch(screen, Request(UiCommand.STATUS))
    assert "no readable text" in screen.read()


def test_status_reports_a_corrupt_index_without_crashing(screen, embedder, make_file):
    from sift_downloads.index import update_index
    make_file("notes.md", "text")
    update_index(embedder=embedder)
    screen.session.settings.index_path.write_bytes(b"garbage")
    dispatch(screen, Request(UiCommand.STATUS))
    assert screen.read().strip()


# --- the first-run offer ----------------------------------------------------
#
# `sift` with no models does nothing useful, and the person most likely to be
# in that state is the one who just installed it. The offer is the one place
# the session asks a question before the prompt appears, so what it must never
# do is get in the way: a decline, a failure and a Ctrl-C all have to land back
# at a working session.

@pytest.fixture
def offer(monkeypatch):
    """Stub the plan, the pull and the question. Records what happened."""
    import sift_downloads.setup as setup

    box = {"plan": setup.SetupPlan(to_pull=["ollama/nomic-embed-text"]),
           "answer": "y", "pulled": [], "asked": 0}

    def fake_pull(model, on_progress):
        box["pulled"].append(model)
        on_progress(setup.PullProgress(model, "pulling", 1, 2))

    def fake_ask(*args, **kwargs):
        box["asked"] += 1
        return box["answer"]

    monkeypatch.setattr(setup, "plan_setup", lambda *a, **k: box["plan"])
    monkeypatch.setattr(setup, "pull_model", fake_pull)
    monkeypatch.setattr(ui_module, "read_line", fake_ask)
    return box


def test_a_first_run_offers_to_download_what_is_missing(screen, offer):
    ui_module._offer_setup(screen)
    assert offer["pulled"] == ["ollama/nomic-embed-text"]
    assert "ollama/nomic-embed-text" in screen.read()


def test_nothing_is_asked_when_the_models_are_already_there(screen, offer):
    import sift_downloads.setup as setup
    offer["plan"] = setup.SetupPlan()
    ui_module._offer_setup(screen)
    assert offer["asked"] == 0
    assert screen.read().strip() == ""


def test_declining_downloads_nothing_and_says_nothing_more(screen, offer):
    offer["answer"] = ""
    ui_module._offer_setup(screen)
    assert offer["pulled"] == []


def test_a_missing_ollama_is_explained_instead_of_offered(screen, offer):
    """sift cannot install Ollama, so there is nothing to say yes to."""
    import sift_downloads.setup as setup
    offer["plan"] = setup.SetupPlan(to_pull=["ollama/nomic-embed-text"], server_up=False,
                                    install_hint="brew install ollama")
    ui_module._offer_setup(screen)
    assert offer["asked"] == 0
    assert "brew install ollama" in screen.read()


def test_a_failed_download_reports_one_line_and_returns_to_the_session(screen, offer,
                                                                       monkeypatch):
    import sift_downloads.setup as setup

    def boom(model, on_progress):
        raise setup.SetupError("could not pull ollama/x: file does not exist")

    monkeypatch.setattr(setup, "pull_model", boom)
    ui_module._offer_setup(screen)
    assert "file does not exist" in screen.read()


def test_ctrl_c_during_the_download_returns_to_the_session(screen, offer, monkeypatch):
    import sift_downloads.setup as setup

    def interrupted(model, on_progress):
        raise KeyboardInterrupt

    monkeypatch.setattr(setup, "pull_model", interrupted)
    ui_module._offer_setup(screen)          # must not raise
    assert "resume" in screen.read().lower()


def test_the_session_offers_setup_before_it_tries_to_sync(monkeypatch):
    """Order is the whole point: the sync is what fails without models.

    Both of those run BEFORE the Application starts, which is why read_line and
    the plain rich console survive untouched for that phase.
    """
    from sift_downloads.config import get_settings
    done: list[str] = []
    monkeypatch.setattr(ui_module, "_offer_setup", lambda ui: done.append("offer"))
    monkeypatch.setattr(ui_module, "_do_sync", lambda ui, quiet=False: done.append("sync"))
    monkeypatch.setattr(ui_module, "_start_terminal",
                        lambda ui: done.append("terminal") or 0)

    assert ui_module.run(get_settings()) == 0
    assert done == ["offer", "sync", "terminal"]


def test_the_session_really_reaches_the_terminal_module(monkeypatch):
    """The one function whose entire job is the import direction, and the only
    place it is executed. Every other test stubs `_start_terminal` out, so
    renaming `run_session` in terminal.py used to leave the whole suite green
    while every real session died at startup on the ImportError.
    """
    from sift_downloads import terminal as terminal_module

    seen = []
    monkeypatch.setattr(terminal_module, "run_session",
                        lambda ui: seen.append(ui) or 3)
    ui = Ui(Session())
    assert ui_module._start_terminal(ui) == 3
    assert seen == [ui], "the caller's Ui was not the one handed over"
