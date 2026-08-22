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
import threading

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth
from rich.console import Console

from sift_downloads import terminal as terminal_module
from sift_downloads.config import ConfigError
from sift_downloads.session import Session
from sift_downloads.terminal import (
    REDRAW_INTERVAL,
    SPINNER_INTERVAL,
    Cancelled,
    LiveRegion,
    Scrollback,
    TerminalSession,
    run_session,
)
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


def a_region(cancelled=False, width=80):
    """A LiveRegion with its four collaborators replaced by lists and a width."""
    committed: list[str] = []
    repaints: list[int] = []
    region = LiveRegion(committed.append, lambda: repaints.append(1),
                        lambda: cancelled, lambda: width)
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


def test_a_capped_paragraph_breaks_where_a_wrap_would_have():
    """The cap used to cut at its own character count, which left a short
    ragged row in the middle of a paragraph - and a model's paragraph is over
    the cap most of the time, so it fired on nearly every answer."""
    words = ("the quick brown fox jumps over the lazy dog "
             * 20).strip()
    region, committed, _ = a_region(width=40)
    region.append(words)
    region.flush()
    assert len(committed) > 1
    # Every row full to within one word of the screen, except the last.
    for row in committed[:-1]:
        assert len(row) > 40 - 12, f"ragged row mid-paragraph: {row!r}"
    assert " ".join(row.strip() for row in committed) == words


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
    region = LiveRegion(committed.append, lambda: None, lambda: True,
                        lambda: 80)
    with pytest.raises(Cancelled):
        region.append("a whole line\n")
    assert committed == []


def test_the_region_is_empty_when_nothing_is_happening():
    region, _, _ = a_region()
    assert region.render() == ""
    assert region.height() == 0


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
    assert region.height() == 1
    region.append("short")
    assert region.height() == 2


def test_a_wrapped_tail_is_counted_in_rows_not_lines():
    """The Application is inline, so it has to be told its own height. Counting
    the tail as one row would draw the box over the answer."""
    region, _, _ = a_region(width=40)
    region.append("x" * 100)
    assert region.height() == 3


def test_a_long_line_is_wrapped_before_the_terminal_gets_it():
    """The terminal wraps too, but it wraps to column 0 and cuts wherever the
    edge falls - so the indent is lost and words split in half. Measured on an
    80-column terminal before this fix: "either part" / "y may end"."""
    words = "the quick brown fox jumps over the lazy dog and keeps on going"
    region, committed, _ = a_region(width=40)
    region.append(words + "\n")
    assert len(committed) > 1, "one row means the terminal did the wrapping"
    assert all(row.startswith("  ") for row in committed), "indent on every row"
    assert all(len(row) <= 40 for row in committed), "a row wider than the screen"
    # Rejoining proves both halves at once: no character was dropped, and no
    # word was cut, because a cut word could not be rejoined by a space.
    assert " ".join(row.strip() for row in committed) == words


def test_a_wide_character_answer_wraps_by_columns_not_code_points():
    """A CJK ideograph or emoji is one code point and TWO terminal columns.
    Measuring with len() lets a row through at twice its real width, so the
    terminal wraps it after all - to column 0, cutting wherever the edge
    falls, losing the indent - the exact defect 3f79ac5 was written to fix,
    silently un-fixed for any non-Latin answer. sift indexes whatever is in
    the user's Downloads folder, so this is a realistic corpus."""
    words = "通知期間は六十日です。" * 8
    region, committed, _ = a_region(width=40)
    region.append(words)
    region.flush()
    columns = 40 - len(LiveRegion.INDENT)
    assert len(committed) > 1, "one row means len() let a too-wide row through"
    for row in committed:
        text = row[len(LiveRegion.INDENT):]
        drawn = sum(get_cwidth(ch) for ch in text)
        assert drawn <= columns, f"row is {drawn} columns wide, budget is {columns}"
    assert "".join(row[len(LiveRegion.INDENT):] for row in committed) == words


def test_every_row_the_region_reports_is_a_row_it_draws():
    """height() and render() used to compute the shape separately, and the
    Window does not wrap - so it clipped the tail at the screen edge and padded
    the rest with blanks. Measured mid-answer: 4 rows reserved, 1 drawn."""
    region, _, _ = a_region(width=40)
    region.show("reading your files")
    region.append("x " * 60)
    drawn = region.render().split("\n")
    assert region.height() == len(drawn)
    # The status row carries escapes, so only the tail rows can be measured.
    assert all(len(row) <= 40 for row in drawn[1:]), "a row wider than the screen"


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


