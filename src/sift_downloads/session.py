"""
What an interactive session does, with no terminal involved.

Deliberately separated from ui.py. Everything here is pure — parse a line,
decide what it means, run it, return a result object — so it can be tested
headlessly and on Windows, where the pty the real UI needs doesn't exist.
ui.py is then only responsible for drawing.

v1 is stateless between queries: each line is answered on its own. The one
thing carried across is `last_hits`, because `/open 2` has to know what "2"
referred to. `history` is recorded but not yet fed to the model — that is the
seam where conversational follow-ups drop in later without reshaping anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sift_downloads.config import Settings, get_settings


class UiCommand:
    """Names of the things a session can be asked to do."""

    FIND = "find"
    ASK = "ask"
    OPEN = "open"
    REVEAL = "reveal"
    STATUS = "status"
    SYNC = "sync"
    HELP = "help"
    QUIT = "quit"
    NOTHING = "nothing"
    ERROR = "error"


@dataclass
class Request:
    """One parsed input line."""

    command: str
    argument: str = ""
    index: int | None = None      # for /open and /reveal
    recent: bool = False
    message: str = ""             # for ERROR


# Aliases people will reach for without reading the help.
_QUIT_WORDS = {"quit", "exit", ":q", "q"}
_ALIASES = {
    "f": UiCommand.FIND, "find": UiCommand.FIND, "search": UiCommand.FIND,
    "a": UiCommand.ASK, "ask": UiCommand.ASK,
    "o": UiCommand.OPEN, "open": UiCommand.OPEN,
    "r": UiCommand.REVEAL, "reveal": UiCommand.REVEAL,
    "s": UiCommand.STATUS, "status": UiCommand.STATUS,
    "sync": UiCommand.SYNC, "index": UiCommand.SYNC, "reindex": UiCommand.SYNC,
    "h": UiCommand.HELP, "help": UiCommand.HELP, "?": UiCommand.HELP,
    "q": UiCommand.QUIT, "quit": UiCommand.QUIT, "exit": UiCommand.QUIT,
}


def parse(line: str) -> Request:
    """Turn a typed line into a Request.

    Bare text searches, because finding a file is the common case and should
    cost no syntax. Questions are one character away: a leading '?' or /ask.
    """
    text = line.strip()
    if not text:
        return Request(UiCommand.NOTHING)

    if text.lower() in _QUIT_WORDS:
        return Request(UiCommand.QUIT)

    # A lone '?' is someone asking for help, not searching for a punctuation mark.
    if text == "?":
        return Request(UiCommand.HELP)

    # '?what is my notice period' — the shorthand for asking.
    if text.startswith("?"):
        question = text[1:].strip()
        if not question:
            return Request(UiCommand.HELP)
        return Request(UiCommand.ASK, argument=question)

    if not text.startswith("/"):
        return Request(UiCommand.FIND, argument=text)

    word, _, rest = text[1:].partition(" ")
    rest = rest.strip()
    command = _ALIASES.get(word.lower())

    if command is None:
        return Request(UiCommand.ERROR,
                       message=f"unknown command /{word} — try /help")

    if command in (UiCommand.OPEN, UiCommand.REVEAL):
        if not rest:
            return Request(UiCommand.ERROR,
                           message=f"/{word} needs a result number, e.g. /{word} 1")
        try:
            index = int(rest.split()[0])
        except ValueError:
            return Request(UiCommand.ERROR,
                           message=f"'{rest}' is not a result number")
        return Request(command, index=index)

    if command == UiCommand.FIND:
        # "/find --recent tax" — the one flag worth having inline.
        recent = False
        for flag in ("--recent", "-r"):
            if rest.startswith(flag + " ") or rest == flag:
                recent = True
                rest = rest[len(flag):].strip()
        if not rest:
            return Request(UiCommand.ERROR, message="/find needs something to look for")
        return Request(UiCommand.FIND, argument=rest, recent=recent)

    if command == UiCommand.ASK:
        if not rest:
            return Request(UiCommand.ERROR, message="/ask needs a question")
        return Request(UiCommand.ASK, argument=rest)

    return Request(command)


@dataclass
class Session:
    """State carried across one interactive run."""

    settings: Settings = field(default_factory=get_settings)
    last_hits: list = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, text)
    synced: bool = False

    def remember(self, role: str, text: str) -> None:
        """Record a turn.

        Unused by v1's prompting — every question is answered on its own. It
        exists so that turning this into a conversation is a change to how the
        prompt is built, not a change to the shape of the session.
        """
        self.history.append((role, text))

    def resolve(self, index: int | None) -> tuple[Path | None, str]:
        """Map a result number from the last search to a path.

        Returns (path, error). One-based, because the list the user is looking
        at is numbered from 1.
        """
        if not self.last_hits:
            return None, "nothing to open yet — search for something first"
        if index is None:
            return None, "which result? e.g. /open 1"
        if not 1 <= index <= len(self.last_hits):
            count = len(self.last_hits)
            have = "is 1 result" if count == 1 else f"are {count} results"
            return None, f"no result {index} — there {have}"
        return self.last_hits[index - 1].path, ""


HELP = [
    ("<anything>", "search your files for it"),
    ("?<question>", "ask a question, answered from your files"),
    ("/ask <question>", "same as ?"),
    ("/find [-r] <text>", "search explicitly; -r favours recent files"),
    ("/open <n>", "open result n"),
    ("/reveal <n>", "show result n in your file manager"),
    ("/sync", "refresh the index now"),
    ("/status", "what's indexed"),
    ("/help", "this list"),
    ("/quit", "leave (or ctrl-d)"),
]
