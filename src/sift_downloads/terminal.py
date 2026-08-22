"""
The interactive session — owning the terminal.

ui.py draws; session.py decides; this file holds the cursor. One persistent
prompt_toolkit Application, full_screen=False, with a live region for work in
progress and the input box pinned below it. The box never leaves, so you can
type the next question while the current one is still streaming.

Two libraries cannot both own the cursor. prompt_toolkit keeps a model of where
it sits so it can redraw the box, and a stray escape sequence from another
writer invalidates that model - which is why prompt_toolkit's own stdout proxy
replaces every \\x1b with '?'. So rich stops writing to the terminal here and
becomes a string formatter: it renders into Scrollback, ANSI() parses the
escapes back into prompt_toolkit's own model, and prompt_toolkit paints. Nobody
fights.

full_screen=False is not a preference. A full-screen app swaps to the alternate
buffer and your session history disappears when sift exits, which is the exact
opposite of what README.md:33 promises.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console

from sift_downloads.session import Request, Runner, Verdict, parse
from sift_downloads.ui import PROMPT_STYLE, Region, Ui

log = logging.getLogger(__name__)

# 15 repaints a second, the same rate rich's Live ran at. This is a constructor
# argument rather than throttling code: Application.invalidate() is documented
# thread safe, coalesces repeat calls behind an _invalidated flag, and honours
# min_redraw_interval.
REDRAW_INTERVAL = 1 / 15
# The spinner is the only sign of life while a model reads your files, and
# nothing else asks for that repaint - a model thinking for twenty seconds emits
# no tokens, so no append() invalidates. Ten frames a second, under the redraw
# throttle, so a tick can never cost more than one paint.
SPINNER_INTERVAL = 1 / 10


class Cancelled(Exception):
    """Ctrl-C landed while this line was running.

    Raised out of the live region, which is the only thing the worker touches
    between tokens - so it needs no cancellation flag threaded through ui.py.
    """


class Scrollback:
    """A file for rich's Console. Complete lines go out, escapes intact.

    rich buffers into whatever file it is given, so this collects writes and
    hands over whole lines. The caller turns each one into real scrollback.
    """

    def __init__(self, commit: Callable[[str], None]):
        self._commit = commit
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            self._commit(line)
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._commit(self._buffer)
            self._buffer = ""

    def isatty(self) -> bool:
        """False on purpose. The Console is built with force_terminal=True so it
        emits colour, and this keeps rich from deciding it may also animate."""
        return False


def _wrap(text: str, room: int) -> list[str]:
    """Break one logical line into rows that fit, on spaces where possible.

    Greedy, and it never returns an empty list — a caller that drew nothing for
    a non-empty line would lose the user's answer rather than mis-shape it.
    """
    rows: list[str] = []
    while len(text) > room:
        # Start at 1 for the reason append() does: a space at index 0 is a cut
        # that removes nothing. Search one past `room` so a space landing
        # exactly on the boundary is used rather than wasted.
        cut = text.rfind(" ", 1, room + 1)
        if cut < 0:
            # A word longer than the terminal. Break it — the alternative is a
            # row that wraps anyway, at column 0, with no indent.
            rows.append(text[:room])
            text = text[room:]
        else:
            rows.append(text[:cut])
            text = text[cut + 1:]
    rows.append(text)
    return rows


class LiveRegion(Region):
    """The rows between your scrollback and the input box.

    Holds at most two things: a status line with a spinner, and the unfinished
    tail of the answer. A finished line is committed into real scrollback the
    moment it completes, which is why there is no height cap here and no scroll
    policy - the region stays paragraph-sized however long the answer is, and
    the answer lands in scrollback progressively, which is what it does today.
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    INDENT = "  "
    # A model that streams one unbroken paragraph never triggers the newline
    # commit, and the region would grow until it pushed the box off the screen.
    # Cut at a space instead, which is what wrapping does anyway.
    TAIL_MAX_CHARS = 240

    def __init__(self, commit: Callable[[str], None],
                 invalidate: Callable[[], None],
                 should_stop: Callable[[], bool],
                 width: Callable[[], int]):
        self._commit = commit
        self._invalidate = invalidate
        self._should_stop = should_stop
        # Asked per call, never cached. The terminal can be resized between two
        # tokens of one answer, and a width read once at startup then wraps the
        # rest of the session to a screen that is no longer there.
        self._width = width
        self.message = ""
        self.tail = ""
        self._frame = 0
        self._started = 0.0

    # --- Region ------------------------------------------------------------

    def show(self, message: str) -> None:
        self.message = message
        self._started = time.monotonic()
        self._frame = 0
        self._invalidate()

    def append(self, delta: str) -> None:
        if self._should_stop():
            raise Cancelled
        self.tail += delta
        while "\n" in self.tail:
            line, _, self.tail = self.tail.partition("\n")
            self._commit_line(line)
        while len(self.tail) > self.TAIL_MAX_CHARS:
            # Send whole ROWS to scrollback, not an arbitrary 240 characters.
            # Cutting on the character count puts the break wherever the count
            # happened to fall, which leaves a short ragged row mid-paragraph -
            # and a model's paragraph is over the cap most of the time, so that
            # fired on nearly every answer. A row boundary is where the text
            # was going to break anyway.
            rows = _wrap(self.tail, self._room())
            if len(rows) < 2:
                # A screen wide enough to hold the whole cap on one row. There
                # is no row to send, and no growth to prevent either.
                break
            self._commit(self.INDENT + rows[0])
            rest = self.tail[len(rows[0]):]
            # _wrap eats the single space it broke on, and nothing when it had
            # to break a word. Step over that space only if it is really there.
            self.tail = rest[1:] if rest.startswith(" ") else rest
        self._invalidate()

    def flush(self) -> None:
        if self.tail:
            self._commit_line(self.tail)
            self.tail = ""
        self._invalidate()

    def _commit_line(self, line: str) -> None:
        """Send one logical line to scrollback, as rows that fit the screen.

        The terminal will wrap anything longer than the screen, but it wraps to
        column 0 and cuts wherever the edge falls - so a long answer loses its
        indent and splits mid-word. Measured on an 80-column terminal before
        this: "either part" / "y may end the contract". ui.py:229 already
        refuses to let that happen to a search result; this is the same rule
        for a streamed answer, which is the longer text of the two.
        """
        if not line:
            self._commit("")
            return
        for row in _wrap(line, self._room()):
            self._commit(self.INDENT + row)

    def _room(self) -> int:
        """Columns left for the answer once the indent has taken its two."""
        return max(1, self._width() - len(self.INDENT))

    # --- drawing -----------------------------------------------------------

    def tick(self) -> None:
        """Advance the spinner. Called on a timer while something is running."""
        if self.message:
            self._frame += 1
            self._invalidate()

    def rows(self) -> list[str]:
        """Every row the region draws, already fitted to the screen.

        One source of truth for both render() and height(). They used to
        compute the shape separately - render() emitted the tail as a single
        long row while height() counted what that row WOULD take once wrapped -
        and the Window does not wrap, so it clipped the row at the screen edge
        and padded the difference with blanks. Measured mid-answer at 80
        columns: 4 rows reserved, 1 drawn, 3 empty above the box, and the rest
        of the streaming tail invisible until it reached scrollback.
        """
        rows = []
        if self.message:
            spin = self.FRAMES[self._frame % len(self.FRAMES)]
            # `4s`, not rich's `0:00:04` - wrong-sized for a wait this short.
            seconds = int(time.monotonic() - self._started)
            # Clipped, not wrapped: a status is one row. The download status
            # carries two file sizes and does overflow a narrow terminal.
            line = f"  {spin} {self.message} {seconds}s"[:self._width()]
            rows.append(f"\x1b[2m{line}\x1b[0m")
        if self.tail:
            rows += [self.INDENT + row for row in _wrap(self.tail, self._room())]
        return rows

    def render(self) -> str:
        """The region, as an ANSI string for prompt_toolkit's ANSI() to parse."""
        return "\n".join(self.rows())

    def height(self) -> int:
        """How many rows the region needs. An inline Application does not grow
        to fit its content on its own — it has to be told."""
        return len(self.rows())


