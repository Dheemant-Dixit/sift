"""
Tests for the interactive session's logic and rendering.

Everything here runs headless — no pty, no terminal, so it works in CI and on
Windows. The event loop itself isn't tested here; what's tested is the part
that decides what a typed line MEANS and what the result looks like, which is
where the bugs actually live.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from sift_downloads.session import HELP, Session, UiCommand, parse
from sift_downloads.ui import Ui, _clip


# --- parsing ---------------------------------------------------------------

def test_bare_text_searches():
    """Finding a file is the common case and should cost no syntax."""
    request = parse("rental agreement")
    assert request.command == UiCommand.FIND
    assert request.argument == "rental agreement"


def test_question_mark_prefix_asks():
    request = parse("?what is my notice period")
    assert request.command == UiCommand.ASK
    assert request.argument == "what is my notice period"


def test_lone_question_mark_is_help_not_an_empty_question():
    assert parse("/?").command == UiCommand.HELP


def test_slash_ask():
    request = parse("/ask how much was the deposit")
    assert request.command == UiCommand.ASK
    assert request.argument == "how much was the deposit"


def test_ask_without_a_question_is_an_error():
    request = parse("/ask")
    assert request.command == UiCommand.ERROR
    assert "question" in request.message


@pytest.mark.parametrize("line", ["quit", "exit", ":q", "/quit", "/q", "/exit"])
def test_quit_words(line):
    assert parse(line).command == UiCommand.QUIT


@pytest.mark.parametrize("line,expected", [
    ("/help", UiCommand.HELP), ("/h", UiCommand.HELP),
    ("/status", UiCommand.STATUS), ("/s", UiCommand.STATUS),
    ("/sync", UiCommand.SYNC), ("/reindex", UiCommand.SYNC),
    ("/find x", UiCommand.FIND), ("/f x", UiCommand.FIND),
    ("/search x", UiCommand.FIND),
])
def test_aliases(line, expected):
    assert parse(line).command == expected


def test_empty_line_does_nothing():
    assert parse("").command == UiCommand.NOTHING
    assert parse("    ").command == UiCommand.NOTHING


def test_unknown_command_is_reported():
    request = parse("/frobnicate")
    assert request.command == UiCommand.ERROR
    assert "/frobnicate" in request.message


def test_open_takes_a_number():
    request = parse("/open 2")
    assert request.command == UiCommand.OPEN
    assert request.index == 2


def test_reveal_takes_a_number():
    assert parse("/reveal 3").index == 3


def test_open_without_a_number_is_an_error():
    assert parse("/open").command == UiCommand.ERROR


def test_open_with_nonsense_is_an_error():
    request = parse("/open banana")
    assert request.command == UiCommand.ERROR
    assert "not a result number" in request.message


def test_find_recent_flag():
    for line in ("/find --recent tax", "/find -r tax"):
        request = parse(line)
        assert request.command == UiCommand.FIND
        assert request.argument == "tax"
        assert request.recent is True


def test_find_without_the_flag_is_not_recent():
    assert parse("/find tax").recent is False


def test_a_query_that_merely_starts_with_r_is_not_a_flag():
    """'-r' must be the flag, not any word beginning with r."""
    request = parse("/find receipts")
    assert request.argument == "receipts"
    assert request.recent is False


# --- resolving a result number ---------------------------------------------

class _Hit:
    def __init__(self, name):
        self.path = Path(f"/downloads/{name}")


def test_resolve_before_searching_explains_itself():
    path, problem = Session().resolve(1)
    assert path is None
    assert "search for something first" in problem


def test_resolve_picks_the_right_file():
    session = Session()
    session.last_hits = [_Hit("a.pdf"), _Hit("b.pdf")]
    path, problem = session.resolve(2)
    assert problem == ""
    assert path.name == "b.pdf"


def test_resolve_is_one_based_and_bounded():
    session = Session()
    session.last_hits = [_Hit("a.pdf")]
    assert session.resolve(0)[0] is None
    assert session.resolve(2)[0] is None
    assert "1 result" in session.resolve(2)[1]


def test_history_is_recorded_for_later_use():
    """v1 doesn't prompt with history, but it records it so v2 can."""
    session = Session()
    session.remember("user", "find my lease")
    session.remember("assistant", "here it is")
    assert session.history == [("user", "find my lease"), ("assistant", "here it is")]


