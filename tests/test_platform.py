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
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

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


# --- shutdown: Ctrl-C used to kill the sync it landed on --------------------
#
# The timer runs on a daemon thread, so the interpreter never waited for it.
# Measured against the real loop with SIGINT: the sync dies wherever it had got
# to and `watch()` still prints "Stopping ..." and exits 0. Nothing is torn, but
# the work is dropped without a word, and the kill can land inside store.save()
# between the temp write and the rename — which leaves index.tmp.npz in the data
# dir holding document text.
#
# Four wrong fixes, one test each. Waiting while holding _lock deadlocks the
# deferred path; waiting without _stopping lets the sync arm a fresh timer on
# its way out; waiting with no bound makes Ctrl-C unresponsive on a stuck sync;
# and letting the second Ctrl-C escape skips observer.stop() in watch()'s
# finally block.

@pytest.fixture
def slow_sync(monkeypatch):
    """A sync that blocks until released, so shutdown can be tested mid-flight."""
    from sift_downloads.index import SyncStats
    box = SimpleNamespace(started=threading.Event(), release=threading.Event(),
                          finished=[], stats=SyncStats(), after=None)

    def sync(settings):
        box.started.set()
        box.release.wait(timeout=5)
        if box.after:
            box.after()
        box.finished.append(1)
        return box.stats

    monkeypatch.setattr(watch_module, "update_index", sync)
    return box


def test_ctrl_c_waits_for_a_sync_already_running(slow_sync):
    """The finding. Without the grace period the work is thrown away silently."""
    from sift_downloads.config import get_settings
    handler = DebouncedReindexHandler(get_settings())

    syncing = threading.Thread(target=handler._run, daemon=True)
    syncing.start()
    assert slow_sync.started.wait(timeout=2), "the sync never started — this proves nothing"

    stopping = threading.Thread(target=lambda: handler.cancel(grace=5), daemon=True)
    stopping.start()
    stopping.join(timeout=0.3)
    assert stopping.is_alive(), "cancel() returned while the sync was still running"

    slow_sync.release.set()
    stopping.join(timeout=5)
    assert not stopping.is_alive()
    assert slow_sync.finished == [1]


def test_waiting_for_the_sync_does_not_deadlock_the_deferred_path(slow_sync):
    """The tempting one-liner: acquire _syncing inside the `with self._lock` block.

    It is the deadlock the two locks already exist to avoid, moved somewhere
    new. `_run` takes _syncing and then _lock — that is what _schedule does on
    the deferred path — so waiting for _syncing while holding _lock inverts the
    order and hangs the watcher on shutdown, with no output at all.

    The sync calls _schedule itself here to stand in for that path
    deterministically; the real one runs it a few lines later.
    """
    from sift_downloads.config import get_settings
    handler = DebouncedReindexHandler(get_settings())
    slow_sync.after = lambda: handler._schedule(30)     # wants _lock, holds _syncing

    syncing = threading.Thread(target=handler._run, daemon=True)
    syncing.start()
    assert slow_sync.started.wait(timeout=2)

    stopping = threading.Thread(target=lambda: handler.cancel(grace=5), daemon=True)
    stopping.start()
    stopping.join(timeout=0.3)
    slow_sync.release.set()

    # Promptly, not eventually. Holding _lock across the wait does not hang
    # forever here — it stalls until the grace period expires and then drops
    # the sync it was supposed to be waiting for, which "returns eventually"
    # cannot tell apart from success.
    stopping.join(timeout=2)
    assert not stopping.is_alive(), "cancel() blocked _schedule instead of waiting for it"
    syncing.join(timeout=2)
    assert not syncing.is_alive()
    assert slow_sync.finished == [1]
    handler.cancel()          # drop the timer _schedule just armed


