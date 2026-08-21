"""
The interactive session's terminal half.

Two things are tested here that the old ui.read_line never was. The pure pieces
- the rich->ANSI bridge and the live region - are plain objects with no
Application in sight. The Application itself is driven headlessly with
prompt_toolkit's own create_pipe_input/DummyOutput, so key bindings are real
tests rather than a coverage gap.
"""
from __future__ import annotations

import asyncio
import re

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from sift_downloads.session import Session
from sift_downloads.terminal import Cancelled, LiveRegion, Scrollback, TerminalSession
from sift_downloads.ui import Ui

# --- the rich -> ANSI bridge ------------------------------------------------

def test_a_complete_line_is_committed_and_a_partial_one_is_held():
    lines = []
    sb = Scrollback(lines.append)
    sb.write("first\nsec")
    assert lines == ["first"]
    sb.write("ond\n")
    assert lines == ["first", "second"]


def test_a_partial_line_is_committed_on_flush():
    lines = []
    sb = Scrollback(lines.append)
    sb.write("no newline")
    assert lines == []
    sb.flush()
    assert lines == ["no newline"]


def test_flushing_twice_does_not_commit_an_empty_line():
    lines = []
    sb = Scrollback(lines.append)
    sb.write("x")
    sb.flush()
    sb.flush()
    assert lines == ["x"]


def test_write_returns_the_character_count_rich_expects():
    assert Scrollback(lambda line: None).write("hello") == 5


def test_riches_escape_sequences_survive_the_bridge():
    """This is the whole reason approach C exists. Writing rich's output through
    prompt_toolkit's stdout proxy replaces every \\x1b with '?' - the styling
    comes back as literal '?[2m' noise. Rendering to a string does not."""
    lines = []
    console = Console(file=Scrollback(lines.append), force_terminal=True,
                      width=60, highlight=False)
    console.print("  [dim]— reading from —[/dim]")
    assert "\x1b[" in lines[0]
    assert "?[" not in lines[0]


def test_the_bridge_hands_prompt_toolkit_real_styles_not_text():
    lines = []
    console = Console(file=Scrollback(lines.append), force_terminal=True,
                      width=60, highlight=False)
    console.print("  [dim]— reading from —[/dim]")
    fragments = ANSI(lines[0]).__pt_formatted_text__()
    assert any("dim" in style for style, _ in fragments), \
        "the dim survived as a style, not as printable text"
    assert "\x1b" not in "".join(text for _, text in fragments)


def test_the_bridge_is_not_a_tty_so_rich_never_tries_to_animate():
    assert Scrollback(lambda line: None).isatty() is False


def test_a_bare_newline_commits_a_blank_line():
    """`ui.console.print()` with no arguments is how ui.py spaces its output, and
    it arrives here as one newline. Swallowing it would run the answer straight
    into the sources block above it."""
    lines = []
    sb = Scrollback(lines.append)
    sb.write("text\n")
    sb.write("\n")
    sb.write("more\n")
    assert lines == ["text", "", "more"]


# --- the live region --------------------------------------------------------


def a_region(cancelled=False):
    """A LiveRegion with its three collaborators replaced by lists."""
    committed: list[str] = []
    repaints: list[int] = []
    region = LiveRegion(committed.append, lambda: repaints.append(1),
                        lambda: cancelled)
    return region, committed, repaints


def test_a_finished_line_leaves_the_region_for_scrollback():
    region, committed, _ = a_region()
    region.append("Notice period: 60 days\n")
    assert committed == ["  Notice period: 60 days"]
    assert region.tail == ""


def test_an_unfinished_line_stays_in_the_region():
    region, committed, _ = a_region()
    region.append("Security deposit is one hun")
    assert committed == []
    assert region.tail == "Security deposit is one hun"


def test_the_tail_is_committed_when_the_answer_ends():
    region, committed, _ = a_region()
    region.append("the last words")
    region.flush()
    assert committed == ["  the last words"]
    assert region.tail == ""


def test_committed_lines_keep_the_two_space_indent():
    """Same indent Padding gave them at ui.py:252, so the answer still lines up
    under the sources block above it."""
    region, committed, _ = a_region()
    region.append("a\nb\n")
    assert committed == ["  a", "  b"]


def test_a_blank_line_in_the_answer_is_kept():
    region, committed, _ = a_region()
    region.append("para one\n\npara two\n")
    assert committed == ["  para one", "", "  para two"]


def test_an_endless_paragraph_is_committed_before_it_pushes_the_box_away():
    """A model that never emits a newline would otherwise grow the region without
    bound. Cut at a space, which is what wrapping does to you anyway."""
    region, committed, _ = a_region()
    region.append("word " * 80)
    assert committed, "nothing was committed, so the region grew unbounded"
    assert len(region.tail) < LiveRegion.TAIL_MAX_CHARS
    assert not committed[0].endswith("wor"), "cut at a space, not mid-word"


