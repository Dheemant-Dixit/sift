"""
Tests for the two modules that talk to the operating system.

open_file.py and watch.py both branch on platform and shell out, so the useful
question is not "does it work here" but "does each platform get the right call".
Every subprocess is captured; nothing is ever launched.

watch.py's debouncing is the other thing worth pinning down. Its whole purpose
is that one user action produces many filesystem events and must produce exactly
one re-index — a property that is invisible until it regresses.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sift_downloads import open_file as open_file_module
from sift_downloads import watch as watch_module
from sift_downloads.config import DIST_NAME
from sift_downloads.open_file import open_file, reveal_file
from sift_downloads.watch import DebouncedReindexHandler


@pytest.fixture
def ran(monkeypatch):
    """Capture subprocess.run calls instead of making them."""
    calls = []
    monkeypatch.setattr(open_file_module.subprocess, "run",
                        lambda cmd, **kw: calls.append((cmd, kw)))
    return calls


def as_platform(monkeypatch, system):
    monkeypatch.setattr(open_file_module.platform, "system", lambda: system)


# open_file passes str(path) to the OS, and str(Path("/src/a.pdf")) is
# "\\src\\a.pdf" on Windows. Build expectations the same way the code does,
# so these assert the ARGUMENT SHAPE rather than the host's separator.
A_PDF = Path("/src/a.pdf")
A_SUB = Path("/src/sub/a.pdf")


# --- opening a file ---------------------------------------------------------

def test_macos_opens_with_open(monkeypatch, ran):
    as_platform(monkeypatch, "Darwin")
    open_file(A_PDF)
    assert ran[0][0] == ["open", str(A_PDF)]


def test_linux_opens_with_xdg_open(monkeypatch, ran):
    as_platform(monkeypatch, "Linux")
    open_file(A_PDF)
    assert ran[0][0] == ["xdg-open", str(A_PDF)]


def test_an_unknown_platform_falls_back_to_xdg_open(monkeypatch, ran):
    as_platform(monkeypatch, "FreeBSD")
    open_file(A_PDF)
    assert ran[0][0][0] == "xdg-open"


def test_windows_uses_startfile(monkeypatch):
    as_platform(monkeypatch, "Windows")
    started = []
    monkeypatch.setattr(open_file_module.os, "startfile",
                        lambda p: started.append(p), raising=False)
    open_file(A_PDF)
    assert started == [str(A_PDF)]


def test_a_failed_open_is_not_swallowed(monkeypatch, ran):
    as_platform(monkeypatch, "Darwin")
    open_file(A_PDF)
    assert ran[0][1]["check"] is True


# --- revealing a file -------------------------------------------------------

def test_macos_reveals_with_dash_r(monkeypatch, ran):
    as_platform(monkeypatch, "Darwin")
    reveal_file(A_PDF)
    assert ran[0][0] == ["open", "-R", str(A_PDF)]


def test_windows_reveals_with_explorer_select(monkeypatch, ran):
    as_platform(monkeypatch, "Windows")
    reveal_file(A_PDF)
    assert ran[0][0] == ["explorer", f"/select,{A_PDF}"]


def test_windows_reveal_does_not_check_the_exit_code(monkeypatch, ran):
    """explorer /select returns 1 even when it works."""
    as_platform(monkeypatch, "Windows")
    reveal_file(A_PDF)
    assert ran[0][1]["check"] is False


def test_linux_reveal_opens_the_containing_folder(monkeypatch, ran):
    """There is no standard 'select this file' call on Linux."""
    as_platform(monkeypatch, "Linux")
    reveal_file(A_SUB)
    assert ran[0][0] == ["xdg-open", str(A_SUB.parent)]


# --- which events the watcher cares about ----------------------------------

def event(path="/src/a.pdf", kind="created", is_directory=False, dest=""):
    return SimpleNamespace(src_path=path, event_type=kind,
                           is_directory=is_directory, dest_path=dest)


@pytest.fixture
def handler(monkeypatch):
    """A handler whose sync is captured and whose timer fires immediately."""
    from sift_downloads.config import get_settings
    h = DebouncedReindexHandler(get_settings())
    return h


@pytest.fixture
def scheduled(monkeypatch):
    """Record debounce scheduling without waiting for real timers."""
    calls = []
    monkeypatch.setattr(DebouncedReindexHandler, "_schedule",
                        lambda self, delay: calls.append(delay))
    return calls


def test_a_supported_file_triggers_a_sync(handler, scheduled):
    handler.dispatch(event("/src/a.pdf"))
    assert scheduled == [watch_module.DEBOUNCE_SECONDS]


def test_an_unsupported_file_is_ignored(handler, scheduled):
    handler.dispatch(event("/src/movie.mkv"))
    assert scheduled == []


def test_a_directory_event_is_ignored(handler, scheduled):
    handler.dispatch(event("/src/subfolder", is_directory=True))
    assert scheduled == []


def test_a_move_onto_a_supported_type_counts(handler, scheduled):
    """Downloads finish by renaming a .crdownload onto its real name."""
    handler.dispatch(event("/src/a.pdf.crdownload", kind="moved", dest="/src/a.pdf"))
    assert scheduled == [watch_module.DEBOUNCE_SECONDS]


def test_a_move_between_two_ignored_types_does_not(handler, scheduled):
    handler.dispatch(event("/src/a.mkv", kind="moved", dest="/src/b.mkv"))
    assert scheduled == []


# --- debouncing: many events, one sync -------------------------------------

def test_a_burst_of_events_produces_exactly_one_sync(monkeypatch):
    """The property the whole module exists for."""
    from sift_downloads.config import get_settings
    from sift_downloads.index import SyncStats

    syncs = []
    monkeypatch.setattr(watch_module, "update_index",
                        lambda s: syncs.append(1) or SyncStats())
    monkeypatch.setattr(watch_module, "DEBOUNCE_SECONDS", 0.02)

    handler = DebouncedReindexHandler(get_settings())
    for _ in range(20):
        handler.dispatch(event())
    assert syncs == [], "nothing should have run while events were still arriving"

    handler._timer.join(timeout=2)
    assert syncs == [1]


def test_a_failing_sync_is_logged_and_does_not_kill_the_watcher(monkeypatch, caplog):
    from sift_downloads.config import get_settings
    monkeypatch.setattr(watch_module, "update_index",
                        lambda s: (_ for _ in ()).throw(RuntimeError("disk full")))
    handler = DebouncedReindexHandler(get_settings())
    handler._run()          # must not raise
    assert "disk full" in caplog.text


def test_deferred_files_schedule_their_own_retry(monkeypatch, scheduled):
    """Nothing else will wake the watcher for a file that was still being written."""
    from sift_downloads.config import get_settings
    from sift_downloads.index import SyncStats
    monkeypatch.setattr(watch_module, "update_index", lambda s: SyncStats(deferred=2))
    DebouncedReindexHandler(get_settings())._run()
    assert scheduled == [watch_module.RETRY_SECONDS]


def test_no_deferred_files_means_no_retry(monkeypatch, scheduled):
    from sift_downloads.config import get_settings
    from sift_downloads.index import SyncStats
    monkeypatch.setattr(watch_module, "update_index", lambda s: SyncStats())
    DebouncedReindexHandler(get_settings())._run()
    assert scheduled == []


def test_cancelling_stops_a_pending_sync(monkeypatch):
    from sift_downloads.config import get_settings
    from sift_downloads.index import SyncStats
    syncs = []
    monkeypatch.setattr(watch_module, "update_index",
                        lambda s: syncs.append(1) or SyncStats())
    monkeypatch.setattr(watch_module, "DEBOUNCE_SECONDS", 0.05)

    handler = DebouncedReindexHandler(get_settings())
    handler.dispatch(event())
    handler.cancel()
    handler._timer.join(timeout=1)
    assert syncs == []


def test_cancel_is_safe_before_anything_was_scheduled():
    from sift_downloads.config import get_settings
    DebouncedReindexHandler(get_settings()).cancel()   # must not raise


# --- watch() without watchdog installed ------------------------------------

def test_a_missing_watchdog_explains_the_extra_to_install(monkeypatch, make_file):
    """The hint must name the DISTRIBUTION, which is not what the command is called.

    `sift` is a different, unrelated project on PyPI, so `pip install sift[watch]`
    installs a stranger's package and still leaves watch mode broken. This asserts
    the distribution name is present AND that the bare command name is not being
    offered as one — the previous `assert "watch" in ...` passed either way, which
    made it decoration rather than a regression test.
    """
    from sift_downloads.index import SyncStats
    make_file("a.md", "x")
    monkeypatch.setattr(watch_module, "update_index", lambda s: SyncStats())
    monkeypatch.setitem(sys.modules, "watchdog.observers", None)

    with pytest.raises(SystemExit) as e:
        watch_module.watch()

    message = str(e.value)
    assert "watchdog" in message                    # says what is missing
    assert f"{DIST_NAME}[watch]" in message         # says what to install
    assert "sift[watch]" not in message             # and not the wrong project


# --- watch(), the observer loop --------------------------------------------

class FakeObserver:
    """Stands in for watchdog's Observer, recording how it was driven."""

    instances: list[FakeObserver] = []

    def __init__(self):
        self.scheduled: list[tuple[object, str, bool]] = []
        self.started = self.stopped = self.joined = False
        FakeObserver.instances.append(self)

    def schedule(self, handler, path, recursive=False):
        self.scheduled.append((handler, path, recursive))

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self):
        self.joined = True


