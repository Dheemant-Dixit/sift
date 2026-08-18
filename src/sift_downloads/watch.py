"""
Watching the folder and re-syncing when it changes.

Optional — `sift index` on demand is enough for most people, and the CLI runs a
fast sync before every query anyway. This is for folders busy enough that you'd
rather the work happened in the background.

Three details that matter in practice:

1. DEBOUNCING. One user action — saving a file, a browser finishing a download,
   unzipping an archive — fires many rapid filesystem events. Re-indexing on
   each would be absurd, so every event restarts a short timer and the sync runs
   only once things go quiet.

2. DEFERRED FILES. A file still being written is skipped by the ingest layer
   (see FRESHNESS_GUARD_SECONDS). In watch mode that would be a trap: the events
   have already fired, so nothing would ever trigger a retry and the file would
   stay unindexed indefinitely. So when a sync reports deferred files, the
   watcher schedules itself another pass.

3. ONE SYNC AT A TIME. A sync can outlast the next debounce window, and each
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


class DebouncedReindexHandler:
    """Collapse bursts of file events into a single debounced re-index."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()       # guards the timer handle only
        self._syncing = threading.Lock()    # held while update_index runs

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
            if stats.deferred:
                # Files were still being written. Nothing else will wake us for
                # them, so come back on our own.
                log.info("  %d file(s) still being written — re-checking shortly", stats.deferred)
                self._schedule(RETRY_SECONDS)
        finally:
            self._syncing.release()

    def cancel(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()


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
        handler.cancel()
        observer.stop()
        observer.join()
