import logging
import threading
from pathlib import Path

from onedrive import constants
from onedrive.db import Database, Item
from onedrive.graph_client import GraphClient

logger = logging.getLogger(__name__)


def path_for(drive_id: str, item_id: str) -> Path:
    # two-level fanout so the cache dir never holds huge numbers of siblings
    return constants.CONTENT_CACHE_DIR / drive_id / item_id[:2] / item_id


class ContentCache:
    def __init__(self, db: Database, graph_client: GraphClient):
        self.db = db
        self.graph = graph_client
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, item_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(item_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[item_id] = lock
            return lock

    def ensure_cached(self, item: Item, log_open_activity: bool = True) -> Path:
        """Returns a local path with this item's content, downloading it first
        if necessary. Safe to call concurrently for the same item.

        log_open_activity=False skips the "downloaded" activity-log entry
        (still downloads and caches normally) - for callers that know this
        particular open() wasn't the user actually opening the file, e.g.
        KIO's own kmimetypefinder helper reading a file's magic bytes just
        to label it in a Dolphin folder view. Confirmed live: opening
        `~/OneDrive/...` in Dolphin silently downloaded and logged "You
        opened ..." for files the user never touched, traced directly to
        kmimetypefinder's own open() call (via pyfuse3's RequestContext.pid)
        for any file whose type isn't obvious from its extension alone."""
        local_path = path_for(item.drive_id, item.id)
        with self._lock_for(item.id):
            # re-read current state in case another thread just finished
            current = self.db.get_item_by_id(item.drive_id, item.id)
            if (
                current
                and current.content_state == "ready"
                and local_path.exists()
                and local_path.stat().st_size == current.size
            ):
                return local_path

            if current is not None and current.remote_id is None:
                # A locally-created, not-yet-synced item (offline mount
                # write) has no remote copy to download - its content is
                # only ever the local staged file, and the cache-hit check
                # above should already have short-circuited for it (create()
                # sets content_state='ready' the moment it stages content).
                # Reaching here means that invariant broke somewhere, not
                # that a real download should be attempted against a
                # synthetic "pending:..." id - fail clearly instead of
                # sending a bogus id into a Graph URL.
                raise FileNotFoundError(
                    f"item {item.id} has no remote_id yet and no usable local cache at {local_path}"
                )

            self.db.set_content_state(item.drive_id, item.id, "downloading")
            try:
                logger.info("Downloading item id=%s path=%s size=%s", item.id, item.path, item.size)
                self.graph.download_content(item.drive_id, item.remote_id, local_path)
                self.db.set_content_state(item.drive_id, item.id, "ready")
                if log_open_activity:
                    self.db.log_activity("downloaded", item.name, item.path, "mount")
            except Exception:
                self.db.set_content_state(item.drive_id, item.id, "none")
                raise
            return local_path

    def cache_size_bytes(self) -> int:
        total = 0
        if constants.CONTENT_CACHE_DIR.exists():
            for f in constants.CONTENT_CACHE_DIR.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        return total
