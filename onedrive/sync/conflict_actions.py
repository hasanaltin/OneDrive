import datetime
import logging
import os
from pathlib import Path, PurePosixPath

from onedrive.content_cache import path_for
from onedrive.db import Database
from onedrive.graph_client import GraphClient

logger = logging.getLogger(__name__)


def _mtime_iso(stat_result) -> str:
    return datetime.datetime.fromtimestamp(
        stat_result.st_mtime, tz=datetime.timezone.utc
    ).isoformat()


def _original_rel_path(conflict_row: dict) -> str:
    """The conflicted-copy path (conflict_row['path']) always lives in the
    same parent directory as the original file (see
    pair_worker._unique_conflict_path) - so the original's rel_path is just
    that parent joined with the original name (conflict_row['name'])."""
    parent = PurePosixPath(conflict_row["path"]).parent
    if str(parent) == ".":
        return conflict_row["name"]
    return (parent / conflict_row["name"]).as_posix()


def resolve_pair_conflict(db: Database, graph: GraphClient, pair, conflict_row: dict, decision: str) -> None:
    """Acts on one already-auto-resolved "keep both" conflict for a Folder
    Pair, per the user's review choice:

    - 'dismiss': keep both files exactly as they are, just stop showing this
      conflict in the review list.
    - 'keep_server': discard the local edit - delete the conflicted-copy
      file (locally and on OneDrive, which moves it to the recycle bin
      rather than hard-deleting it).
    - 'keep_local': restore the local edit over the original path - upload
      the conflicted-copy's content as the new version of the *original*
      remote item, replace the original local file's bytes with it, then
      remove the now-redundant conflicted copy.

    Only the final step (dismiss_conflict) runs on success, so a failure
    partway through (e.g. a network error) leaves the conflict listed for
    the user to retry rather than silently disappearing."""
    conflict_rel = conflict_row["path"]
    original_rel = _original_rel_path(conflict_row)
    conflict_abs = Path(pair.local_path) / conflict_rel
    original_abs = Path(pair.local_path) / original_rel

    if decision == "dismiss":
        db.dismiss_conflict(conflict_row["id"])
        return

    if decision == "keep_server":
        conflict_pf = db.get_pair_file(pair.id, conflict_rel)
        if conflict_pf is not None and conflict_pf.remote_item_id:
            graph.delete_item(pair.drive_id, conflict_pf.remote_item_id)
            # Tombstone it in the items cache immediately, rather than
            # waiting for DeltaSyncWorker's next poll - otherwise the very
            # next reconciliation pass can still see this path in
            # list_descendants() with no pair_files baseline (just purged
            # below), misclassify it as "new, remote-only", and try to
            # download an item that's already gone (harmless - logged and
            # self-heals next pass - but avoidable).
            db.mark_deleted(pair.drive_id, conflict_pf.remote_item_id)
        if conflict_abs.exists():
            conflict_abs.unlink()
        db.purge_pair_file(pair.id, conflict_rel)
        db.log_activity("deleted", os.path.basename(conflict_rel), conflict_rel, f"pair:{pair.id}")
        db.dismiss_conflict(conflict_row["id"])
        logger.info("pair %s: conflict on %s resolved - kept server version", pair.id, original_rel)
        return

    if decision == "keep_local":
        if not conflict_abs.exists():
            raise FileNotFoundError(f"conflicted copy no longer exists: {conflict_rel}")
        original_pf = db.get_pair_file(pair.id, original_rel)
        if original_pf is None or not original_pf.remote_item_id:
            raise RuntimeError(f"original file is not currently tracked by this pair: {original_rel}")

        # Upload first - this is the only step that can fail on a network/
        # conflict error, and until it succeeds nothing else has been
        # touched, so a failed attempt is always safe to just retry.
        result = graph.upload_file(
            pair.drive_id, pair.remote_item_id, os.path.basename(original_rel), conflict_abs,
            existing_item_id=original_pf.remote_item_id, if_match=original_pf.last_synced_etag,
        )
        db.upsert_item(pair.drive_id, result)

        original_abs.parent.mkdir(parents=True, exist_ok=True)
        os.replace(conflict_abs, original_abs)  # atomic - also removes conflict_abs

        st = original_abs.stat()
        db.upsert_pair_file(
            pair.id, original_rel, remote_item_id=result["id"], last_synced_etag=result.get("eTag"),
            last_synced_mtime=_mtime_iso(st), last_synced_size=st.st_size, is_folder=False,
        )

        conflict_pf = db.get_pair_file(pair.id, conflict_rel)
        if conflict_pf is not None and conflict_pf.remote_item_id:
            try:
                graph.delete_item(pair.drive_id, conflict_pf.remote_item_id)
                db.mark_deleted(pair.drive_id, conflict_pf.remote_item_id)
            except Exception:
                logger.warning(
                    "pair %s: keep_local resolved but failed to remove the now-redundant "
                    "conflict copy remotely - it may need manual cleanup", pair.id, exc_info=True,
                )
        db.purge_pair_file(pair.id, conflict_rel)
        db.log_activity("uploaded", os.path.basename(original_rel), original_rel, f"pair:{pair.id}")
        db.dismiss_conflict(conflict_row["id"])
        logger.info("pair %s: conflict on %s resolved - kept local version", pair.id, original_rel)
        return

    raise ValueError(f"unknown conflict resolution decision: {decision!r}")