# Generous: the whole file runs in ~2s, and no single drive() needs more than
# the ~0.3s of typing it is given. It exists so a regression FAILS instead of
# hanging - an Application that never exits freezes pytest with no output at
# all, which is how this task lost an afternoon once already.
DRIVE_TIMEOUT = 3.0


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
            try:
                result = await asyncio.wait_for(app.run_async(),
                                                timeout=DRIVE_TIMEOUT)
            except asyncio.TimeoutError:
                task.cancel()
                raise AssertionError(
                    f"the Application was still running {DRIVE_TIMEOUT}s after "
                    f"the last key. The usual cause is the closing ctrl-d not "
                    f"reaching on_eof: it only quits an EMPTY box, and the box "
                    f"holds {terminal.area.text!r}."
                ) from None
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
    """The one behaviour this whole change exists to make: ctrl-c stopped being
    an exit. It has to be observed WHILE the app runs. drive()'s return value
    cannot see it - ctrl-c and ctrl-d both exit with result=None, so `result is
    None` is produced by the bug and by the fix alike.
    """
    terminal, _ = a_terminal()
    alive = []

    async def look(term):
        alive.append(term.app.is_running)

    drive(terminal, ["half a quest", "\x03"], extra=look)
    assert alive == [True], "ctrl-c must not have ended the session"
    assert terminal.area.text == ""


def test_ctrl_c_on_an_empty_idle_box_says_how_to_quit():
    terminal, painted = a_terminal()
    drive(terminal, ["\x03"])
    assert "ctrl-d to quit" in screen_text(painted)


def test_ctrl_c_while_working_cancels_and_drops_the_queue():
    """`cancelled` is read WHILE the app runs, not from the wreckage afterwards.

    drive() sends ctrl-d after every test, and ctrl-d also stops the running
    line - so a flag read at the end of the drive is set by ctrl-d whether or
    not ctrl-c ever touched it, and the single most important cancellation
    assertion in this file would go on passing with ctrl-c gutted. Same shape
    as `alive` in test_ctrl_c_while_idle_with_text_clears_the_box_and_does_not_quit.
    """
    terminal, painted = a_terminal()
    terminal.start = lambda text: None
    cancelled = []

    async def look(term):
        cancelled.append(term.runner.cancelled)

    drive(terminal, ["first\r", "second\r", "\x03"], extra=look)
    assert cancelled == [True], "ctrl-c did not stop the running line"
    assert terminal.queued_line == ""
    assert "cancelled" in screen_text(painted)


def test_ctrl_d_while_working_still_leaves():
    terminal, _ = a_terminal()
    terminal.start = lambda text: None
    drive(terminal, ["first\r"])       # drive() sends ctrl-d after
    assert terminal.runner.busy is True


def test_the_submitted_line_is_echoed_into_scrollback():
    """The box no longer erases itself, but Enter clears the text out of it - so
    what you asked still has to land in scrollback above the answer. Stubs
    dispatch, not start - the echo lives inside start() now (see the next
    test), so stubbing start() away would stub away the thing under test."""
    terminal, painted = a_terminal()
    terminal.dispatch = lambda request: True
    drive(terminal, ["notice period\r"])
    assert "notice period" in screen_text(painted)


def test_a_queued_lines_echo_waits_until_it_actually_starts():
    """Echoing at QUEUE time put "> question B" in scrollback while answer A
    was still streaming underneath it - A's remaining rows then landed under a
    prompt for a question nobody had started answering yet, with nothing to
    mark them as A's rather than B's. start() is the one place both the RUN
    path (on_enter) and the dequeue path (_work's finally) actually begin a
    line, so that is where the echo has to live."""
    terminal, painted = a_terminal()
    release = threading.Event()
    mid_queue_snapshot = []

    def held_dispatch(request):
        if request.argument == "first":
            release.wait(timeout=2)
        return True
    terminal.dispatch = held_dispatch

    async def peek_while_queued_then_release(term):
        await asyncio.sleep(0.05)
        assert term.queued_line == "second", "the setup itself is wrong"
        mid_queue_snapshot.append(screen_text(painted))
        release.set()
        # Give _work's finally a chance to actually dequeue and start "second"
        # before drive()'s own ctrl-d tears the Application down - otherwise
        # this races the session ending against the very thing being checked.
        for _ in range(100):
            if term.queued_line == "":
                return
            await asyncio.sleep(0.01)
        raise AssertionError("\"second\" was never dequeued")

    drive(terminal, ["first\r", "second\r"], extra=peek_while_queued_then_release)

    assert "second" not in mid_queue_snapshot[0], \
        "the queued line's echo leaked into scrollback before it started"
    assert "second" in screen_text(painted), "it must still land once it runs"


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


