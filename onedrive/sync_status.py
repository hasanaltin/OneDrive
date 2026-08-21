"""Resolves an absolute local filesystem path to this app's notion of that
file's sync status - the single source of truth both the tray popup's badge
(gui/activity_popup.py) and the Dolphin overlay-icon socket server
(dolphin_overlay_server.py) read from, so a file never shows a different
color in one place than the other."""
import datetime
from pathlib import Path

from onedrive.db import Database

LOCAL = "local"
CLOUD = "cloud"
SYNCING = "syncing"


def _mtime_iso(stat_result) -> str:
    # Mirrors sync/pair_worker.py's own _mtime_iso() exactly - duplicated
    # rather than imported to keep this module's import graph light (it's
    # on the hot path for every Dolphin overlay-icon request), not because
    # the logic itself is meant to differ.
    return datetime.datetime.fromtimestamp(
        stat_result.st_mtime, tz=datetime.timezone.utc
    ).isoformat()


def status_for_path(db: Database, mountpoint: Path | None, abs_path: Path) -> str | None:
    """None means "not one of ours" - the caller (an overlay plugin, the
    popup) should show no badge at all rather than guessing.

    A path under the on-demand mount is looked up by its drive-relative
    path in the items cache: content_state == 'ready' means the bytes are
    actually on disk right now (LOCAL), 'downloading' means a transfer is
    in flight this moment (SYNCING), anything else (never fetched, or
    stale because the remote copy changed) means the mount would need a
    fresh network fetch before the content is usable offline (CLOUD) -
    folders never have their own downloadable content, so they're always
    LOCAL. A path under a Folder Pair's local_path is always LOCAL by
    definition: Folder Pairs sync downloads real files to a real local
    folder, there's no on-demand placeholder concept there at all."""
    if mountpoint is not None:
        try:
            rel = abs_path.relative_to(mountpoint)
        except ValueError:
            rel = None
        if rel is not None:
            drive_id = db.get_sync_state("drive_id")
            if not drive_id:
                return None
            drive_rel_path = "" if str(rel) == "." else "/" + rel.as_posix()
            item = db.get_item_by_path(drive_id, drive_rel_path)
            if item is None:
                return None
            if item.is_folder:
                return LOCAL
            if item.content_state == "ready":
                return LOCAL
            if item.content_state == "downloading":
                return SYNCING
            return CLOUD

    for pair in db.list_pairs():
        try:
            rel = abs_path.relative_to(pair.local_path)
        except ValueError:
            continue
        # relative_to only checks the path is syntactically under
        # local_path, not that anything's actually there - without this,
        # a query for a name that doesn't exist under a pair (e.g. Dolphin
        # asking about a just-deleted file mid-refresh) would wrongly
        # claim LOCAL rather than "no opinion."
        if not abs_path.exists():
            return None
        if abs_path.is_dir():
            # Folders have no content of their own to be "mid-sync" -
            # matches the on-demand mount branch above, which is always
            # LOCAL for folders too.
            return LOCAL
        # A file whose on-disk mtime/size doesn't match what PairSyncWorker
        # last confirmed synced (sync/reconcile.py's own
        # _local_matches_synced check, reused here) covers both "just
        # edited, not uploaded yet" and "upload/download actually in
        # flight right now" - it's the same window, since pair_files only
        # gets updated once the corresponding Graph call actually succeeds
        # (see pair_worker._execute_action). No pair_files row at all
        # means "never confirmed synced" too, e.g. right after local
        # creation - also SYNCING, not LOCAL.
        try:
            st = abs_path.stat()
        except OSError:
            return None
        pair_file = db.get_pair_file(pair.id, rel.as_posix())
        if (
            pair_file is None
            or pair_file.last_synced_mtime != _mtime_iso(st)
            or pair_file.last_synced_size != st.st_size
        ):
            return SYNCING
        return LOCAL

    return None
