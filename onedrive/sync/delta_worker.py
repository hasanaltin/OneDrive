import logging
import threading
from typing import Callable

from onedrive import constants
from onedrive.db import Database, now_iso
from onedrive.graph_client import GraphAuthError, GraphClient

logger = logging.getLogger(__name__)


class DeltaSyncWorker(threading.Thread):
    """Keeps the local metadata cache in sync with OneDrive. The very first
    run (no saved delta_link yet) performs a full crawl; every run after that
    is a cheap incremental /delta call. Same code path for both - they only
    differ in whether sync_state['delta_link'] is set."""

    def __init__(
        self,
        db: Database,
        graph_client: GraphClient,
        drive_id: str,
        interval: int = constants.DELTA_POLL_INTERVAL_SECONDS,
        on_status: Callable[[str], None] | None = None,
        on_auth_required: Callable[[], None] | None = None,
    ):
        super().__init__(daemon=True, name="DeltaSyncWorker")
        self.db = db
        self.graph = graph_client
        self.drive_id = drive_id
        self.interval = interval
        self.on_status = on_status or (lambda _msg: None)
        self.on_auth_required = on_auth_required or (lambda: None)
        self._stop = threading.Event()
        self._wake = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sync_once()
            except GraphAuthError:
                logger.warning("delta sync needs re-authentication")
                self.on_status("Sign-in required")
                self.on_auth_required()
            except Exception:
                logger.exception("delta sync pass failed")
                self.on_status("Sync error - will retry")
            self._wake.clear()
            self._wake.wait(self.interval)

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _sync_once(self) -> None:
        delta_link = self.db.get_sync_state("delta_link")
        count = 0
        for page in self.graph.delta(delta_link):
            for graph_item in page:
                self.db.upsert_item(self.drive_id, graph_item)
            count += len(page)
            if page:
                self.on_status(f"Syncing... {count} items")
        self.db.resolve_pending_paths(self.drive_id)
        if self.graph.last_delta_link:
            self.db.set_sync_state("delta_link", self.graph.last_delta_link)
        self.db.set_sync_state("last_sync_at", now_iso())
        self.on_status("Idle")