class TerminalSession:
    """The Application, its layout, and the keys that drive it.

    Deliberately thin. Every decision about what a key means is a call into
    session.Runner, which is pure and unit-tested; this class reads a key, asks
    the runner, and does what it says.
    """

    def __init__(self, ui: Ui):
        self.ui = ui
        self.runner = Runner()
        # A lambda, not the bound method. `self.runner.should_stop` binds to
        # THIS Runner forever, so replacing self.runner later would leave the
        # region asking the dead one - and a dead Runner answers False, which
        # fails in the "never stop" direction. That is the exact silence
        # `left` exists to close.
        self.region = LiveRegion(self.commit, self.invalidate,
                                 lambda: self.runner.should_stop(), self._width)
        self.queued_line = ""
        self.app: Application | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.area = TextArea(multiline=False, prompt="> ", height=1,
                             history=InMemoryHistory(), wrap_lines=False)

        # Take the terminal off rich. Every Ui method still lays out exactly as
        # it did - results(), sources() and the score alignment are untouched -
        # but into a string, which ANSI() parses and prompt_toolkit paints.
        # Colour is the one thing that does change: highlight=False turns off
        # rich's guessing at numbers and paths, which the default Console at
        # ui.py:185 had on. Explicit markup - [dim], [green] - is unaffected.
        # Skipping this is not a cosmetic miss: ui.echo and ui.note would write
        # straight past the renderer, and prompt_toolkit's model of where the
        # cursor sits is the only reason the box can be redrawn at all.
        self.ui.region = self.region
        self.ui.console = Console(file=Scrollback(self.commit),
                                  force_terminal=True, highlight=False)

    # --- painting ----------------------------------------------------------

    def paint(self, line: str) -> None:
        """Put one line on the screen. Replaced wholesale in tests."""
        print_formatted_text(ANSI(line))

    def commit(self, line: str) -> None:
        """Move one finished line into real scrollback.

        Two callers, two rules, and getting them the same way round deadlocks.

        From the WORKER thread we wait for the paint to happen. That wait is the
        whole ordering guarantee - prompt_toolkit's renderer is incremental and
        writes only the cells that changed, so byte order is not screen order
        and a fire-and-forget version interleaves commits with the streaming
        tail. run_in_terminal ensure_futures eagerly, so it also has to be
        *constructed* on the loop thread; hence the wrapper coroutine.

        From the LOOP thread - ui.note("cancelled") out of a key binding, say -
        waiting is a deadlock: the thing we would be waiting for is us. Schedule
        and return. Ordering is not at risk there because nothing else is
        running on the loop at that moment.
        """
        # Before the app starts, and again after it has gone. A worker outlives
        # the Application - ctrl-d while busy leaves one streaming, and Python
        # cannot kill a thread - so its next commit() arrives at a closed loop.
        # Painting straight to the terminal is right in both cases: there is no
        # renderer left to fight, and the alternative is a RuntimeError raised
        # into the worker thread and a line the user never sees.
        if self.loop is None or self.loop.is_closed():
            self.paint(line)
            return

        def schedule():
            return run_in_terminal(lambda: self.paint(line))

        if self._on_the_loop():
            schedule()
            return

        async def painter() -> None:
            await schedule()

        asyncio.run_coroutine_threadsafe(painter(), self.loop).result()

    def _on_the_loop(self) -> bool:
        """Am I on the thread running the app's loop?

        Not "is any loop running here". A worker driving its own loop would
        answer yes to that, take the non-blocking branch, and lose both
        guarantees at once: commit() would return before the paint landed, and
        run_in_terminal would be scheduled on the worker's loop, so the paint
        would run on the worker thread, past the app's renderer. Silently.
        """
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def invalidate(self) -> None:
        if self.app is not None:
            self.app.invalidate()

    def render_queued(self) -> str:
        if not self.queued_line:
            return ""
        return f"\x1b[2m  queued: {self.queued_line}\x1b[0m"

    # --- the worker --------------------------------------------------------

    def dispatch(self, request: Request) -> None:
        """Run one request. Replaced in tests; ui.dispatch does the real work."""
        from sift_downloads import ui as ui_module

        if not ui_module.dispatch(self.ui, request):
            self.on_eof()

    def start(self, text: str) -> None:
        """Send one line to the worker. Returns at once - the box stays live."""
        request = parse(text)
        if self.loop is None:                       # pragma: no cover
            return
        self.loop.create_task(self._work(request))

    async def _work(self, request: Request) -> None:
        assert self.loop is not None
        try:
            await self.loop.run_in_executor(None, self._run_one, request)
        finally:
            # The region ends empty however this line ended. Per-branch tidying
            # is what left a failed answer's half pinned above the box for the
            # rest of the session - and the NEXT answer then painted on top of
            # it, so text from question A appeared inside the answer to
            # question B with nothing to mark it. That is the attribution shape
            # this whole tool is careful about, arriving through the UI.
            #
            # show("") is a backstop, not dead weight: nothing today shows a
            # status outside Region.status, whose own finally clears it. A
            # spinner and a climbing counter over an idle box is a lie, so this
            # covers the first dispatch path that shows one without a `with`.
            # It also invalidates, which is why nothing below asks again.
            self.region.show("")
            self.region.tail = ""
            nxt = self.runner.finished()
            self.queued_line = ""
            if nxt is not None:
                self.start(nxt)

    def _run_one(self, request: Request) -> None:
        """The blocking half, on a worker thread."""
        from sift_downloads.config import ConfigError
        from sift_downloads.store import IndexProblem

        try:
            self.dispatch(request)
            return
        except Cancelled:
            # Ctrl-C between tokens. The answer is thrown away; the session is
            # not. Cancel does not mean stop, it means stop listening. Nothing
            # to keep here - you asked for the fragment to go.
            return
        except (ConfigError, IndexProblem) as e:
            # ConfigError carries a paragraph. One line is what fits above the
            # box, and it is what ui.py's own loop always did with it.
            message = str(e).split("\n")[0]
        except Exception as e:              # a bad query must not kill the session
            message = f"{type(e).__name__}: {e}"
            log.debug("unhandled error in session", exc_info=True)

        # One exit for both errors, so there is one flush and nothing to leave
        # unpinned. Keep the tokens that did arrive, then say what broke: a
        # partial answer with an error under it is honest, a vanished one is
        # not, and either way the region is empty when the line ends.
        self.region.flush()
        self.ui.error(message)

    # --- keys --------------------------------------------------------------

    def on_enter(self) -> None:
        text = self.area.text
        verdict = self.runner.submit(text)
        if verdict == Verdict.IGNORE:
            return
        if verdict == Verdict.REJECT:
            # The box keeps what you typed. A refusal is "not yet", not "never",
            # so wiping the line would throw away the very question sift just
            # told you to ask again in a moment.
            self.ui.note("one at a time — that one is still queued")
            return
        self.area.text = ""
        self.ui.echo(text.strip())
        if verdict == Verdict.QUEUE:
            self.queued_line = text
            self.invalidate()
            return
        self.start(text)

    def on_interrupt(self) -> None:
        verdict = self.runner.interrupt(self.area.text)
        if verdict == Verdict.CLEAR:
            self.area.text = ""
            return
        if verdict == Verdict.HINT:
            # The one thing anybody loses is an undocumented exit, with a
            # visible hint printed in its place on the exact keystroke that
            # used to take it. /help has always said "leave (or ctrl-d)".
            self.ui.note("(ctrl-d to quit)")
            return
        self.queued_line = ""
        self.ui.note("cancelled")
        self.invalidate()

    def on_eof(self) -> None:
        """End the session. Reached from a key binding AND from the worker, via
        `/quit` - and Application.exit() may only be touched on the loop."""
        if self.app is None:
            return
        # What leaving means to the queue and to the running line is session
        # policy, so the runner decides it - the same rule ctrl-c already goes
        # through interrupt() for. Only the runner's copy changes:
        # `queued_line` is the string the box drew, the box is going away, and
        # _work's finally clears it a moment later.
        #
        # This bounds the ANSWER, not the wait. LiveRegion.append is the only
        # place a worker notices, so a model that has not produced its first
        # token yet, and /sync, run to the end regardless - exactly as they do
        # for ctrl-c today. It is not "ctrl-d returns immediately".
        self.runner.leaving()
        if self._on_the_loop():
            self.app.exit(result=None)
        elif self.loop is not None:
            self.loop.call_soon_threadsafe(self.app.exit, None)

    # --- the Application ---------------------------------------------------

    def build(self) -> Application:
        bindings = KeyBindings()
        bindings.add("enter")(lambda event: self.on_enter())
        bindings.add("c-c")(lambda event: self.on_interrupt())

        @bindings.add("c-d")
        def _eof(event) -> None:
            # An empty box only, the way bash and python read it. With text in
            # the box it would throw away something the user is mid-way through.
            if not self.area.text:
                self.on_eof()

        live = Window(
            FormattedTextControl(lambda: ANSI(self.region.render())),
            height=self.region.height,
            dont_extend_height=True,
        )
        queued = Window(
            FormattedTextControl(lambda: ANSI(self.render_queued())),
            height=lambda: 1 if self.queued_line else 0,
            dont_extend_height=True,
        )
        self.app = Application(
            layout=Layout(HSplit([live, Frame(HSplit([queued, self.area]),
                                              title="sift")])),
            key_bindings=bindings,
            style=PROMPT_STYLE,
            full_screen=False,          # scrollback survives; see the docstring
            mouse_support=False,
            min_redraw_interval=REDRAW_INTERVAL,
        )
        self.loop = asyncio.get_running_loop()
        # The width is only knowable once there is an output to ask.
        self.ui.console.width = self._width()
        return self.app

    def _width(self) -> int:
        if self.app is None:
            return 80
        return self.app.output.get_size().columns

    # --- running -----------------------------------------------------------

    def run_forever(self) -> int:
        """Own the terminal until the user leaves."""
        try:
            asyncio.run(self._main())
        finally:
            # Drop the loop on the way out. commit() paints directly when the
            # loop is None or closed, and a loop that has STOPPED is neither -
            # is_closed() is still False, so a worker still streaming would
            # wait on .result() for a loop that never turns again. Forever,
            # silently, with nothing on screen. None puts it back on the same
            # direct path it uses before the app starts.
            self.loop = None
        return 0

    async def _main(self) -> None:
        app = self.build()
        ticker = asyncio.create_task(self._tick())
        try:
            await app.run_async()
        finally:
            ticker.cancel()

    async def _tick(self) -> None:
        """Turn the spinner. The only repaint nothing else asks for."""
        while True:
            await asyncio.sleep(SPINNER_INTERVAL)
            self.region.tick()


def run_session(ui: Ui) -> int:
    """Hand the terminal to a persistent Application until the user leaves."""
    return TerminalSession(ui).run_forever()