def test_the_region_the_ui_writes_through_is_the_one_that_can_be_cancelled():
    """The console is only half the swap. Ui builds its own PlainRegion, whose
    append() just accumulates a string - so if TerminalSession leaves that one
    in place, ctrl-c during an answer says "cancelled", drops the queue, and the
    worker streams happily on to the end. Every signal the user gets is a lie.
    LiveRegion.append is the only place a worker notices a cancellation, so it
    has to be the region Ui reaches for.
    """
    terminal, _ = a_terminal()
    terminal.runner.cancelled = True
    with pytest.raises(Cancelled):
        terminal.ui.region.append("more of an answer nobody wants")


def test_the_app_stays_inline_and_throttles_its_repaints():
    """full_screen=False is not a preference. A full-screen Application swaps to
    the alternate buffer, so everything sift printed disappears the moment it
    exits - the opposite of what README.md:33 promises. min_redraw_interval is
    the repaint throttle: every token appended invalidates, and without it the
    renderer redraws once per token instead of 15 times a second.
    """
    terminal, _ = a_terminal()
    drive(terminal, [])
    assert terminal.app.full_screen is False, "the alternate buffer eats scrollback"
    assert terminal.app.min_redraw_interval == REDRAW_INTERVAL


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


def test_a_worker_running_its_own_loop_still_waits_for_the_paint():
    """`_on_the_loop` has to ask "am I on the APP's loop", not "is any loop
    running in this thread". A worker with a loop of its own answers yes to the
    loose question and takes the key-binding branch, which breaks both
    invariants at once: commit() returns before the line is painted, and the
    paint is scheduled on the worker's loop, so it runs on the worker thread
    past the renderer that is drawing the box.
    """
    terminal, painted = a_terminal()
    on_loop_thread = threading.current_thread().name
    seen = []
    paint_threads = []

    def record(line):
        paint_threads.append(threading.current_thread().name)
        painted.append(line)

    terminal.paint = record

    async def commit_from_a_worker_with_a_loop(term):
        def worker():
            async def inner():
                term.commit("sources")
                seen.append(list(painted))
            asyncio.run(inner())            # the worker's own event loop
        await asyncio.get_running_loop().run_in_executor(None, worker)

    drive(terminal, [], extra=commit_from_a_worker_with_a_loop)
    assert seen == [["sources"]], "commit() returned before the paint landed"
    assert paint_threads == [on_loop_thread], "the paint ran off the app's loop"


def test_a_key_press_during_worker_output_does_not_deadlock():
    """rich's Console holds its own lock for the whole of a print() call, down
    to the write into Scrollback - which is commit(). A worker's commit()
    BLOCKS on that lock while it waits for its paint to land on the loop, so a
    key binding that prints through the SAME Console blocks trying to acquire
    a lock the worker is holding while waiting for the very loop that key
    binding is running on to make progress. Deadlock. Measured, before
    PerThreadConsole existed: a worker printing a tight burst (what
    ui.results() does for a search) with a real ctrl-c landing a few
    milliseconds in wedged the session 3 runs out of 3, and not even
    drive()'s own asyncio.wait_for could catch it - a stalled event loop
    cannot run its own timeout callback either. Only something outside the
    loop entirely can, which is what the bounded join below is for, not
    decoration - the same reason test_a_commit_after_the_loop_closes... two
    tests down uses one.
    """
    terminal, painted = a_terminal()
    # Set by the worker's OWN code, not inferred from drive() returning:
    # asyncio.run()'s teardown does not join the default executor's thread,
    # so the burst can still be mid-flight after drive() has already come
    # back. Checking `painted` at that point would be exactly the kind of
    # repro-that-passes-by-doing-nothing this project has been burned by
    # before.
    dispatch_finished = threading.Event()

    def fake_dispatch(request):
        for i in range(200):                # tight: no gap between commits
            terminal.ui.note(f"result {i}")
        dispatch_finished.set()
        return True
    terminal.dispatch = fake_dispatch

    async def press_ctrl_c_mid_burst(term):
        await asyncio.sleep(0.003)
        term.on_interrupt()                 # the loop thread, same as a real key

    outcome: list[object] = []

    def run():
        try:
            outcome.append(drive(terminal, ["?long search query\r"],
                                 extra=press_ctrl_c_mid_burst))
        except BaseException as e:           # reported below, not swallowed
            outcome.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=8.0)

    assert not thread.is_alive(), \
        "deadlocked: a key press during worker output wedged the session"
    assert dispatch_finished.wait(timeout=3.0), \
        "the worker never finished - the race was not actually exercised"
    assert outcome and not isinstance(outcome[0], BaseException), outcome
    assert any("result 199" in p for p in painted), \
        "the burst finished but its last line never reached the screen"


