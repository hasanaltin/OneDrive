import logging
import threading

from onedrive import constants
from onedrive.content_cache import ContentCache
from onedrive.db import Database, Item

logger = logging.getLogger(__name__)


class PinWorker(threading.Thread):
    """Eagerly downloads and refreshes content for any folder the user has
    flagged 'always keep on device'. Un-pinning never deletes already-cached
    bytes - eviction is a deliberate later feature, not automatic here."""

    def __init__(
        self,
        db: Database,
        content_cache: ContentCache,
        interval: int = constants.PIN_WORKER_INTERVAL_SECONDS,
    ):
        super().__init__(daemon=True, name="PinWorker")
        self.db = db
        self.cache = content_cache
        self.interval = interval
        self._stop = threading.Event()
        self._wake = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._pass_once()
            except Exception:
                logger.exception("pin worker pass failed")
            self._wake.clear()
            self._wake.wait(self.interval)

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _pass_once(self) -> None:
        for folder in self.db.get_pinned_folders():
            self._download_subtree(folder, root_drive_id=folder.drive_id, root_id=folder.id)

    def _download_subtree(self, folder: Item, *, root_drive_id: str, root_id: str) -> None:
        for child in self.db.list_children(folder.drive_id, folder.id):
            if self._stop.is_set():
                return
            # Re-checks the ROOT folder's live pin state before every file,
            # not just the worker's own shutdown flag - without this, a
            # folder unpinned seconds (or even mid-way) into its own
            # download pass kept downloading everything under it
            # regardless, since get_pinned_folders() above is only ever
            # read once at the start of the whole pass. Confirmed live:
            # pinning a large photo folder for under a second, then
            # unpinning it, still downloaded its entire multi-GB subtree -
            # only killing the app process actually stopped it. Cheap (one
            # indexed row lookup) compared to the download it's guarding.
            if not self._is_still_pinned(root_drive_id, root_id):
                return
            if child.is_folder:
                self._download_subtree(child, root_drive_id=root_drive_id, root_id=root_id)
            elif child.content_state in ("none", "stale"):
                try:
                    self.cache.ensure_cached(child)
                except Exception:
                    logger.warning("pin download failed for %s", child.path, exc_info=True)

    def _is_still_pinned(self, drive_id: str, item_id: str) -> bool:
        item = self.db.get_item_by_id(drive_id, item_id)
        return item is not None and item.is_pinned
