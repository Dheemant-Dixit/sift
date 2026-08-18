"""
The interactive session — drawing only.

Inline rather than full-screen: results scroll into your normal terminal
history and a bordered input box stays pinned at the bottom. Nothing takes over
the screen, so your scrollback survives the session and everything printed
stays selectable and copyable afterwards.

The split from session.py is deliberate. Deciding what a line means and doing
it is pure logic and lives there; this file only turns results into pixels and
reads keys. That keeps the interesting parts testable without a terminal.
"""
from __future__ import annotations

import logging
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console
from rich.live import Live
from rich.padding import Padding
from rich.text import Text

from sift_downloads.config import ConfigError, Settings, get_settings
from sift_downloads.session import HELP, Request, Session, UiCommand, parse
from sift_downloads.store import IndexProblem

log = logging.getLogger(__name__)

# Muted enough to sit behind the content, visible enough to find.
PROMPT_STYLE = Style.from_dict({
    "frame.border": "#585858",
    "frame.label": "#8a8a8a",
    "": "",
})

MATCH_MARKER = {"content": "·", "filename": "name", "both": "·+name"}


def _clip(text: str, room: int) -> str:
    """Collapse whitespace and cut to `room` columns, with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= room:
        return flat
    return flat[: max(1, room - 1)].rstrip() + "…"


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def read_line(history: InMemoryHistory, title: str = "sift") -> str | None:
    """Draw the input box and return what was typed, or None to quit.

    A one-shot Application per line, with erase_when_done, so the box is
    redrawn at the bottom each time instead of accumulating copies up the
    scrollback.
    """
    area = TextArea(multiline=False, prompt="> ", height=1, history=history,
                    wrap_lines=False)
    bindings = KeyBindings()

    @bindings.add("enter")
    def _accept(event) -> None:
        event.app.exit(result=area.text)

    @bindings.add("c-c")
    @bindings.add("c-d")
    def _quit(event) -> None:
        event.app.exit(result=None)

    app = Application(
        layout=Layout(Frame(area, title=title)),
        key_bindings=bindings,
        style=PROMPT_STYLE,
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
    )
    return app.run()


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _size(num_bytes: int) -> str:
    from sift_downloads.cli import human_size
    return human_size(num_bytes)


def _age(timestamp: float) -> str:
    from sift_downloads.cli import human_age
    return human_age(timestamp)


class Ui:
    """Everything that writes to the screen."""

    def __init__(self, session: Session, console: Console | None = None):
        self.session = session
        self.console = console or Console()

    # --- chrome ------------------------------------------------------------

    def banner(self) -> None:
        settings = self.session.settings
        self.console.print()
        self.console.print("[bold]●[/bold] [bold]sift[/bold]", end="")
        self.console.print(f"  [dim]{settings.source_dir}[/dim]")
        privacy = ("[green]local[/green]" if not settings.uses_cloud()
                   else "[yellow]cloud models in use[/yellow]")
        self.console.print(f"  [dim]{settings.chat_model} · {privacy}[/dim]")
        self.console.print("  [dim]type to search · ?question to ask · /help[/dim]")
        self.console.print()

    def echo(self, line: str) -> None:
        """Put the submitted line into scrollback, since the box erases itself."""
        self.console.print(f"[bold cyan]>[/bold cyan] {line}")

    def error(self, message: str) -> None:
        self.console.print(f"  [red]{message}[/red]")

    def note(self, message: str) -> None:
        self.console.print(f"  [dim]{message}[/dim]")

    def help(self) -> None:
        self.console.print()
        for keys, what in HELP:
            self.console.print(f"  [cyan]{keys:<18}[/cyan] [dim]{what}[/dim]")
        self.console.print()

    # --- results -----------------------------------------------------------

    def results(self, hits: list, query: str) -> None:
        if not hits:
            self.console.print(f"  [dim]nothing matched[/dim] [italic]{query}[/italic]")
            self.note("try fewer words, or /sync if you just downloaded it")
            return

        # Every detail line is clipped to one row rather than allowed to wrap.
        # A wrapped snippet reflows to column 0 and breaks the indentation that
        # makes a dense list scannable — one truncated line reads far better
        # than three ragged ones.
        width = self.console.width
        indent = 6
        room = max(20, width - indent - 1)

        self.console.print()
        for i, hit in enumerate(hits, 1):
            self.console.print(self._result_heading(i, hit, width))
            self.console.print(
                f"      [dim]{_size(hit.size)} · {_age(hit.modified)}[/dim]")
            if hit.snippet:
                self.console.print(f"      [dim italic]{_clip(hit.snippet, room)}[/dim italic]")
            elif hit.note:
                self.console.print(f"      [yellow dim]({_clip(hit.note, room - 2)})[/yellow dim]")
            if hit.duplicates:
                copies = ", ".join(Path(d).name for d in hit.duplicates)
                self.console.print(
                    f"      [dim]+{len(hit.duplicates)} copy: {_clip(copies, room - 12)}[/dim]")
        self.console.print()
        self.note("/open N to open · /reveal N to show in finder")

    @staticmethod
    def _result_heading(position: int, hit, width: int) -> Text:
        """` 1. Name.pdf                            0.98 name` — score flushed right."""
        # Marker padded to a fixed width so the SCORES line up in a column;
        # right-aligning the whole block instead would stagger them by however
        # wide each marker happens to be.
        right = f"{hit.score:.2f} {MATCH_MARKER[hit.matched_on]:<6}"
        prefix = f"  {position:>2}. "
        name_room = max(8, width - len(prefix) - len(right) - 2)
        name = _clip(hit.name, name_room)
        gap = max(1, width - len(prefix) - len(name) - len(right) - 1)

        line = Text()
        line.append(prefix)
        line.append(name, style="bold")
        line.append(" " * gap)
        line.append(right, style="dim")
        return line

    def sources(self, chunks: list[dict]) -> None:
        if not chunks:
            return
        self.console.print()
        seen: list[str] = []
        for chunk in chunks:
            tag = (f"{chunk['filename']} "
                   f"[dim](chunk {chunk['chunk_index']}, {chunk['score']:.2f})[/dim]")
            if tag not in seen:
                seen.append(tag)
        self.console.print("  [dim]— from —[/dim]")
        for tag in seen:
            self.console.print(f"    [dim]•[/dim] {tag}")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _do_find(ui: Ui, request: Request) -> None:
    from sift_downloads.find import find_files

    session = ui.session
    with ui.console.status("[dim]searching...[/dim]", spinner="dots"):
        hits = find_files(request.argument, limit=10, recent_first=request.recent,
                          settings=session.settings)
    session.last_hits = hits
    session.remember("user", request.argument)
    ui.results(hits, request.argument)


def _do_ask(ui: Ui, request: Request) -> None:
    from sift_downloads.generate import AnswerStream

    session = ui.session
    session.remember("user", request.argument)

    with ui.console.status("[dim]reading your files...[/dim]", spinner="dots"):
        stream = AnswerStream(request.argument, settings=session.settings)

    if stream.refusal is not None:
        ui.console.print()
        ui.console.print(f"  [yellow]{stream.refusal.text}[/yellow]")
        ui.console.print()
        return

    # Stream the answer as it arrives, the way the one-shot CLI cannot.
    # Live re-renders a padded Text rather than appending raw deltas, so
    # wrapped lines keep their indent instead of reflowing to column 0.
    ui.console.print()
    body = Text(style="default")
    with Live(Padding(body, (0, 0, 0, 2)), console=ui.console,
              refresh_per_second=15, vertical_overflow="visible") as live:
        for delta in stream:
            body.append(delta)
            live.update(Padding(body, (0, 0, 0, 2)))

    if not body.plain.strip():
        ui.note("(the model returned nothing)")
    answer = stream.finish()
    session.remember("assistant", answer.text)
    ui.sources(answer.chunks)
    ui.console.print()


def _do_open(ui: Ui, request: Request, reveal: bool) -> None:
    from sift_downloads.open_file import open_file, reveal_file

    path, problem = ui.session.resolve(request.index)
    if problem:
        ui.error(problem)
        return
    try:
        (reveal_file if reveal else open_file)(path)
    except Exception as e:
        ui.error(f"could not open {path.name}: {type(e).__name__}: {e}")
        return
    ui.note(f"{'revealed' if reveal else 'opened'} {path.name}")


def _do_sync(ui: Ui, quiet: bool = False) -> None:
    from sift_downloads.index import update_index

    try:
        with ui.console.status("[dim]syncing index...[/dim]", spinner="dots"):
            stats = update_index(ui.session.settings)
    except (ConfigError, IndexProblem) as e:
        ui.error(str(e).split("\n")[0])
        return
    ui.session.synced = True
    if stats.upgraded:
        # Announced even in quiet mode: the sync just took a minute instead of a
        # second, and silence would read as a hang.
        ui.note("index format changed in this version — re-embedded everything once")
        for path in stats.needs_unlock:
            ui.error(f"{Path(path).name} was unlocked before — "
                     f"run `unlock` to read it again")
    if quiet and not stats.changed:
        return
    if stats.changed:
        ui.note(f"+{stats.added} added, ~{stats.modified} modified, "
                f"-{stats.deleted} deleted → {stats.chunks_total} chunks")
    else:
        ui.note(f"index up to date — {stats.chunks_total} chunks "
                f"across {stats.files_total} files")


def _do_status(ui: Ui) -> None:
    from sift_downloads.index import Manifest
    from sift_downloads.store import VectorStore

    settings = ui.session.settings
    manifest = Manifest.load(settings=settings)
    if not settings.index_path.exists():
        ui.note("no index yet — /sync to build one")
        return
    try:
        store = VectorStore.load(settings=settings)
    except IndexProblem as e:
        ui.error(str(e).split("\n")[0])
        return
    size_mb = settings.index_path.stat().st_size / 1024 / 1024
    ui.note(f"{len(store)} chunks from {len(manifest.files)} files ({size_mb:.1f}MB)")
    ui.note(f"index at {settings.data_dir}")
    empty = [p for p, e in manifest.files.items() if not e.get("num_chunks")]
    if empty:
        ui.note(f"{len(empty)} file(s) have no readable text (findable by name)")


def dispatch(ui: Ui, request: Request) -> bool:
    """Run one request. Returns False when the session should end."""
    if request.command == UiCommand.QUIT:
        return False
    if request.command == UiCommand.NOTHING:
        return True
    if request.command == UiCommand.ERROR:
        ui.error(request.message)
        return True
    if request.command == UiCommand.HELP:
        ui.help()
        return True
    if request.command == UiCommand.FIND:
        _do_find(ui, request)
        return True
    if request.command == UiCommand.ASK:
        _do_ask(ui, request)
        return True
    if request.command in (UiCommand.OPEN, UiCommand.REVEAL):
        _do_open(ui, request, reveal=request.command == UiCommand.REVEAL)
        return True
    if request.command == UiCommand.SYNC:
        _do_sync(ui)
        return True
    if request.command == UiCommand.STATUS:
        _do_status(ui)
        return True
    return True


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def run(settings: Settings | None = None) -> int:
    """Start an interactive session. Returns a process exit code."""
    settings = settings or get_settings()
    session = Session(settings=settings)
    ui = Ui(session)

    ui.banner()
    _do_sync(ui, quiet=True)

    history = InMemoryHistory()
    while True:
        try:
            line = read_line(history)
        except (EOFError, KeyboardInterrupt):
            break
        if line is None:            # ctrl-c / ctrl-d in the box
            break

        request = parse(line)
        if request.command == UiCommand.NOTHING:
            continue
        ui.echo(line.strip())

        try:
            if not dispatch(ui, request):
                break
        except KeyboardInterrupt:
            # Interrupting a slow query returns you to the prompt; it doesn't
            # end the session. Only the empty box exits.
            ui.note("cancelled")
        except (ConfigError, IndexProblem) as e:
            ui.error(str(e).split("\n")[0])
        except Exception as e:      # a bad query must not kill the session
            ui.error(f"{type(e).__name__}: {e}")
            log.debug("unhandled error in session", exc_info=True)

    ui.console.print("  [dim]bye[/dim]")
    return 0