def test_a_commit_after_the_loop_closes_still_paints_the_line():
    """A worker outlives the Application: ctrl-d while busy leaves one
    streaming, and nothing can kill a thread. Its next commit() therefore
    arrives at a loop that is closed. Handing that to run_coroutine_threadsafe
    raises RuntimeError('Event loop is closed') into the worker thread and the
    line is lost, so commit() falls back to painting directly - the same thing
    it already does before the app has started.
    """
    terminal, painted = a_terminal()
    dead = asyncio.new_event_loop()
    dead.close()
    terminal.loop = dead
    trouble = []

    def worker():
        try:
            terminal.commit("the last of the answer")
        except Exception as exc:            # catching it IS the assertion
            trouble.append(repr(exc))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive(), "commit() blocked forever on a dead loop"
    assert trouble == [], "commit() raised into the worker thread"
    assert painted == ["the last of the answer"]


def test_a_commit_does_not_hang_when_the_loop_has_stopped_but_not_closed():
    """asyncio.run()'s own teardown closes the loop only AFTER its main
    coroutine has already returned - there is a real window where the loop
    has stopped pumping callbacks but is_closed() still reads False. A
    commit() landing in that window used to submit to a loop nothing will
    ever run again and block on .result() forever, silently: is_closed()
    said "not closed yet", so the already-closed fallback above never fired.

    Reproduced directly, without relying on hitting asyncio.run()'s narrow
    internal window by luck: a loop is run to completion in its own thread
    and left open (never closed) - "stopped but not closed" exactly.
    """
    terminal, painted = a_terminal()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "the loop never stopped running"
    assert not loop.is_closed(), "the loop closed itself - this would not reproduce it"

    terminal.loop = loop
    done = threading.Event()

    def worker():
        terminal.commit("a line from a stopped loop")
        done.set()

    caller = threading.Thread(target=worker, daemon=True)
    caller.start()
    caller.join(timeout=5.0)
    assert done.is_set(), "commit() blocked forever on a stopped, unclosed loop"
    assert painted == ["a line from a stopped loop"]
    loop.close()



def test_a_commit_survives_the_loop_cancelling_its_paint(monkeypatch):
    """The third way a loop can fail to paint, and the one that killed the
    worker outright.

    commit() covers two of them: a loop already closed (paint straight to the
    terminal) and a loop stopped but not closed (the COMMIT_TIMEOUT above).
    Both assume the submission either runs or is ignored. There is a third:
    the loop is alive enough to ACCEPT the coroutine, and then asyncio.run()'s
    teardown cancels every pending task before it closes. `.result()` raises
    CancelledError, which is a BaseException - so it sailed past the
    `except TimeoutError` and out of commit(), out of ui.note(), and killed
    the worker thread mid-burst. The remaining lines were never painted and
    nothing said so: the exception surfaced in a run_in_executor future that
    the dying loop was no longer awaiting.

    Observed as a flake on the CI matrix rather than reasoned about: the
    worker died at commit 21 of 200 with the loop shutting down under it.
    Reproduced here without depending on that race - run_in_terminal is
    replaced by a paint that never completes, so the painter task is reliably
    pending when the cancellation arrives, exactly as asyncio.run() delivers
    it.
    """
    terminal, painted = a_terminal()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    terminal.loop = loop

    painting = threading.Event()

    async def never_finishes(_func):
        painting.set()
        await asyncio.Event().wait()        # pending, and cancellable

    monkeypatch.setattr(terminal_module, "run_in_terminal", never_finishes)

    done = threading.Event()

    def worker():
        terminal.commit("a line the loop will never paint")
        done.set()

    caller = threading.Thread(target=worker, daemon=True)
    caller.start()

    # Cancel only once the painter is genuinely pending. Cancelling before the
    # task exists would cancel nothing, commit() would fall through to the
    # 2.0s timeout instead, and the test would pass without ever exercising
    # cancellation at all.
    assert painting.wait(timeout=5.0), "the painter never started"
    loop.call_soon_threadsafe(
        lambda: [task.cancel() for task in asyncio.all_tasks(loop)])

    caller.join(timeout=5.0)
    assert done.is_set(), \
        "commit() raised instead of returning - the worker thread is dead"
    assert painted == ["a line the loop will never paint"], \
        "the cancelled line never reached the screen"

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2.0)
    loop.close()

