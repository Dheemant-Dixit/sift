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

from collections.abc import Callable


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
