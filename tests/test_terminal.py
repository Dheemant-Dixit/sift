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
from rich.console import Console

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
