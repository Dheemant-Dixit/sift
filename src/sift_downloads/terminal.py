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

import time
from collections.abc import Callable

from sift_downloads.ui import Region


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