def test_shutdown_does_not_arm_another_sync(slow_sync):
    """A sync reporting deferred files re-arms the timer. During shutdown it must not.

    Otherwise cancel() cancels the pending timer, waits for the sync, and the
    sync schedules a fresh one on its way out — so the watcher starts another
    sync while the process is trying to leave, and that one is killed exactly
    as before.
    """
    from sift_downloads.config import get_settings
    from sift_downloads.index import SyncStats
    slow_sync.stats = SyncStats(deferred=2)
    handler = DebouncedReindexHandler(get_settings())

    syncing = threading.Thread(target=handler._run, daemon=True)
    syncing.start()
    assert slow_sync.started.wait(timeout=2)

    stopping = threading.Thread(target=lambda: handler.cancel(grace=5), daemon=True)
    stopping.start()
    stopping.join(timeout=0.3)
    slow_sync.release.set()
    stopping.join(timeout=5)
    syncing.join(timeout=5)

    assert slow_sync.finished == [1], "the deferred sync did not finish"
    assert handler._timer is None, "shutdown armed a retry that will be killed anyway"


def test_a_stuck_sync_does_not_make_ctrl_c_hang_forever(caplog):
    """The grace period is a bound, not a promise."""
    from sift_downloads.config import get_settings
    handler = DebouncedReindexHandler(get_settings())
    handler._syncing.acquire()          # a sync that is never going to finish

    # On a thread on purpose. An unbounded wait is the obvious "just let it
    # finish" fix, and called inline it hangs the whole pytest run with no
    # output instead of failing — which is worse than not testing it.
    stopping = threading.Thread(target=lambda: handler.cancel(grace=0.1), daemon=True)
    stopping.start()
    stopping.join(timeout=3)
    assert not stopping.is_alive(), "Ctrl-C waited past the grace period"
    assert "dropping it" in caplog.text


def test_a_second_ctrl_c_during_the_wait_is_absorbed(caplog):
    """cancel() runs inside watch()'s finally block.

    A KeyboardInterrupt escaping from here skips observer.stop() and join(), so
    the impatient second Ctrl-C leaves the watchdog threads up and the process
    hung — the opposite of what it was pressed for.
    """
    import logging

    from sift_downloads.config import get_settings
    caplog.set_level(logging.INFO)            # the shutdown notices are INFO
    handler = DebouncedReindexHandler(get_settings())

    class InterruptedLock:
        def acquire(self, blocking=True, timeout=-1):
            if not blocking:
                return False              # report a sync as running
            raise KeyboardInterrupt       # the user's second Ctrl-C

        def release(self):
            raise AssertionError("nothing was acquired")

    handler._syncing = InterruptedLock()
    try:
        handler.cancel(grace=5)
    except KeyboardInterrupt:
        # Caught rather than allowed to propagate: pytest treats a bare
        # KeyboardInterrupt as "abort the session", so the wrong fix would
        # cancel the run instead of failing this test.
        pytest.fail("the second Ctrl-C escaped cancel(); watch()'s finally block "
                    "would skip observer.stop()")
    assert "dropped" in caplog.text


def test_watch_hands_its_grace_period_to_cancel(monkeypatch, make_file):
    """Wiring, not behaviour — but a grace period watch() never passes is decoration.

    cancel() defaults to grace=0 so "cancel" still means cancel for every other
    caller, and that default is exactly what makes it possible to fix the
    shutdown path and never reach it.
    """
    from sift_downloads.index import SyncStats
    make_file("a.md", "x")
    monkeypatch.setattr(watch_module, "update_index", lambda s: SyncStats())

    stopped = []
    fake = SimpleNamespace(schedule=lambda *a, **k: None, start=lambda: None,
                           stop=lambda: stopped.append(1), join=lambda: None)
    monkeypatch.setitem(sys.modules, "watchdog.observers",
                        SimpleNamespace(Observer=lambda: fake))
    monkeypatch.setattr(watch_module.time, "sleep",
                        lambda n: (_ for _ in ()).throw(KeyboardInterrupt))

    graces = []
    monkeypatch.setattr(DebouncedReindexHandler, "cancel",
                        lambda self, grace=0.0: graces.append(grace))

    watch_module.watch()

    assert graces == [watch_module.SHUTDOWN_GRACE_SECONDS]
    assert graces[0] > 0, "watch() passed a grace period that waits for nothing"
    assert stopped == [1], "the finally block did not finish"


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

    instances: ClassVar[list[FakeObserver]] = []

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