# --- the worker -------------------------------------------------------------

async def wait_for_idle(terminal, tries=100):
    for _ in range(tries):
        if not terminal.runner.busy:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the worker never finished")


class HeldLine:
    """A dispatch that holds the FIRST line open until it is let go.

    Every queue test needs it. `busy` is set the moment Enter is pressed and
    cleared by _work's finally, so a dispatch that returns straight away leaves
    the runner idle again before the second line is typed - and the second line
    then takes the plain RUN path. The tests still pass, from the wrong cause;
    coverage is what gives it away, because `self.start(nxt)` is never reached.
    An Event pair rather than a sleep: the point is ordering, not duration.
    """

    def __init__(self, ran, boom=None):
        self.ran = ran
        self.boom = boom
        self.running = threading.Event()
        self.release = threading.Event()
        self.queued_while_running = None

    def __call__(self, request):
        self.ran.append(request.argument or "?")
        if len(self.ran) > 1:
            return
        self.running.set()
        self.release.wait(timeout=2)
        if self.boom is not None:
            raise self.boom

    async def let_go_then_wait(self, terminal):
        """drive()'s `extra`: unblock the first line, then wait for the queue."""
        for _ in range(100):
            if self.running.is_set():
                break
            await asyncio.sleep(0.02)
        self.queued_while_running = terminal.queued_line
        self.release.set()
        await wait_for_idle(terminal)


def test_a_line_is_dispatched_off_the_event_loop():
    """litellm and Ollama block. Running them on the loop freezes the box, which
    is the thing this whole change exists to prevent."""
    terminal, _ = a_terminal()
    threads = []
    terminal.dispatch = lambda request: threads.append(threading.current_thread())
    drive(terminal, ["notice period\r"], extra=wait_for_idle)
    assert threads and threads[0] is not threading.current_thread()


def test_a_queued_line_runs_when_the_first_one_ends():
    terminal, _ = a_terminal()
    ran = []
    held = HeldLine(ran)
    terminal.dispatch = held
    drive(terminal, ["first\r", "second\r"], extra=held.let_go_then_wait)
    assert held.queued_while_running == "second", "the second line never queued"
    assert ran == ["first", "second"]


def test_a_queued_line_still_runs_after_the_first_one_fails():
    """An error has never ended a sift session and should not start now. Only
    Ctrl-C drops the queue - an exception is one line going wrong."""
    terminal, painted = a_terminal()
    ran = []
    held = HeldLine(ran, boom=RuntimeError("ollama said no"))
    terminal.dispatch = held
    drive(terminal, ["first\r", "second\r"], extra=held.let_go_then_wait)
    assert held.queued_while_running == "second", "the second line never queued"
    assert ran == ["first", "second"]
    assert "ollama said no" in screen_text(painted)


def test_a_config_error_is_reported_as_one_line_not_the_whole_explanation():
    """ConfigError carries a paragraph - the consent refusal names the models,
    then what to do about it. One line is what fits above the box, and it is
    what ui.py's own loop always did with it."""
    terminal, painted = a_terminal()

    def dispatch(request):
        raise ConfigError("cloud models configured without consent\n"
                          "re-run with --allow-cloud")

    terminal.dispatch = dispatch
    drive(terminal, ["first\r"], extra=wait_for_idle)
    screen = screen_text(painted)
    assert "cloud models configured without consent" in screen
    assert "--allow-cloud" not in screen, "the whole paragraph landed in the region"


def test_a_cancelled_line_leaves_the_session_running():
    """Ctrl-C between tokens. The half-written answer goes; the session stays -
    and nothing is reported as an error, because the user asked for this.

    A cancel DISCARDS the fragment, where an error keeps it: you asked for the
    answer to stop, so you do not want the half you had been given.
    """
    terminal, painted = a_terminal()

    def dispatch(request):
        terminal.region.tail = "half an answer nobody wants"
        raise Cancelled

    terminal.dispatch = dispatch
    drive(terminal, ["first\r"], extra=wait_for_idle)
    assert terminal.region.tail == "", "the abandoned answer stayed on screen"
    assert "half an answer nobody wants" not in screen_text(painted), \
        "a cancelled fragment was committed to scrollback anyway"
    assert "Cancelled" not in screen_text(painted), "a cancel is not an error"


