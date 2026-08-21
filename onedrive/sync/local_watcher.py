import logging
import queue
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class _PairEventHandler(FileSystemEventHandler):
    """No I/O, no DB access here - watchdog invokes this on its own internal
    reader thread, so it just hands off fast. All correctness logic lives in
    the reconciler, not here; every event (whatever it was) just means
    'this pair's state may have changed, go take a fresh look.'"""

    def __init__(self, pair_id: int, event_queue: "queue.Queue[tuple[int, float]]"):
        self.pair_id = pair_id
        self.queue = event_queue

    def on_any_event(self, event) -> None:
        self.queue.put((self.pair_id, time.monotonic()))


class LocalWatcher:
    """One shared Observer for all pairs; watch()/unwatch() dynamically as
    pairs are added/removed/toggled at runtime."""

    def __init__(self, event_queue: "queue.Queue[tuple[int, float]]"):
        self._observer = Observer()
        self._observer.start()
        self._queue = event_queue
        self._watches: dict[int, object] = {}

    def watch(self, pair_id: int, local_path: Path) -> None:
        if pair_id in self._watches:
            return
        handler = _PairEventHandler(pair_id, self._queue)
        self._watches[pair_id] = self._observer.schedule(
            handler, str(local_path), recursive=True
        )
        logger.info("watching pair %s at %s", pair_id, local_path)

    def unwatch(self, pair_id: int) -> None:
        watch = self._watches.pop(pair_id, None)
        if watch is not None:
            self._observer.unschedule(watch)
            logger.info("stopped watching pair %s", pair_id)

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
