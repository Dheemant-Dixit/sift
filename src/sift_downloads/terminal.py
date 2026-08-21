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
                 is_cancelled: Callable[[], bool]):
        self._commit = commit
        self._invalidate = invalidate
        self._is_cancelled = is_cancelled
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
        if self._is_cancelled():
            raise Cancelled
        self.tail += delta
        while "\n" in self.tail:
            line, _, self.tail = self.tail.partition("\n")
            self._commit(self.INDENT + line if line else "")
        while len(self.tail) > self.TAIL_MAX_CHARS:
            # Start at 1, not 0. A space at index 0 is a cut that removes
            # nothing, and rfind reports "not found" as -1 - so a naive guard
            # treats a real cut point and no cut point the same way, and the
            # tail never shrinks again.
            cut = self.tail.rfind(" ", 1, self.TAIL_MAX_CHARS)
            if cut < 0:
                # Nothing to break on. Cut mid-word, which is what wrapping
                # does to you anyway, and far better than a region that grows
                # until it pushes the box off the screen.
                self._commit(self.INDENT + self.tail[:self.TAIL_MAX_CHARS])
                self.tail = self.tail[self.TAIL_MAX_CHARS:]
            else:
                self._commit(self.INDENT + self.tail[:cut])
                self.tail = self.tail[cut + 1:]
        self._invalidate()

    def flush(self) -> None:
        if self.tail:
            self._commit(self.INDENT + self.tail)
            self.tail = ""
        self._invalidate()

    # --- drawing -----------------------------------------------------------

    def tick(self) -> None:
        """Advance the spinner. Called on a timer while something is running."""
        if self.message:
            self._frame += 1
            self._invalidate()

    def render(self) -> str:
        """The region, as an ANSI string for prompt_toolkit's ANSI() to parse."""
        rows = []
        if self.message:
            spin = self.FRAMES[self._frame % len(self.FRAMES)]
            # `4s`, not rich's `0:00:04` - wrong-sized for a wait this short.
            seconds = int(time.monotonic() - self._started)
            rows.append(f"\x1b[2m  {spin} {self.message} {seconds}s\x1b[0m")
        if self.tail:
            rows.append(self.INDENT + self.tail)
        return "\n".join(rows)

    def height(self, width: int) -> int:
        """How many rows the region needs. An inline Application does not grow
        to fit its content on its own — it has to be told."""
        rows = 1 if self.message else 0
        if self.tail:
            room = max(1, width - len(self.INDENT))
            rows += -(-len(self.tail) // room)      # ceil
        return rows


class TerminalSession:
    """The Application, its layout, and the keys that drive it.

    Deliberately thin. Every decision about what a key means is a call into
    session.Runner, which is pure and unit-tested; this class reads a key, asks
    the runner, and does what it says.
    """

    def __init__(self, ui: Ui):
        self.ui = ui
        self.runner = Runner()
        self.region = LiveRegion(self.commit, self.invalidate,
                                 lambda: self.runner.cancelled)
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
        except Cancelled:
            # Ctrl-C between tokens. The answer is thrown away; the session is
            # not. Cancel does not mean stop, it means stop listening. Nothing
            # to keep here - you asked for the fragment to go.
            pass
        except (ConfigError, IndexProblem) as e:
            self.region.flush()
            self.ui.error(str(e).split("\n")[0])
        except Exception as e:              # a bad query must not kill the session
            # Keep the tokens that did arrive, then say what broke. A partial
            # answer under an error line is honest; a vanished answer is not.
            self.region.flush()
            self.ui.error(f"{type(e).__name__}: {e}")
            log.debug("unhandled error in session", exc_info=True)

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
            height=lambda: self.region.height(self._width()),
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
