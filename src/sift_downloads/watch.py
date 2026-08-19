"""
Watching the folder and re-syncing when it changes.

Optional — `sift index` on demand is enough for most people, and the CLI runs a
fast sync before every query anyway. This is for folders busy enough that you'd
rather the work happened in the background.

Four details that matter in practice:

1. DEBOUNCING. One user action — saving a file, a browser finishing a download,
   unzipping an archive — fires many rapid filesystem events. Re-indexing on
   each would be absurd, so every event restarts a short timer and the sync runs
   only once things go quiet.

2. DEFERRED FILES. A file still being written is skipped by the ingest layer
   (see FRESHNESS_GUARD_SECONDS). In watch mode that would be a trap: the events
   have already fired, so nothing would ever trigger a retry and the file would
   stay unindexed indefinitely. So when a sync reports deferred files, the
   watcher schedules itself another pass.

3. SHUTDOWN. Ctrl-C used to kill a sync wherever it had got to: the timer runs
   on a daemon thread, so the interpreter does not wait for it. Nothing is torn
   — store.save() writes then renames — but the work is dropped without a word,
   and the kill can land between the two files a sync writes. So shutdown now
   gives a running sync a grace period to finish.

4. ONE SYNC AT A TIME. A sync can outlast the next debounce window, and each
   timer fires on its own thread, so two of them can meet. update_index is
   load-modify-save over one index.npz: the run that finishes last wins, and
   what it writes was built before the other run's file existed. That file
   loses its manifest entry too, so it goes unindexed with nothing left to
   re-arm the watcher.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from sift_downloads.config import DIST_NAME, Settings, get_settings, require_source_dir
from sift_downloads.index import update_index

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 3.0
RETRY_SECONDS = 5.0   # how long to wait before re-checking deferred files

# How long Ctrl-C waits for a sync that is already running. A bound rather than
# a promise: a folder big enough to outlast this is a folder where re-syncing
# costs less than an unresponsive Ctrl-C. A second Ctrl-C drops it immediately.
SHUTDOWN_GRACE_SECONDS = 30.0


class DebouncedReindexHandler:
    """Collapse bursts of file events into a single debounced re-index."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()       # guards the timer handle only
        self._syncing = threading.Lock()    # held while update_index runs
        # Read and written without a lock on purpose: a bool assignment is
        # atomic, and the only thing it guards against is arming a new timer
        # while shutting down. Taking _lock for it would put cancel() back in
        # the deadlock described on cancel().
        self._stopping = False

    # watchdog calls this; typed loosely so watchdog stays an optional import.
    def dispatch(self, event) -> None:
        if not self._is_relevant(event):
            return
        name = Path(getattr(event, "dest_path", "") or event.src_path).name
        log.info("  · %s: %s", event.event_type, name)
        self._schedule(DEBOUNCE_SECONDS)

    def _is_relevant(self, event) -> bool:
        if getattr(event, "is_directory", False):
            return False
        # On a move, either end landing on a type we index makes it relevant.
        paths = [event.src_path, getattr(event, "dest_path", "")]
        return any(Path(p).suffix.lower() in self.settings.extensions for p in paths if p)

    def _schedule(self, delay: float) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self._run)
            self._timer.daemon = True
            self._timer.start()

    def _run(self) -> None:
        if self._stopping:
            return
        # Two locks, deliberately. _syncing cannot be _lock: _run schedules its
        # own retry, _schedule takes _lock, and a threading.Lock is not
        # reentrant — one lock across both would deadlock the watcher on the
        # deferred path, silently and forever. And we never WAIT for _syncing.
        # Blocking here would park a thread per fired timer for the length of a
        # sync the docstring measures in tens of seconds; re-arming instead
        # costs one timer, which the next _schedule collapses anyway. Nothing
        # is dropped: the events that woke us are gone, but the retry is not.
        if not self._syncing.acquire(blocking=False):
            log.info("  · still syncing — re-checking shortly")
            self._schedule(RETRY_SECONDS)
            return
        try:
            log.info("Change settled — syncing ...")
            try:
                stats = update_index(self.settings)
            except Exception as e:
                log.error("  ! sync failed: %s: %s", type(e).__name__, e)
                return
            log.info("  %d chunks across %d files (%.1fs)",
                     stats.chunks_total, stats.files_total, stats.seconds)
            if stats.deferred and not self._stopping:
                # Files were still being written. Nothing else will wake us for
                # them, so come back on our own.
                log.info("  %d file(s) still being written — re-checking shortly", stats.deferred)
                self._schedule(RETRY_SECONDS)
        finally:
            self._syncing.release()

    def cancel(self, grace: float = 0.0) -> None:
        """Stop scheduling. With `grace`, let a sync already running finish.

        Waiting is not tidiness. The timer thread is a daemon, so without this
        the interpreter drops it mid-sync at shutdown. Measured, the kill lands
        in one of three places: during embedding (the work is simply thrown
        away), inside store.save() between the temp write and the rename (which
        leaves index.tmp.npz sitting in the data dir holding your document
        text), or between store.save() and manifest.save() (which leaves the
        manifest one sync behind the store). All three heal on the next run,
        which is why this is a grace period and not a guarantee — but none of
        them is mentioned to the user, who only sees "Stopping ...".

        _lock is released before waiting, deliberately. `_run` takes _syncing
        and then _lock — through _schedule, on the deferred path. Waiting for
        _syncing while still holding _lock inverts that order and hangs the
        watcher on shutdown: the same deadlock the two locks exist to avoid,
        moved somewhere new.
        """
        self._stopping = True
        with self._lock:
            if self._timer:
                self._timer.cancel()
        if grace <= 0:
            return
        if self._syncing.acquire(blocking=False):     # nothing was running
            self._syncing.release()
            return
        log.info("Finishing the sync in progress (Ctrl-C again to drop it) ...")
        try:
            if self._syncing.acquire(timeout=grace):
                self._syncing.release()
            else:
                log.warning("  ! still syncing after %.0fs — dropping it", grace)
        except KeyboardInterrupt:
            # Must not escape: watch() calls this from a finally block, and an
            # exception here would skip observer.stop() and leave the threads up.
            log.info("  dropped — the next sync will pick up where this left off")


def watch(settings: Settings | None = None) -> None:
    """Sync once, then keep syncing as the folder changes. Blocks until Ctrl-C."""
    settings = settings or get_settings()
    source = require_source_dir(settings)

    try:
        from watchdog.observers import Observer
    except ImportError:
        raise SystemExit(
            "watch mode needs the watchdog package:\n"
            f'  pip install "{DIST_NAME}[watch]"'
        ) from None

    log.info("Initial sync of %s ...", source)
    stats = update_index(settings)
    log.info("  %d chunks across %d files (%.1fs)",
             stats.chunks_total, stats.files_total, stats.seconds)

    handler = DebouncedReindexHandler(settings)
    observer = Observer()
    # Non-recursive, matching the ingest scan: descending into an unzipped
    # project folder would generate thousands of irrelevant events.
    observer.schedule(handler, str(source), recursive=False)
    observer.start()
    log.info("Watching %s for changes (Ctrl-C to stop) ...", source)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping ...")
    finally:
        handler.cancel(SHUTDOWN_GRACE_SECONDS)
        observer.stop()
        observer.join()