def test_a_paragraph_with_no_spaces_at_all_is_still_committed():
    """`rfind` returns -1 when there is nothing to break on. Treating that as
    "do nothing" lets the region grow forever, which is the exact failure
    TAIL_MAX_CHARS exists to prevent."""
    region, committed, _ = a_region()
    region.append("y" * 400)
    assert committed
    assert len(region.tail) <= LiveRegion.TAIL_MAX_CHARS


def test_a_leading_space_does_not_wedge_the_cut():
    """A space at index 0 removes nothing when cut on, and `rfind` reports "not
    found" as -1. Conflating the two stops the tail shrinking ever again."""
    region, committed, _ = a_region()
    region.append(" " + "y" * 400)
    region.append(" and more text after that")
    assert committed
    assert len(region.tail) <= LiveRegion.TAIL_MAX_CHARS


def test_the_streaming_tail_is_actually_drawn():
    """height() counts rows; only render() puts the text on screen. Dropping the
    tail from render() passes every row-count test in this file."""
    region, _, _ = a_region()
    region.append("Security deposit is one hun")
    assert "Security deposit is one hun" in region.render()


def test_appending_asks_for_a_repaint():
    region, _, repaints = a_region()
    region.append("x")
    assert repaints


def test_a_status_asks_for_a_repaint():
    region, _, repaints = a_region()
    region.show("thinking...")
    assert repaints


def test_appending_after_ctrl_c_stops_the_worker():
    """The only place a worker can notice cancellation is between tokens, and the
    region is the only thing it touches there."""
    region, _, _ = a_region(cancelled=True)
    with pytest.raises(Cancelled):
        region.append("a token")


def test_a_cancelled_region_commits_nothing():
    committed: list[str] = []
    region = LiveRegion(committed.append, lambda: None, lambda: True)
    with pytest.raises(Cancelled):
        region.append("a whole line\n")
    assert committed == []


def test_the_region_is_empty_when_nothing_is_happening():
    region, _, _ = a_region()
    assert region.render() == ""
    assert region.height(80) == 0


def test_the_status_line_carries_a_spinner_and_the_seconds():
    region, _, _ = a_region()
    region.show("thinking...")
    out = region.render()
    assert "thinking..." in out
    assert any(frame in out for frame in LiveRegion.FRAMES)
    assert "0s" in out, "the elapsed counter reads 4s, not 0:00:04"


def test_clearing_the_status_empties_the_region():
    region, _, _ = a_region()
    region.show("thinking...")
    region.show("")
    assert region.render() == ""


def test_the_region_is_one_row_for_a_status_and_two_with_a_tail():
    region, _, _ = a_region()
    region.show("thinking...")
    assert region.height(80) == 1
    region.append("short")
    assert region.height(80) == 2


def test_a_wrapped_tail_is_counted_in_rows_not_lines():
    """The Application is inline, so it has to be told its own height. Counting
    the tail as one row would draw the box over the answer."""
    region, _, _ = a_region()
    region.append("x" * 100)
    assert region.height(40) == 3


def test_the_spinner_advances_between_repaints():
    region, _, _ = a_region()
    region.show("thinking...")
    first = region.render()
    region.tick()
    assert region.render() != first


# --- the Application --------------------------------------------------------


def a_terminal():
    """A TerminalSession, plus the lines it would have painted.

    TerminalSession replaces ui.console with one rendering through Scrollback,
    so there is no buffer left to read - asserting on a Console handed to Ui()
    would go permanently empty and the tests would pass by vacuity. `painted` is
    the real path: rich renders to a string, Scrollback cuts it into lines, and
    paint() would put each one on the screen.
    """
    painted: list[str] = []
    terminal = TerminalSession(Ui(Session()))
    terminal.paint = painted.append
    return terminal, painted


def screen_text(painted: list[str]) -> str:
    """What the user would have seen, with the styling taken back off."""
    return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(painted))


def drive(terminal, keys, extra=None):
    """Run the Application headlessly, feeding it `keys`, and return its result."""
    async def main():
        with create_pipe_input() as pipe, create_app_session(
                input=pipe, output=DummyOutput()):
            app = terminal.build()

            async def typist():
                await asyncio.sleep(0.02)
                for chunk in keys:
                    pipe.send_text(chunk)
                    await asyncio.sleep(0.02)
                if extra is not None:
                    await extra(terminal)
                pipe.send_text("\x04")          # ctrl-d always ends the test
                await asyncio.sleep(0.05)

            task = asyncio.create_task(typist())
            result = await app.run_async()
            await task
            return result

    return asyncio.run(main())


