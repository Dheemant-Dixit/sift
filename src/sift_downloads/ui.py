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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console
from rich.padding import Padding
from rich.text import Text

from sift_downloads.config import ConfigError, Settings, get_settings
from sift_downloads.humanize import MATCH_MARKER, human_age, human_size
from sift_downloads.session import HELP, Request, Session, UiCommand
from sift_downloads.store import IndexProblem

log = logging.getLogger(__name__)

# Muted enough to sit behind the content, visible enough to find.
PROMPT_STYLE = Style.from_dict({
    "frame.border": "#585858",
    "frame.label": "#8a8a8a",
    "": "",
})

def _clip(text: str, room: int) -> str:
    """Collapse whitespace and cut to `room` columns, with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= room:
        return flat
    return flat[: max(1, room - 1)].rstrip() + "…"


# ---------------------------------------------------------------------------
# The live region
# ---------------------------------------------------------------------------
#
# Four things used to animate through rich here: console.status in three
# actions, and Progress + Live in _do_ask. Every one wrote escape sequences
# straight at the terminal. Once a persistent Application owns the cursor that
# is no longer allowed - prompt_toolkit keeps a model of where the cursor sits
# so it can redraw the pinned box, and a stray escape from another writer
# invalidates it. So all of them go behind this seam, and terminal.py supplies
# an implementation that draws in the region above the box instead.


class Status:
    """A transient one-liner. `update` replaces it in place."""

    def __init__(self, region: Region, message: str):
        self._region = region
        self.update(message)

    def update(self, message: str) -> None:
        self._region.show(message)


class Region:
    """Where work-in-progress is drawn. Three methods, and that is the contract.

    `status` is a context manager so the caller reads the way `console.status`
    did. `append`/`flush` accumulate a streaming answer.
    """

    def show(self, message: str) -> None:
        raise NotImplementedError

    @contextmanager
    def status(self, message: str) -> Iterator[Status]:
        handle = Status(self, message)
        try:
            yield handle
        finally:
            self.show("")

    def append(self, delta: str) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError


class PlainRegion(Region):
    """No terminal to own, so no animation: say it once and move on.

    This is what a dumb terminal gets, and what the tests read. The animated
    version lives in terminal.py, where there is an Application to draw it.
    """

    INDENT = 2

    def __init__(self, console: Console):
        self.console = console
        self._tail = ""

    def show(self, message: str) -> None:
        """Nothing. A status is transient, and there is no way to erase a line
        already printed into scrollback.

        Two things break if this prints. The startup sync is quiet on purpose so
        it does not push the banner off the screen, which
        `test_a_quiet_sync_with_no_changes_prints_nothing` has pinned since it
        was written. And `_offer_setup` calls `status.update` once per chunk of
        a model download — printing each one turns one pull into hundreds of
        lines. The spinner belongs to LiveRegion, which can redraw in place.
        """

    def append(self, delta: str) -> None:
        self._tail += delta

    def flush(self) -> None:
        if not self._tail:
            return
        # A padded Text rather than raw deltas, so a wrapped line keeps its
        # indent instead of reflowing to column 0. That is the property Live
        # was chosen for at ui.py:224, and it has to survive the replacement.
        self.console.print(Padding(Text(self._tail, style="default"),
                                   (0, 0, 0, self.INDENT)))
        self._tail = ""


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

class Ui:
    """Everything that writes to the screen."""

    def __init__(self, session: Session, console: Console | None = None,
                 region: Region | None = None):
        self.session = session
        self.console = console or Console()
        self.region = region or PlainRegion(self.console)

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
                f"      [dim]{human_size(hit.size)} · {human_age(hit.modified)}[/dim]")
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

    def sources(self, chunks: list[dict], heading: str = "from") -> None:
        if not chunks:
            return
        self.console.print()
        seen: list[str] = []
        for chunk in chunks:
            tag = (f"{chunk['filename']} "
                   f"[dim](chunk {chunk['chunk_index']}, {chunk['score']:.2f})[/dim]")
            if tag not in seen:
                seen.append(tag)
        self.console.print(f"  [dim]— {heading} —[/dim]")
        for tag in seen:
            self.console.print(f"    [dim]•[/dim] {tag}")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _do_find(ui: Ui, request: Request) -> None:
    from sift_downloads.find import find_files

    session = ui.session
    with ui.region.status("searching..."):
        hits = find_files(request.argument, limit=10, recent_first=request.recent,
                          settings=session.settings)
    session.last_hits = hits
    session.remember("user", request.argument)
    ui.results(hits, request.argument)


def _do_ask(ui: Ui, request: Request) -> None:
    from sift_downloads.generate import AnswerStream

    session = ui.session
    session.remember("user", request.argument)

    with ui.region.status("reading your files..."):
        stream = AnswerStream(request.argument, settings=session.settings)

    if stream.refusal is not None:
        ui.console.print()
        ui.console.print(f"  [yellow]{stream.refusal.text}[/yellow]")
        ui.console.print()
        return

    # Retrieval is done, so the filenames exist a long time before the first
    # token does. Showing them here fills the wait AND puts the grounding
    # before the claims, which is the honest order for a RAG tool.
    ui.sources(stream.chunks, heading="reading from")

    # The model call lives in AnswerStream.__iter__ and does not fire until
    # something asks for an item, so pulling the first token by hand is what
    # brings it inside the spinner. Stopping the spinner is then the same
    # instant as having something to show: no blank gap, no double-drawing.
    tokens = iter(stream)
    with ui.region.status("thinking..."):
        first = next(tokens, None)

    ui.console.print()
    body = first or ""
    if first:
        ui.region.append(first)
    for delta in tokens:
        body += delta
        ui.region.append(delta)
    ui.region.flush()

    if not body.strip():
        ui.note("(the model returned nothing)")
    answer = stream.finish()
    session.remember("assistant", answer.text)
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
        with ui.region.status("syncing index..."):
            stats = update_index(ui.session.settings)
    except (ConfigError, IndexProblem) as e:
        ui.error(str(e).split("\n")[0])
        return
    except Exception as e:
        # Anything else is a model server saying no — a missing model answers
        # /api/embed with a 404, and litellm raises its own class for it. The
        # startup sync runs outside the session loop, so an escape here is a
        # traceback instead of a session. cli._quiet_sync makes the same catch.
        first = str(e).split("\n")[0]
        ui.error(f"could not refresh the index ({type(e).__name__}: {first})")
        log.debug("sync failed", exc_info=True)
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


def _offer_setup(ui: Ui) -> None:
    """First run: offer to fetch the models, right where the user already is.

    A stranger's first `sift` starts here with nothing pulled, and every path
    out of this function has to leave a working session behind — a decline, a
    failed download and a Ctrl-C included. Nothing here is fatal: without
    models sift can still find files by name.

    sift does not install Ollama (see setup.py), so a missing server is
    explained rather than offered — there would be nothing to say yes to.
    """
    from sift_downloads.setup import SetupError, plan_setup, pull_model

    plan = plan_setup(ui.session.settings)
    if plan.ready:
        return

    for model, why in plan.skipped:
        ui.note(f"{model}: {why}")

    if not plan.server_up:
        ui.error("Ollama isn't running, so searching inside documents won't work yet.")
        ui.note(f"try: {plan.install_hint}")
        return

    ui.note(f"first run — sift needs: {', '.join(plan.to_pull)}")
    ui.note("they come from ollama.com into Ollama's store; Ctrl-C is safe, it resumes")
    reply = read_line(InMemoryHistory(), title="download them now? [y/N]")
    if (reply or "").strip().lower() not in ("y", "yes"):
        return

    with ui.region.status("downloading...") as status:
        for model in plan.to_pull:
            try:
                pull_model(model, lambda p: status.update(
                    f"{p.model} — {p.status} "
                    f"{human_size(p.completed)} / {human_size(p.total)}"))
            except SetupError as e:
                ui.error(str(e))
                return
            except KeyboardInterrupt:
                ui.note(f"stopped — run `sift setup` to resume {model}")
                return
            ui.note(f"{model} ready")


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

def _start_terminal(ui: Ui) -> int:
    """Imported here, not at module scope: terminal.py imports this module."""
    from sift_downloads.terminal import run_session

    return run_session(ui)


def run(settings: Settings | None = None) -> int:
    """Start an interactive session. Returns a process exit code.

    Everything before the Application starts still uses the plain rich console
    and the one-shot read_line: banner, the first-run offer and the first sync
    all happen while nothing owns the terminal yet.
    """
    settings = settings or get_settings()
    session = Session(settings=settings)
    ui = Ui(session)

    ui.banner()
    _offer_setup(ui)
    _do_sync(ui, quiet=True)

    code = _start_terminal(ui)
    ui.console.print("  [dim]bye[/dim]")
    return code