def resolve_mount_conflict(db: Database, graph: GraphClient, drive_id: str, conflict_row: dict, decision: str) -> None:
    """Same three decisions and same "both versions are already preserved"
    premise as resolve_pair_conflict() above, for one already-auto-resolved
    "keep both" conflict raised by the on-demand mount's offline write path
    (see mount_sync_worker.MountSyncWorker._resolve_write_conflict).

    The mount has no pair_files table and no real local directory tree of
    its own - FUSE serves everything from the whole-account `items` cache
    plus content_cache, so this reads/writes those directly instead of a
    pair's local filesystem paths."""
    conflict_rel = conflict_row["path"]
    original_rel = _original_rel_path(conflict_row)

    if decision == "dismiss":
        db.dismiss_conflict(conflict_row["id"])
        return

    if decision == "keep_server":
        conflict_item = db.get_item_by_path(drive_id, conflict_rel)
        if conflict_item is not None:
            if conflict_item.remote_id:
                graph.delete_item(drive_id, conflict_item.remote_id)
            db.mark_deleted(drive_id, conflict_item.id)
            cached = path_for(drive_id, conflict_item.id)
            if cached.exists():
                cached.unlink()
        db.log_activity("deleted", os.path.basename(conflict_rel), conflict_rel, "mount")
        db.dismiss_conflict(conflict_row["id"])
        logger.info("mount: conflict on %s resolved - kept server version", original_rel)
        return

    if decision == "keep_local":
        conflict_item = db.get_item_by_path(drive_id, conflict_rel)
        if conflict_item is None or not conflict_item.remote_id:
            raise FileNotFoundError(f"conflicted copy no longer exists: {conflict_rel}")
        conflict_local_path = path_for(drive_id, conflict_item.id)
        if not conflict_local_path.exists():
            # The conflict copy's bytes were uploaded straight from the
            # ORIGINAL item's own cache slot at creation time (see
            # mount_sync_worker._resolve_write_conflict) - its own cache
            # slot is never separately populated unless something has
            # opened/read it since (content_state stays the upsert_item
            # default of 'none'). Download fresh rather than assuming it's
            # already local - same lazy-fetch the mount's own open() path
            # already relies on everywhere else.
            conflict_local_path.parent.mkdir(parents=True, exist_ok=True)
            graph.download_content(drive_id, conflict_item.remote_id, conflict_local_path)

        original_item = db.get_item_by_path(drive_id, original_rel)
        if original_item is None or not original_item.remote_id:
            raise RuntimeError(f"original file is not currently tracked by the mount: {original_rel}")

        # Upload first - this is the only step that can fail on a network/
        # conflict error, and until it succeeds nothing else has been
        # touched, so a failed attempt is always safe to just retry.
        result = graph.upload_file(
            drive_id, original_item.parent_id, original_item.name, conflict_local_path,
            existing_item_id=original_item.remote_id, if_match=original_item.etag,
        )
        db.confirm_synced_item(drive_id, original_item.id, result)

        original_local_path = path_for(drive_id, original_item.id)
        original_local_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(conflict_local_path, original_local_path)  # atomic - also removes conflict_local_path

        if conflict_item.remote_id:
            try:
                graph.delete_item(drive_id, conflict_item.remote_id)
                db.mark_deleted(drive_id, conflict_item.id)
            except Exception:
                logger.warning(
                    "mount: keep_local resolved but failed to remove the now-redundant "
                    "conflict copy remotely - it may need manual cleanup", exc_info=True,
                )
        db.log_activity("uploaded", original_item.name, original_rel, "mount")
        db.dismiss_conflict(conflict_row["id"])
        logger.info("mount: conflict on %s resolved - kept local version", original_rel)
        return

    raise ValueError(f"unknown conflict resolution decision: {decision!r}")