def test_ctrl_d_on_an_empty_box_ends_the_session():
    terminal, _ = a_terminal()
    assert drive(terminal, []) is None


def test_a_typed_line_reaches_the_runner_on_enter():
    terminal, _ = a_terminal()
    seen = []
    terminal.start = seen.append
    drive(terminal, ["notice period\r"])
    assert seen == ["notice period"]
    assert terminal.runner.busy is True


def test_enter_clears_the_box_so_the_next_question_starts_empty():
    terminal, _ = a_terminal()
    terminal.start = lambda text: None
    drive(terminal, ["notice period\r"])
    assert terminal.area.text == ""


def test_a_second_line_is_shown_as_queued_rather_than_run():
    terminal, _ = a_terminal()
    started = []
    terminal.start = started.append
    drive(terminal, ["first\r", "second\r"])
    assert started == ["first"], "the second line must not start a second call"
    assert terminal.queued_line == "second"
    assert "second" in terminal.render_queued()


def test_a_third_line_is_refused_and_stays_in_the_box():
    """A refusal must not eat your typing. Clearing the box here would also hide
    the bug: ctrl-d only quits an empty box, so the box staying full is exactly
    why this test has to empty it by hand before drive() sends ctrl-d."""
    terminal, painted = a_terminal()
    terminal.start = lambda text: None
    survived = []

    async def look_then_clear(term):
        survived.append(term.area.text)
        term.area.text = ""

    drive(terminal, ["first\r", "second\r", "third\r"], extra=look_then_clear)
    assert terminal.queued_line == "second"
    assert "one at a time" in screen_text(painted)
    assert survived == ["third"], "a refused line must still be there to re-send"


def test_ctrl_c_while_idle_with_text_clears_the_box_and_does_not_quit():
    terminal, _ = a_terminal()
    result = drive(terminal, ["half a quest", "\x03"])
    assert terminal.area.text == ""
    assert result is None, "ctrl-c must not have ended it - ctrl-d did"


def test_ctrl_c_on_an_empty_idle_box_says_how_to_quit():
    terminal, painted = a_terminal()
    drive(terminal, ["\x03"])
    assert "ctrl-d to quit" in screen_text(painted)


def test_ctrl_c_while_working_cancels_and_drops_the_queue():
    terminal, painted = a_terminal()
    terminal.start = lambda text: None
    drive(terminal, ["first\r", "second\r", "\x03"])
    assert terminal.runner.cancelled is True
    assert terminal.queued_line == ""
    assert "cancelled" in screen_text(painted)


def test_ctrl_d_while_working_still_leaves():
    terminal, _ = a_terminal()
    terminal.start = lambda text: None
    drive(terminal, ["first\r"])       # drive() sends ctrl-d after
    assert terminal.runner.busy is True


def test_the_submitted_line_is_echoed_into_scrollback():
    """The box no longer erases itself, but Enter clears the text out of it - so
    what you asked still has to land in scrollback above the answer."""
    terminal, painted = a_terminal()
    terminal.start = lambda text: None
    drive(terminal, ["notice period\r"])
    assert "notice period" in screen_text(painted)


def test_a_committed_line_is_painted_while_the_box_stays_up():
    terminal, painted = a_terminal()

    async def commit_from_a_worker(term):
        await asyncio.get_running_loop().run_in_executor(
            None, term.commit, "\x1b[2m— reading from —\x1b[0m")

    drive(terminal, [], extra=commit_from_a_worker)
    assert painted == ["\x1b[2m— reading from —\x1b[0m"]


def test_the_ui_stops_writing_at_the_terminal_once_the_box_is_up():
    """Approach C in one assertion. If Ui keeps a Console of its own, ui.note
    writes past prompt_toolkit's renderer and corrupts the box it is drawing."""
    terminal, painted = a_terminal()
    terminal.ui.note("a note")
    terminal.ui.console.file.flush()
    assert "a note" in screen_text(painted)
    assert isinstance(terminal.ui.console.file, Scrollback)


def test_a_worker_commit_does_not_return_until_the_line_is_painted():
    """The blocking .result() in commit()'s worker path, pinned directly.

    Asserting that commits merely *arrive* in order proves nothing:
    run_coroutine_threadsafe is FIFO, so three fire-and-forget commits land in
    order too. What the wait actually buys is that the worker cannot run ahead
    of the renderer - so that is what this asserts. Drop the .result() and
    `painted` is still empty when commit() returns.
    """
    terminal, painted = a_terminal()
    seen = []

    async def commit_then_look(term):
        def worker():
            term.commit("sources")
            seen.append(list(painted))
        await asyncio.get_running_loop().run_in_executor(None, worker)

    drive(terminal, [], extra=commit_then_look)
    assert seen == [["sources"]], "commit() returned before the paint landed"