def test_a_failed_answer_leaves_nothing_in_the_region():
    """Ollama restarting or unloading a model mid-generation raises out of the
    token iterator. The tokens that did arrive are kept - a partial answer with
    an error under it is honest - but nothing stays pinned above the box."""
    terminal, painted = a_terminal()

    def dispatch(request):
        terminal.region.append("half an answer")
        raise RuntimeError("ollama died")

    terminal.dispatch = dispatch
    drive(terminal, ["first\r"], extra=wait_for_idle)
    screen = screen_text(painted)
    assert terminal.region.tail == "", "the half-answer is still pinned above the box"
    assert "half an answer" in screen, "the tokens that arrived were thrown away"
    assert "ollama died" in screen


def test_the_answer_after_a_failed_one_does_not_inherit_its_half():
    """The failure that matters in this repo is attribution - one document's
    real content handed back as another's. A tail left behind by a failed line
    splices the end of answer A onto the front of answer B, on one line, with
    no marker and no way for the reader to tell. Same shape, through the UI.
    """
    terminal, painted = a_terminal()
    ran = []

    def dispatch(request):
        ran.append(request.argument)
        if len(ran) == 1:
            terminal.region.append("half an answer")
            raise RuntimeError("ollama died")
        terminal.region.append("and the NEXT answer")
        terminal.region.flush()

    terminal.dispatch = dispatch
    drive(terminal, ["first\r", "second\r"], extra=wait_for_idle)
    assert ran == ["first", "second"], "the second line never ran"
    second = [line for line in painted if "and the NEXT answer" in line]
    assert second, "the second answer never landed in scrollback"
    assert all("half an answer" not in line for line in second), \
        f"the failed line's text is inside the next answer: {second!r}"


def test_a_status_left_running_by_a_line_is_cleared_when_it_ends():
    """The backstop in _work's finally. Nothing today shows a status outside
    Region.status, whose own finally clears it, so this pins the line rather
    than leaving it to look covered. What it prevents is user-visible: a
    spinner and a climbing elapsed counter sitting over an idle box."""
    terminal, _ = a_terminal()

    def dispatch(request):
        terminal.region.show("thinking...")

    terminal.dispatch = dispatch
    drive(terminal, ["first\r"], extra=wait_for_idle)
    assert terminal.region.message == "", "the spinner is still turning over an idle box"


def test_ctrl_c_clears_the_spinner_instantly_even_if_the_worker_is_still_blocked():
    """Regression: on_interrupt used to only set `cancelled` and print
    "cancelled" - clearing the spinner was left entirely to _work's finally,
    which cannot run before the worker's blocking call returns. A model that
    has not sent its first token yet leaves the worker stuck inside
    `next(tokens, None)`, under `with ui.region.status("thinking..."):` - so
    "cancelled" appeared with a spinner still turning underneath it, for
    however long the model took to answer. Measured at ~2s. Ctrl-c has to
    clear the region itself, synchronously, not wait for the worker to catch
    up to a flag it has no reason to check yet.
    """
    terminal, painted = a_terminal()
    running = threading.Event()
    release = threading.Event()

    def dispatch(request):
        terminal.region.show("thinking...")
        running.set()
        release.wait(timeout=2)               # stands in for the blocked next()

    terminal.dispatch = dispatch
    seen = []

    async def interrupt_while_still_blocked(term):
        for _ in range(100):
            if running.is_set():
                break
            await asyncio.sleep(0.02)
        term.on_interrupt()                    # the loop thread, same as a real key
        seen.append(term.region.message)       # checked BEFORE unblocking the worker
        release.set()
        await wait_for_idle(term)

    drive(terminal, ["first\r"], extra=interrupt_while_still_blocked)
    assert seen == [""], \
        "the spinner was still up right after ctrl-c, before the worker ever unblocked"
    assert "cancelled" in screen_text(painted)


def test_a_status_raised_right_after_cancel_does_not_reopen_the_spinner():
    """Closes the same gap one step further out: `show()` itself must refuse a
    new message once cancelled, or a status opened between the interrupt and
    the worker noticing - "thinking..." replacing "reading your files..." as
    retrieval finishes, say - reopens the exact stale spinner ctrl-c just
    closed. show("") still has to go through, or nothing would ever clear.
    """
    terminal, _ = a_terminal()
    terminal.runner.cancelled = True
    terminal.region.show("thinking...")
    assert terminal.region.message == "", "a message got through after should_stop()"
    terminal.region.show("")
    assert terminal.region.message == "", "show('') must still clear the region"