def test_help_covers_every_documented_command():
    documented = " ".join(keys for keys, _ in HELP)
    for command in ("/ask", "/open", "/reveal", "/sync", "/status", "/help", "/quit"):
        assert command in documented


# --- rendering -------------------------------------------------------------

def test_clip_collapses_whitespace_and_truncates():
    assert _clip("a   b\n\nc", 40) == "a b c"
    clipped = _clip("x" * 100, 10)
    assert len(clipped) == 10
    assert clipped.endswith("…")


def test_clip_leaves_short_text_alone():
    assert _clip("short", 40) == "short"


class _FullHit:
    def __init__(self, name, score, matched_on, snippet="", note="", dupes=()):
        self.path = Path(f"/downloads/{name}")
        self.name = name
        self.score = score
        self.matched_on = matched_on
        self.snippet = snippet
        self.note = note
        self.duplicates = list(dupes)
        self.size = 2048
        self.modified = 0.0
        self.indexed = True


def _render(hits, width=72) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, width=width, no_color=True, highlight=False)
    Ui(Session(), console=console).results(hits, "query")
    return buffer.getvalue()


def test_scores_line_up_in_a_column_across_marker_types():
    """The bug this catches: markers of different widths stagger the scores."""
    out = _render([
        _FullHit("a.pdf", 0.98, "filename"),
        _FullHit("b.pdf", 0.64, "content"),
        _FullHit("c.pdf", 0.71, "both"),
    ])
    columns = [line.index("0.") for line in out.splitlines() if "0." in line]
    assert len(columns) == 3
    assert len(set(columns)) == 1, f"scores not aligned: {columns}"


def test_no_result_line_exceeds_the_terminal_width():
    """A wrapped line reflows to column 0 and wrecks the indentation."""
    out = _render([
        _FullHit("a-very-long-file-name-" * 4 + ".pdf", 0.9, "content",
                 snippet="lorem ipsum " * 40),
    ], width=72)
    too_long = [line for line in out.splitlines() if len(line) > 72]
    assert not too_long, too_long


def test_unreadable_file_shows_its_reason_instead_of_a_snippet():
    out = _render([_FullHit("scan.pdf", 0.98, "filename",
                            note="no extractable text (scanned or image-only?)")])
    assert "no extractable text" in out


def test_duplicates_are_mentioned():
    out = _render([_FullHit("s.pdf", 0.9, "content", snippet="hi",
                            dupes=["/downloads/s (1).pdf"])])
    assert "+1 copy" in out
    assert "s (1).pdf" in out


def test_empty_results_suggest_what_to_do():
    out = _render([])
    assert "nothing matched" in out
    assert "/sync" in out


@pytest.mark.parametrize("line", ["?", "? "])
def test_a_lone_question_mark_asks_for_help(line):
    """Not a search for a punctuation mark."""
    assert parse(line).command == UiCommand.HELP


# --- how a bare `sift` is routed -------------------------------------------

def test_bare_invocation_starts_the_session():
    from sift_downloads.cli import with_default_command as route
    assert route([]) == ["ui"]
    assert route(["--source", "/x"]) == ["ui", "--source", "/x"]


def test_explicit_commands_are_left_alone():
    from sift_downloads.cli import with_default_command as route
    assert route(["find", "tax"]) == ["find", "tax"]
    assert route(["--help"]) == ["--help"]
    assert route(["--version"]) == ["--version"]


def test_a_typo_is_left_for_argparse_to_explain():
    """`sift fnid x` should list valid choices, not fail inside `ui`."""
    from sift_downloads.cli import with_default_command as route
    assert route(["fnid", "x"]) == ["fnid", "x"]


def test_non_interactive_stdin_falls_through_to_help():
    """A bare `sift` in a script has nobody to type into a prompt."""
    from sift_downloads.cli import with_default_command as route
    assert route([], interactive=False) == []
    assert route(["--source", "/x"], interactive=False) == ["--source", "/x"]