@pytest.fixture
def observed(monkeypatch, make_file):
    """Run watch() to completion: one sync, then an immediate Ctrl-C."""
    from sift_downloads.index import SyncStats
    make_file("a.md", "x")
    FakeObserver.instances.clear()

    syncs = []
    monkeypatch.setattr(watch_module, "update_index",
                        lambda s: syncs.append(1) or SyncStats(chunks_total=1, files_total=1))
    monkeypatch.setitem(sys.modules, "watchdog.observers",
                        SimpleNamespace(Observer=FakeObserver))

    def stop_immediately(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(watch_module.time, "sleep", stop_immediately)
    watch_module.watch()
    return SimpleNamespace(observer=FakeObserver.instances[-1], syncs=syncs)


def test_watch_syncs_once_before_watching(observed):
    assert observed.syncs == [1]


def test_watch_watches_the_source_folder_non_recursively(observed):
    from sift_downloads.config import get_settings
    _, path, recursive = observed.observer.scheduled[0]
    assert path == str(get_settings().source_dir)
    assert recursive is False, "recursing would drag in every unzipped project"


def test_watch_installs_the_debouncing_handler(observed):
    handler, _, _ = observed.observer.scheduled[0]
    assert isinstance(handler, DebouncedReindexHandler)


def test_ctrl_c_shuts_the_observer_down_cleanly(observed):
    assert observed.observer.started
    assert observed.observer.stopped and observed.observer.joined


def test_watch_refuses_to_start_on_a_missing_folder(monkeypatch, tmp_path):
    from sift_downloads.config import ConfigError, configure
    configure(source_dir=tmp_path / "gone")
    with pytest.raises(ConfigError):
        watch_module.watch()