def test_the_queued_line_stops_being_shown_once_it_starts():
    """Sampled the moment the second line starts, not at the end of the drive:
    an implementation that only cleared the box at session end passes that."""
    terminal, _ = a_terminal()
    ran = []
    held = HeldLine(ran)
    when_second_started = []

    def dispatch(request):
        held(request)
        if len(ran) > 1:
            when_second_started.append(terminal.queued_line)

    terminal.dispatch = dispatch
    drive(terminal, ["first\r", "second\r"], extra=held.let_go_then_wait)
    assert held.queued_while_running == "second", "the second line never queued"
    assert when_second_started == [""], "the box still says a line is waiting"


def test_leaving_drops_a_queued_line_instead_of_answering_it_anyway():
    """Ctrl-D during an answer cannot stop the worker - nothing can kill a
    thread - and the worker's own finally is what starts the queued line. So
    leaving has to drop the queue, or sift goes on to answer the question you
    abandoned, after you have gone. Measured before the guard existed: it did.
    """
    terminal, _ = a_terminal()
    ran = []
    held = HeldLine(ran)
    terminal.dispatch = held
    starts = []
    real_start = terminal.start

    def counting_start(text):
        starts.append(text)
        real_start(text)

    terminal.start = counting_start

    async def leave_while_the_first_line_is_held(term):
        for _ in range(100):
            if held.running.is_set():
                break
            await asyncio.sleep(0.02)
        # drive() sends ctrl-d next. The first line is let go only afterwards,
        # so its finally runs with the session already over - which is the
        # whole point, and why this is a Timer and not a release here.
        threading.Timer(0.2, held.release.set).start()

    drive(terminal, ["first\r", "second\r"],
          extra=leave_while_the_first_line_is_held)
    assert starts == ["first"], "the queued line was started after the user left"


def test_leaving_mid_answer_stops_the_answer_at_the_next_token():
    """Ctrl-D while an answer is streaming.

    Nothing can kill the worker thread, so the only place it can notice is
    LiveRegion.append between tokens - the same cooperative path ctrl-c uses.
    Without it the model streams a whole answer at someone who has already
    left: measured at 400 tokens over 2.49s, all after the keystroke.

    The worker is let go from INSIDE on_eof rather than on a timer, so it is
    guaranteed to be sitting between two tokens at the moment ctrl-d lands.
    A timer would race the loop's own teardown, which calls finished() and
    puts the flag back down.
    """
    terminal, _ = a_terminal()
    streaming = threading.Event()
    go_on = threading.Event()
    tokens = []

    def dispatch(request):
        terminal.region.append("first token ")
        tokens.append("first token ")
        streaming.set()
        go_on.wait(timeout=2)
        # 40 one-character tokens: the tail stays well under TAIL_MAX_CHARS, so
        # nothing here commits and the test cannot park on a loop that has gone.
        for _ in range(40):
            terminal.region.append("x")
            tokens.append("x")

    terminal.dispatch = dispatch
    real_on_eof = terminal.on_eof

    def on_eof_then_let_the_worker_run():
        real_on_eof()
        go_on.set()

    terminal.on_eof = on_eof_then_let_the_worker_run

    async def wait_until_it_is_streaming(term):
        for _ in range(100):
            if streaming.is_set():
                break
            await asyncio.sleep(0.02)

    try:
        drive(terminal, ["first\r"], extra=wait_until_it_is_streaming)
    finally:
        go_on.set()                 # never leave a worker parked on the gate
    assert streaming.is_set(), "the answer never started, so nothing was stopped"
    assert tokens == ["first token "], \
        f"the answer streamed on after ctrl-d: {len(tokens)} tokens"