# --- one sync at a time -----------------------------------------------------
#
# _schedule can cancel a PENDING timer, but a timer that has already fired is
# running _run on its own thread and nothing can call it back. So a sync that
# outlasts the next debounce window meets its successor head-on. update_index
# is load-modify-save over one index.npz, so the loser of that race is not
# "slower" — it is erased: the run that finishes last writes a store built from
# a snapshot taken before the other run's file existed, and takes that file's
# manifest entry with it. The file is then unindexed with nothing left to
# re-arm the watcher, which is the same silent-forever failure the deferred
# retry exists to prevent.


def test_a_second_sync_does_not_start_while_one_is_running(monkeypatch, scheduled):
    """The race itself: a timer firing mid-sync must not enter update_index."""
    from sift_downloads.config import get_settings
    from sift_downloads.index import SyncStats

    syncs = []
    first_is_running = threading.Event()
    let_it_finish = threading.Event()

    def slow_sync(settings):
        syncs.append(1)
        first_is_running.set()
        assert let_it_finish.wait(timeout=5), "the first sync was never released"
        return SyncStats()

    monkeypatch.setattr(watch_module, "update_index", slow_sync)
    handler = DebouncedReindexHandler(get_settings())

    first = threading.Thread(target=handler._run, daemon=True)
    first.start()
    assert first_is_running.wait(timeout=5), "the first sync never started"

    handler._run()      # the next debounce timer, firing mid-sync

    assert syncs == [1], "the second sync ran while the first was still going"

    let_it_finish.set()
    first.join(timeout=5)
    assert not first.is_alive(), "the first sync never finished"


def test_a_sync_that_arrives_mid_sync_comes_back_instead_of_being_dropped(
        monkeypatch, scheduled):
    """Refusing to start is only safe if the work is not thrown away.

    The events that triggered the second run have already fired and will not
    fire again, so skipping it outright would lose exactly the file that caused
    it. It re-arms the timer instead — the same answer the module already gives
    for a file that was still being written.
    """
    from sift_downloads.config import get_settings
    from sift_downloads.index import SyncStats

    first_is_running = threading.Event()
    let_it_finish = threading.Event()

    def slow_sync(settings):
        first_is_running.set()
        assert let_it_finish.wait(timeout=5), "the first sync was never released"
        return SyncStats()

    monkeypatch.setattr(watch_module, "update_index", slow_sync)
    handler = DebouncedReindexHandler(get_settings())

    first = threading.Thread(target=handler._run, daemon=True)
    first.start()
    assert first_is_running.wait(timeout=5), "the first sync never started"

    handler._run()

    assert scheduled == [watch_module.RETRY_SECONDS]

    let_it_finish.set()
    first.join(timeout=5)
    assert not first.is_alive(), "the first sync never finished"


def test_a_deferred_retry_is_armed_from_inside_the_sync(monkeypatch):
    """_run schedules its own retry, so whatever guards _run cannot be _lock.

    threading.Lock is not reentrant: hold _lock across _run and the deferred
    path — the one case watch mode exists for — deadlocks on _schedule and the
    watcher stops forever with no error. This does not discriminate against the
    race above; it is a guard on where the guard may live, and it fails by
    hanging, which is why it joins with a timeout instead of blocking CI.

    Every other deferred test replaces _schedule via the `scheduled` fixture,
    so this is the only one that runs the real one.
    """
    from sift_downloads.config import get_settings
    from sift_downloads.index import SyncStats

    monkeypatch.setattr(watch_module, "update_index", lambda s: SyncStats(deferred=1))
    monkeypatch.setattr(watch_module, "RETRY_SECONDS", 30)   # never allowed to fire
    handler = DebouncedReindexHandler(get_settings())

    run = threading.Thread(target=handler._run, daemon=True)
    run.start()
    run.join(timeout=5)

    assert not run.is_alive(), "_run deadlocked scheduling its own retry"
    assert handler._timer is not None, "the retry was never armed"
    handler.cancel()
