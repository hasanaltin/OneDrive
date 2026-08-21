import datetime
import logging
import threading
from pathlib import Path
from typing import Callable

import requests

from onedrive import constants
from onedrive.content_cache import path_for
from onedrive.db import Database
from onedrive.graph_client import GraphAuthError, GraphClient, GraphConflictError

logger = logging.getLogger(__name__)


class MountSyncWorker(threading.Thread):
    """Drains pending_mount_ops - queued by the on-demand FUSE mount's
    offline-tolerant write handlers in fuse/operations.py - against
    Microsoft Graph. One per mounted drive.

    Every op type except 'delete' reads the item's *current* live state
    from `items` at execution time rather than acting on a snapshot (see
    the pending_mount_ops table comment in db.py) - this is what makes
    ordering trivially correct: a rename issued before its own create has
    even synced needs no special-casing, the create just picks up
    whatever name/parent is live in the DB when the worker gets to it.

    Runs a full drain on startup, whenever wake() is called (FUSE handlers
    call this right after enqueueing), and every `interval` seconds
    regardless - the same three-trigger pattern PairSyncWorker already
    uses."""

    def __init__(
        self,
        db: Database,
        graph_client: GraphClient,
        drive_id: str,
        interval: int = constants.MOUNT_SYNC_INTERVAL_SECONDS,
        on_auth_required: Callable[[], None] | None = None,
        on_conflict: Callable[[str, str], None] | None = None,
    ):
        super().__init__(daemon=True, name="MountSyncWorker")
        self.db = db
        self.graph = graph_client
        self.drive_id = drive_id
        self.interval = interval
        self.on_auth_required = on_auth_required or (lambda: None)
        self.on_conflict = on_conflict or (lambda _name, _path: None)
        self._stop = threading.Event()
        self._wake = threading.Event()

    def run(self) -> None:
        # An op left 'in_progress' means the process died mid-network-call
        # last run - whether that call actually landed server-side or not
        # is unknown either way, so it's simply retried (same
        # retry-idempotency precedent PairSyncWorker already relies on:
        # create calls use conflictBehavior "fail", a retried small-file
        # PUT just harmlessly re-sends identical bytes).
        self.db.reset_in_progress_mount_ops(self.drive_id)
        while not self._stop.is_set():
            self._drain_safely()
            self._wake.wait(timeout=self.interval)
            self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    # --- draining the queue -------------------------------------------------

    def _drain_safely(self) -> None:
        try:
            self._drain()
        except GraphAuthError:
            logger.warning("mount sync needs re-authentication")
            self.on_auth_required()
        except Exception:
            logger.exception("mount sync pass failed")

    def _drain(self) -> None:
        # Same gap, same fix as PairSyncWorker's own action loop (see its
        # comment) - without this, stop() didn't actually interrupt a
        # large backlog of queued mount writes, only the wait between
        # drain passes. Safe to break mid-batch: whatever's left over just
        # gets picked up by the next drain pass, same retry-tolerant
        # design already relied on for a mid-op process death.
        for op in self.db.list_pending_mount_ops(self.drive_id):
            if self._stop.is_set():
                return
            try:
                self._execute_op(op)
            except GraphAuthError:
                raise
            except Exception as e:
                logger.exception("mount: op %s (%s) failed", op["seq"], op["op_type"])
                self.db.mark_op_error(op["seq"], str(e))

    def _execute_op(self, op: dict) -> None:
        op_type = op["op_type"]

        if op_type == "delete":
            self._execute_delete(op)
            return

        item = self.db.get_item_by_id(self.drive_id, op["item_id"])
        if item is None:
            # already gone locally (e.g. deleted then purged before this
            # op got a chance to run) - nothing left to do
            self.db.delete_op(op["seq"])
            return

        if op_type == "create_file":
            self._execute_create(op, item, is_folder=False)
        elif op_type == "create_dir":
            self._execute_create(op, item, is_folder=True)
        elif op_type == "write":
            self._execute_write(op, item)
        elif op_type == "rename":
            self._execute_rename(op, item)
        else:
            logger.error("mount: unknown op_type %r, dropping", op_type)
            self.db.delete_op(op["seq"])

    # --- individual op executors ---------------------------------------------

    def _execute_create(self, op: dict, item, is_folder: bool) -> None:
        parent = self.db.get_item_by_id(self.drive_id, item.parent_id)
        if parent is None or parent.remote_id is None:
            return  # parent not synced yet - retry next pass, no error recorded
        self.db.mark_op_in_progress(op["seq"])
        try:
            if is_folder:
                result = self.graph.create_folder(self.drive_id, parent.remote_id, item.name)
            else:
                local_path = path_for(self.drive_id, item.id)
                result = self.graph.upload_file(
                    self.drive_id, parent.remote_id, item.name, local_path, existing_item_id=None,
                )
        except GraphConflictError:
            # Both create_folder and upload_file's new-item path use
            # conflictBehavior "fail", so a retried create after a crash
            # between "Graph call succeeded" and this op being cleared
            # (see db.py's retry-idempotency note) gets a clean 409 here -
            # the item the ORIGINAL attempt already created is sitting
            # right there under this exact name. Confirmed live: op 211
            # retried this way on every pass, forever, with no visibility
            # ("1 numara cok onemli" - stuck ops having no visibility was
            # picked as the top priority to fix). Adopt it as this op's
            # result instead of treating an already-successful create as a
            # failure; a genuine name collision with something ELSE
            # (already existed before this op ever ran) still surfaces via
            # the same log_activity/confirm_synced_item path as any
            # ordinary create, no special-casing needed for that case.
            result = self.graph.get_item_by_path(self.drive_id, parent.remote_id, item.name)
            if result is None:
                raise
        self.db.confirm_synced_item(self.drive_id, item.id, result)
        self.db.log_activity("created", item.name, item.path, "mount", is_folder=is_folder)
        self.db.delete_op(op["seq"])

    def _execute_write(self, op: dict, item) -> None:
        if item.remote_id is None:
            return  # its own create hasn't confirmed yet - retry next pass
        self.db.mark_op_in_progress(op["seq"])
        local_path = path_for(self.drive_id, item.id)

        # Graph's simple PUT .../content endpoint (used for anything under
        # SIMPLE_UPLOAD_MAX_BYTES, i.e. most real edits) doesn't honor
        # If-Match at all per its own docs, so that header alone can't be
        # relied on to catch a conflict - an explicit pre-check GET+etag-
        # compare is the actual safety net (same reasoning the old
        # synchronous flush()-side upload used).
        try:
            current = self.graph.get_item(self.drive_id, item.remote_id)
            current_etag = current.get("eTag")
        except GraphAuthError:
            raise
        except Exception:
            current = None
            current_etag = None

        if current is not None and item.etag is not None and current_etag != item.etag:
            self._resolve_write_conflict(item, local_path, current)
        else:
            result = self.graph.upload_file(
                self.drive_id, item.parent_id, item.name, local_path,
                existing_item_id=item.remote_id, if_match=item.etag,
            )
            # confirm_synced_item, NOT upsert_item: a plain content-replace
            # response reflects Graph's CURRENT server-side name/parent,
            # which is stale if a rename for this same item is queued
            # behind this op and hasn't reached Graph yet - upsert_item's
            # full-field overwrite would silently revert that not-yet-
            # synced local rename back to the old name (see the docstring
            # on confirm_synced_item for the exact reproduction).
            self.db.confirm_synced_item(self.drive_id, item.id, result)
            self.db.log_activity("uploaded", item.name, item.path, "mount")
        self.db.delete_op(op["seq"])

    def _execute_rename(self, op: dict, item) -> None:
        parent = self.db.get_item_by_id(self.drive_id, item.parent_id)
        if parent is None or parent.remote_id is None:
            return  # parent not synced yet - retry next pass
        self.db.mark_op_in_progress(op["seq"])
        try:
            result = self.graph.move_or_rename(
                self.drive_id, item.remote_id,
                new_parent_id=parent.remote_id, new_name=item.name, if_match=item.etag,
            )
        except GraphConflictError:
            # A rename's precondition failure is far more often ordinary
            # etag staleness (drifted since this item's cached etag was
            # last refreshed, unrelated to anyone actually editing it) than
            # a genuine conflict - unlike a content write, there's no
            # content here to protect with a keep-both dance. Confirmed
            # live: op 159 412'd every ~60s pass for 15+ minutes until
            # DeltaSyncWorker's independent, much slower poll happened to
            # refresh the stale etag on its own and let the next retry
            # succeed - self-healing here the same way upload_file()
            # already does for its own well-understood conflict class
            # (fetch current state, retry once) turns that into one extra
            # Graph call instead of a multi-minute error-logging stall.
            current = self.graph.get_item(self.drive_id, item.remote_id)
            result = self.graph.move_or_rename(
                self.drive_id, item.remote_id,
                new_parent_id=parent.remote_id, new_name=item.name, if_match=current.get("eTag"),
            )
        self.db.upsert_item(self.drive_id, result)
        self.db.delete_op(op["seq"])

    def _execute_delete(self, op: dict) -> None:
        item_id = op["item_id"]
        remote_id = op["snapshot_remote_id"]
        etag = op["snapshot_etag"]
        if remote_id is None:
            # Enqueued while this item's create was still mid-flight - its
            # real remote id wasn't known yet at that moment (see the
            # in-flight-race note in operations.py's
            # _delete_item_offline_safe). Wait for the create to settle,
            # then look the row up including deleted=1 (mark_deleted
            # already made it invisible to every normal getter).
            if self.db.has_in_progress_op(self.drive_id, item_id):
                return  # still mid-flight - retry next pass
            row = self.db.get_item_by_id_any(self.drive_id, item_id)
            if row is None or row.remote_id is None:
                # the create never actually reached Graph - nothing to delete
                self.db.delete_op(op["seq"])
                return
            remote_id, etag = row.remote_id, row.etag
        self.db.mark_op_in_progress(op["seq"])
        try:
            self.graph.delete_item(self.drive_id, remote_id, if_match=etag)
        except GraphConflictError:
            pass  # already gone or changed underneath us - either way, done
        except requests.exceptions.HTTPError as e:
            # Already gone remotely (e.g. an earlier attempt's DELETE
            # actually landed server-side before a crash/kill kept this op
            # from being cleared - see db.py's retry-idempotency note) -
            # the goal state of a delete op ("this doesn't exist on
            # OneDrive") is already true, so a 404 here is success, not a
            # failure to keep retrying forever. Confirmed live: op 203
            # retried this way on every pass with no GUI visibility at all.
            if e.response is None or e.response.status_code != 404:
                raise
        self.db.delete_op(op["seq"])

    # --- conflict resolution: keep-both, crash-safe ordering -----------------

    def _resolve_write_conflict(self, item, local_path: Path, current_remote: dict) -> None:
        """Relocated from the old operations.py's _resolve_conflict_upload
        (previously run synchronously inside flush()) and extended to match
        pair_worker._resolve_conflict's full 3-step keep-both dance - the
        old mount version stopped after uploading the conflict copy and
        never actually downloaded the current remote content back to the
        original path, leaving the local file's *bytes* stale even though
        its DB metadata said otherwise."""
        p = Path(item.name)
        stem, suffix = p.stem, p.suffix
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H%M%S")
        existing_names = {c.name for c in self.db.list_children(self.drive_id, item.parent_id)}
        conflict_name = constants.CONFLICT_COPY_SUFFIX_FORMAT.format(stem=stem, ts=ts, suffix=suffix)
        n = 1
        while conflict_name in existing_names:
            conflict_name = f"{stem} (conflicted copy {ts}) ({n}){suffix}"
            n += 1

        parent = self.db.get_item_by_id(self.drive_id, item.parent_id)
        parent_remote_id = parent.remote_id if parent is not None else None
        if parent_remote_id is None:
            logger.warning(
                "mount: no synced remote parent for conflict copy of %s, skipping this pass", item.path
            )
            return

        # Step 1: upload the local edit as a brand-new remote file - this is
        # the step that "claims" the conflict; the local edit is preserved
        # from here on regardless of what happens next.
        result = self.graph.upload_file(
            self.drive_id, parent_remote_id, conflict_name, local_path, existing_item_id=None,
        )
        self.db.upsert_item(self.drive_id, result)
        # Full drive-relative path, not the bare conflict_name - every other
        # "mount" activity_log entry stores a full path (item.path), and the
        # tray popup's click-to-open resolves against that convention.
        conflict_full_path = str(Path(item.path).parent / conflict_name) if item.path else conflict_name
        self.db.log_activity("conflict", item.name, conflict_full_path, "mount")
        self.on_conflict(item.name, conflict_full_path)

        # Step 2: download the current remote version over the original
        # local file. If the process dies before this, the next pass just
        # sees a remote-ahead-of-local item at the original path and
        # re-downloads it via the ordinary open()/ensure_cached path -
        # nothing is lost either way. confirm_synced_item, not upsert_item,
        # for the same stale-name reason as the non-conflict upload path
        # above - this is still the SAME original item, its identity and
        # any not-yet-synced local rename must be left alone here.
        self.graph.download_content(self.drive_id, current_remote["id"], local_path)
        self.db.confirm_synced_item(self.drive_id, item.id, current_remote)

        logger.warning(
            "mount: conflict detected for %s - local edit preserved as %s", item.path, conflict_name
        )