def test_a_worker_that_wakes_after_teardown_is_still_stopped():
    """The gap the gate above can only narrow.

    asyncio.run's teardown cancels the pending _work task, and its finally
    calls finished() - which puts the per-line flag back down. The executor
    thread cannot be cancelled, so a model that pauses between tokens reaches
    its next append() AFTER that, with nothing left to see, and streams the
    whole answer at someone who has gone.

    Here the worker is released from inside finished() itself, so it wakes up
    strictly after the flag was cleared. Not a timer: the point is the order,
    and this is the losing order every run rather than one in ten.
    """
    terminal, _ = a_terminal()
    streaming = threading.Event()
    go_on = threading.Event()
    tokens = []
    stopped = []

    def dispatch(request):
        terminal.region.append("first token ")
        tokens.append("first token ")
        streaming.set()
        go_on.wait(timeout=2)
        try:
            # 40 one-character tokens, well under TAIL_MAX_CHARS, so nothing
            # here commits and the test cannot park on a loop that has gone.
            for _ in range(40):
                terminal.region.append("x")
                tokens.append("x")
        except Cancelled:
            stopped.append(True)
            raise

    terminal.dispatch = dispatch
    real_finished = terminal.runner.finished

    def finished_then_let_the_worker_run():
        nxt = real_finished()
        go_on.set()
        return nxt

    terminal.runner.finished = finished_then_let_the_worker_run

    async def wait_until_it_is_streaming(term):
        for _ in range(100):
            if streaming.is_set():
                break
            await asyncio.sleep(0.02)

    try:
        drive(terminal, ["first\r"], extra=wait_until_it_is_streaming)
    finally:
        go_on.set()                 # never leave a worker parked on the gate
    assert streaming.is_set(), "the answer never started, so nothing was stopped"
    assert terminal.runner.busy is False, "finished() never ran - this proves nothing"
    assert stopped == [True], "the worker was never told to stop"
    assert tokens == ["first token "], \
        f"the answer streamed on after the session ended: {len(tokens)} tokens"


def test_quit_typed_as_a_word_ends_the_session():
    """`/quit` ends the session from the WORKER thread, so app.exit() has to be
    handed back to the loop. drive() sends ctrl-d after every test and that ends
    the app on its own, so the exit has to be watched for while extra still has
    the run - the return value cannot tell the two apart.
    """
    terminal, _ = a_terminal()
    alive = []

    async def wait_then_look(term):
        await wait_for_idle(term)
        for _ in range(50):
            if not term.app.is_running:
                break
            await asyncio.sleep(0.02)
        alive.append(term.app.is_running)

    assert drive(terminal, ["/quit\r"], extra=wait_then_look) is None
    assert alive == [False], "/quit left the session running"


def test_the_ticker_turns_the_spinner_with_nothing_else_happening():
    """Nothing else asks for that repaint. append() invalidates on a token, and a
    model that spends twenty seconds reading your files emits none.

    Watch the FRAME, not the whole rendered line: the line also carries the
    elapsed seconds, which roll over on their own after a second and make a
    ticker that does nothing at all look like it is working.
    """
    terminal, _ = a_terminal()
    terminal.region.show("thinking...")
    still = LiveRegion.FRAMES[0]
    assert still in terminal.region.render()

    async def main():
        ticker = asyncio.create_task(terminal._tick())
        for _ in range(20):
            await asyncio.sleep(SPINNER_INTERVAL)
            if still not in terminal.region.render():
                break
        ticker.cancel()

    asyncio.run(main())
    assert still not in terminal.region.render(), "the spinner never turned"


def test_a_worker_commit_after_the_run_ends_neither_hangs_nor_is_lost():
    """A loop that has STOPPED but is not yet closed.

    commit() paints straight to the terminal when the loop is None or closed. A
    stopped loop is neither - is_closed() is False - so a worker commit() hands
    its line to run_coroutine_threadsafe and then waits on a loop that will
    never turn again. Forever, silently, with nothing on screen. So the run
    drops its reference to the loop on the way out, which puts commit() back on
    the paint-directly path it already uses before the app starts.

    build() is stubbed because asyncio.run always closes the loop it made, so
    the real one cannot leave this state behind to be tested.
    """
    terminal, painted = a_terminal()
    stopped = asyncio.new_event_loop()          # never ran, and NOT closed

    class FinishedApp:
        async def run_async(self):
            return None

    def build_leaving_a_stopped_loop():
        terminal.loop = stopped
        terminal.app = FinishedApp()
        return terminal.app

    terminal.build = build_leaving_a_stopped_loop
    try:
        assert terminal.run_forever() == 0

        done = threading.Event()

        def worker():
            terminal.commit("the last of the answer")
            done.set()

        # daemon, so a regression fails the assertion below instead of wedging
        # the whole run: a thread parked in .result() never comes back.
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=2)
        assert done.is_set(), "commit() is still waiting on a loop that has stopped"
        assert painted == ["the last of the answer"], "the line was lost"
        assert terminal.loop is None
    finally:
        stopped.close()


def test_run_session_hands_over_the_ui_the_caller_already_has(monkeypatch):
    """TerminalSession replaces the console and region ON the Ui it is given. A
    second Ui built here would leave ui.py's original one writing straight at
    the terminal, past the renderer drawing the box."""
    seen = []
    monkeypatch.setattr(TerminalSession, "run_forever",
                        lambda self: seen.append(self.ui) or 7)
    ui = Ui(Session())
    assert run_session(ui) == 7
    assert seen == [ui]
