import datetime
import logging
import sqlite3
import threading
from dataclasses import dataclass

from onedrive import constants

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    drive_id       TEXT NOT NULL,
    id             TEXT NOT NULL,
    remote_id      TEXT,
    parent_id      TEXT,
    name           TEXT NOT NULL,
    path           TEXT,
    is_folder      INTEGER NOT NULL DEFAULT 0,
    size           INTEGER NOT NULL DEFAULT 0,
    etag           TEXT,
    ctag           TEXT,
    mtime          TEXT,
    ctime          TEXT,
    quickxor_hash  TEXT,
    child_count    INTEGER,
    content_state  TEXT NOT NULL DEFAULT 'none',
    is_pinned      INTEGER NOT NULL DEFAULT 0,
    deleted        INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT,
    PRIMARY KEY (drive_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_path      ON items(drive_id, path) WHERE deleted = 0;
CREATE INDEX        IF NOT EXISTS idx_items_parent    ON items(drive_id, parent_id);
-- idx_items_remote_id is created in _migrate(), not here: on a database that
-- already has an `items` table, CREATE TABLE IF NOT EXISTS above is a no-op
-- and doesn't add the remote_id column, but CREATE INDEX IF NOT EXISTS is
-- NOT a no-op - it would fail with "no such column" since _migrate()'s
-- ALTER TABLE (which actually adds the column for pre-existing databases)
-- hasn't run yet at this point in __init__.

-- Queue of offline mount writes (create/mkdir/write/delete/rename through the
-- on-demand mount) waiting to be applied to Microsoft Graph. Every op type
-- except 'delete' reads the item's *current* live state from `items` at
-- execution time rather than snapshotting a payload here - a create's
-- target name/parent is just whatever's live in the DB when the worker gets
-- to it, so e.g. a rename issued before the create has even synced needs no
-- special-casing. 'delete' is the exception: mark_deleted makes the row
-- invisible to every getter that filters deleted=0, so its target must be
-- snapshotted at enqueue time or the worker has nothing to act on.
CREATE TABLE IF NOT EXISTS pending_mount_ops (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_id          TEXT NOT NULL,
    op_type           TEXT NOT NULL,
    item_id           TEXT NOT NULL,
    snapshot_remote_id TEXT,
    snapshot_etag     TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    last_error        TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_mount_ops_item ON pending_mount_ops(drive_id, item_id);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS folder_pairs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    local_path        TEXT NOT NULL UNIQUE,
    drive_id          TEXT NOT NULL,
    remote_item_id    TEXT NOT NULL,
    remote_path       TEXT NOT NULL,
    conflict_policy   TEXT NOT NULL DEFAULT 'keep_both',
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    last_sync_at      TEXT,
    last_sync_status  TEXT NOT NULL DEFAULT 'idle',
    conflict_count    INTEGER NOT NULL DEFAULT 0,
    exclude_patterns  TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pairs_remote ON folder_pairs(drive_id, remote_item_id);

CREATE TABLE IF NOT EXISTS pair_files (
    pair_id            INTEGER NOT NULL REFERENCES folder_pairs(id),
    rel_path           TEXT NOT NULL,
    remote_item_id     TEXT,
    last_synced_etag   TEXT,
    last_synced_mtime  TEXT,
    last_synced_size   INTEGER,
    is_folder          INTEGER NOT NULL DEFAULT 0,
    deleted            INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (pair_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_pair_files_remote ON pair_files(pair_id, remote_item_id);

CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    name        TEXT NOT NULL,
    path        TEXT,
    source      TEXT NOT NULL,
    is_folder   INTEGER NOT NULL DEFAULT 0,
    dismissed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_activity_log_ts ON activity_log(ts DESC);
"""

DEFAULT_EXCLUDE_PATTERNS = "\n".join([
    ".sync_*.db*",
    "*.tmp",
    "~$*",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    # Other sync clients' own bookkeeping files, if a pair overlaps a folder
    # they also manage (e.g. Nextcloud). Without these, two sync clients
    # can end up continuously re-uploading each other's metadata churn back
    # and forth - each client's own log/db write looks like a "real" file
    # change to the other.
    "*.db-wal",
    "*.db-shm",
    "*.db",
    "*.log",
    ".nextcloud*",
    # Editor/app-generated transient files - reported directly, with two
    # confirmed real examples: a Kate swap file and a Chrome PWA shortcut
    # file both showing up as repeating create/delete activity for the
    # same underlying open document/session, purely from being open, not
    # from any real content change worth syncing.
    "*.swp", "*.swo", "*.swx", "*.kate-swp", "*~", ".goutputstream-*",
    "chrome-*.desktop",
    "*.part", "*.crdownload",
    # LibreOffice/OpenOffice's own lock file for an open document
    # (".~lock.<name>#") - confirmed live: the trailing "#" is an illegal
    # OneDrive filename character, so every upload attempt 400'd and
    # retried forever on every sync pass for as long as the document
    # stayed open, purely from having it open, not from any real content
    # worth syncing (same class of problem as the editor swap files above).
    ".~lock.*#",
    # Developer-project internals: regenerable/reproducible, not meant to be
    # backed up this way (git history belongs on an actual git remote, not
    # replicated via a general-purpose two-way file sync - .git's own
    # object-store churn during a commit/gc doesn't play well with being
    # treated as ordinary files anyway), and often huge (a venv or
    # node_modules can be tens of thousands of files) for no benefit.
    ".git", ".venv", "venv", "node_modules", "__pycache__",
])


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class Item:
    drive_id: str
    id: str
    remote_id: str | None
    parent_id: str | None
    name: str
    path: str
    is_folder: bool
    size: int
    etag: str | None
    ctag: str | None
    mtime: str | None
    ctime: str | None
    quickxor_hash: str | None
    child_count: int | None
    content_state: str
    is_pinned: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Item":
        return cls(
            drive_id=row["drive_id"],
            id=row["id"],
            remote_id=row["remote_id"] if "remote_id" in row.keys() else row["id"],
            parent_id=row["parent_id"],
            name=row["name"],
            path=row["path"],
            is_folder=bool(row["is_folder"]),
            size=row["size"],
            etag=row["etag"],
            ctag=row["ctag"],
            mtime=row["mtime"],
            ctime=row["ctime"],
            quickxor_hash=row["quickxor_hash"],
            child_count=row["child_count"],
            content_state=row["content_state"],
            is_pinned=bool(row["is_pinned"]),
        )


@dataclass
class Pair:
    # folder_pairs.conflict_policy (schema-level default 'keep_both') is
    # deliberately NOT exposed here - nothing anywhere ever reads it or
    # writes a non-default value, so keeping it on this dataclass would
    # misleadingly suggest prefer_local/prefer_remote are real, selectable
    # options when only keep_both is actually implemented. The column
    # itself is left alone (SQLite has no cheap DROP COLUMN, and there's no
    # behavioral gain to migrating it away) - just not surfaced in Python.
    id: int
    local_path: str
    drive_id: str
    remote_item_id: str
    remote_path: str
    enabled: bool
    created_at: str
    last_sync_at: str | None
    last_sync_status: str
    conflict_count: int
    exclude_patterns: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Pair":
        return cls(
            id=row["id"],
            local_path=row["local_path"],
            drive_id=row["drive_id"],
            remote_item_id=row["remote_item_id"],
            remote_path=row["remote_path"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            last_sync_at=row["last_sync_at"],
            last_sync_status=row["last_sync_status"],
            conflict_count=row["conflict_count"],
            exclude_patterns=row["exclude_patterns"] if "exclude_patterns" in row.keys() else "",
        )


@dataclass
class PairFile:
    pair_id: int
    rel_path: str
    remote_item_id: str | None
    last_synced_etag: str | None
    last_synced_mtime: str | None
    last_synced_size: int | None
    is_folder: bool
    deleted: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PairFile":
        return cls(
            pair_id=row["pair_id"],
            rel_path=row["rel_path"],
            remote_item_id=row["remote_item_id"],
            last_synced_etag=row["last_synced_etag"],
            last_synced_mtime=row["last_synced_mtime"],
            last_synced_size=row["last_synced_size"],
            is_folder=bool(row["is_folder"]),
            deleted=bool(row["deleted"]),
        )


class Database:
    def __init__(self, db_path=None):
        constants.ensure_dirs()
        path = str(db_path or constants.DB_PATH)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Lightweight, idempotent migrations for columns added after a
        table already existed on disk - CREATE TABLE IF NOT EXISTS in SCHEMA
        only helps brand-new databases."""
        try:
            self._conn.execute(
                "ALTER TABLE folder_pairs ADD COLUMN exclude_patterns TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self._conn.execute("ALTER TABLE items ADD COLUMN remote_id TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Every pre-existing row predates this column and no old call site
        # ever set it - backfill remote_id=id for everything EXCEPT rows that
        # are genuinely still-pending offline creates (id LIKE 'pending:%'),
        # which must stay NULL. Safe to re-run: already-backfilled rows just
        # get set to their own current value again.
        self._conn.execute(
            "UPDATE items SET remote_id = id WHERE remote_id IS NULL AND id NOT LIKE 'pending:%'"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_remote_id ON items(drive_id, remote_id)"
        )
        try:
            self._conn.execute("ALTER TABLE activity_log ADD COLUMN is_folder INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self._conn.execute("ALTER TABLE activity_log ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        # A retired feature's now-unused table - drop it entirely on any
        # existing install that already created it, rather than leaving a
        # dormant table around.
        self._conn.execute("DROP TABLE IF EXISTS item_share_status")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- sync_state -----------------------------------------------------

    def get_sync_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM sync_state WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_sync_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sync_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def delete_sync_state(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sync_state WHERE key=?", (key,))
            self._conn.commit()

    # --- item reads -------------------------------------------------------

    def get_item_by_path(self, drive_id: str, path: str) -> Item | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM items WHERE drive_id=? AND path=? AND deleted=0",
                (drive_id, path),
            ).fetchone()
            return Item.from_row(row) if row else None

    def get_item_by_id(self, drive_id: str, item_id: str) -> Item | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM items WHERE drive_id=? AND id=? AND deleted=0",
                (drive_id, item_id),
            ).fetchone()
            return Item.from_row(row) if row else None

    def get_item_by_id_any(self, drive_id: str, item_id: str) -> Item | None:
        """Same as get_item_by_id but WITHOUT the deleted=0 filter - used
        only by MountSyncWorker's delete-op handler to recover an item's
        remote_id after the in-flight create-vs-delete race (see
        operations.py's _delete_item_offline_safe): by the time that delete
        op runs, mark_deleted has already made the row invisible to every
        normal getter, but its remote_id may have only just been set by a
        create that was still mid-flight at the moment the delete was
        requested."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM items WHERE drive_id=? AND id=?", (drive_id, item_id)
            ).fetchone()
            return Item.from_row(row) if row else None

    def list_children_names(self, drive_id: str, parent_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM items WHERE drive_id=? AND parent_id=? AND deleted=0 "
                "ORDER BY is_folder DESC, name COLLATE NOCASE",
                (drive_id, parent_id),
            ).fetchall()
            return [r["name"] for r in rows]

    def list_children(self, drive_id: str, parent_id: str) -> list[Item]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE drive_id=? AND parent_id=? AND deleted=0",
                (drive_id, parent_id),
            ).fetchall()
            return [Item.from_row(r) for r in rows]

    def list_top_level_folders(self, drive_id: str) -> list[Item]:
        with self._lock:
            root = self._conn.execute(
                "SELECT id FROM items WHERE drive_id=? AND path='' AND deleted=0",
                (drive_id,),
            ).fetchone()
            if not root:
                return []
            rows = self._conn.execute(
                "SELECT * FROM items WHERE drive_id=? AND parent_id=? AND is_folder=1 AND deleted=0 "
                "ORDER BY name COLLATE NOCASE",
                (drive_id, root["id"]),
            ).fetchall()
            return [Item.from_row(r) for r in rows]

    def get_pinned_folders(self) -> list[Item]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE is_pinned=1 AND is_folder=1 AND deleted=0"
            ).fetchall()
            return [Item.from_row(r) for r in rows]

    def count_pending_pinned_downloads(self) -> int:
        """How many files under any pinned folder haven't been downloaded
        yet (content_state 'none' or 'stale') - powers the tray popup's
        "Downloading pinned files..." status. PinWorker (the on-demand
        mount's eager pre-download for pinned folders) never touches
        folder_pairs.last_sync_status at all - it's a completely separate
        mechanism from Folder Pairs sync, so the popup's existing progress
        tracking (keyed entirely off that column) had no visibility into
        it whatsoever. Reported directly: files were visibly downloading
        with zero indication of that anywhere in the popup (files were
        visibly syncing but there was no way to see how much longer it
        would take). Reuses list_descendants() - a plain
        indexed parent_id walk - rather than a recursive CTE, for the same
        reason documented on that method: a CTE here measured 60+ seconds
        on this account's item count."""
        with self._lock:
            pinned_folders = self._conn.execute(
                "SELECT id, drive_id FROM items WHERE is_pinned=1 AND is_folder=1 AND deleted=0"
            ).fetchall()
        count = 0
        for folder in pinned_folders:
            for item in self.list_descendants(folder["drive_id"], folder["id"]):
                if not item.is_folder and item.content_state in ("none", "stale"):
                    count += 1
        return count

    def item_count(self, drive_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM items WHERE drive_id=? AND deleted=0", (drive_id,)
            ).fetchone()
            return row["c"]

    # --- item writes ------------------------------------------------------

    def set_pinned(self, drive_id: str, item_id: str, pinned: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE items SET is_pinned=? WHERE drive_id=? AND id=?",
                (int(pinned), drive_id, item_id),
            )
            self._conn.commit()

    def set_content_state(self, drive_id: str, item_id: str, state: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE items SET content_state=? WHERE drive_id=? AND id=?",
                (state, drive_id, item_id),
            )
            self._conn.commit()

    def mark_deleted(self, drive_id: str, item_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE items SET deleted=1 WHERE drive_id=? AND id=?", (drive_id, item_id)
            )
            self._conn.commit()

    def upsert_item(self, drive_id: str, graph_item: dict) -> None:
        """Applies one Graph delta item (create/update/tombstone) to the cache,
        recomputing this item's path (and cascading to descendants on rename/move)."""
        with self._lock:
            cur = self._conn.cursor()

            if "deleted" in graph_item:
                cur.execute(
                    "UPDATE items SET deleted=1 WHERE drive_id=? AND (id=? OR remote_id=?)",
                    (drive_id, graph_item["id"], graph_item["id"]),
                )
                self._conn.commit()
                return

            remote_id = graph_item["id"]
            name = graph_item.get("name") or ""
            is_root = "root" in graph_item
            parent_ref = graph_item.get("parentReference") or {}
            parent_id = None if is_root else parent_ref.get("id")
            if parent_id is not None:
                # Graph always reports a parent by its REAL id, but a still-
                # offline-pending parent's local row keeps id="pending:xxx"
                # forever (only remote_id becomes the real id once it syncs -
                # see the class docstring at the top of this file). Without
                # this translation, the first upsert_item() for any child of
                # a still-pending folder (e.g. a mount edit's own upload
                # response, or the next delta poll) would silently set
                # parent_id to a value that matches no row's id column,
                # orphaning the child from list_children()/readdir() even
                # though its row is otherwise perfectly intact (deleted=0).
                # For an already-synced normal parent this is a no-op: its
                # remote_id already equals its id, so the lookup just finds
                # the same row and returns the same value.
                parent_local = cur.execute(
                    "SELECT id FROM items WHERE drive_id=? AND remote_id=?", (drive_id, parent_id)
                ).fetchone()
                if parent_local is not None:
                    parent_id = parent_local["id"]
            is_folder = "folder" in graph_item
            size = graph_item.get("size", 0)
            etag = graph_item.get("eTag")
            ctag = graph_item.get("cTag")
            fsi = graph_item.get("fileSystemInfo") or {}
            mtime = fsi.get("lastModifiedDateTime") or graph_item.get("lastModifiedDateTime")
            ctime = fsi.get("createdDateTime") or graph_item.get("createdDateTime")
            quickxor = ((graph_item.get("file") or {}).get("hashes") or {}).get("quickXorHash")
            child_count = (graph_item.get("folder") or {}).get("childCount")

            existing = cur.execute(
                "SELECT id, path, etag FROM items WHERE drive_id=? AND id=?", (drive_id, remote_id)
            ).fetchone()
            if existing is None:
                # Not found under its own Graph id - could be a locally-created
                # item (id="pending:xxx") whose upload just succeeded and is
                # now being confirmed under its real id for the first time
                # (via MountSyncWorker's own upsert, or the very next delta
                # poll picking it up independently). Reusing that row's id
                # instead of inserting under the real Graph id keeps the
                # FUSE-facing identity (inode mapping, content_cache path)
                # stable - without this fallback, the next delta poll after
                # any offline-created item syncs would insert a second row
                # for the same logical file and crash on idx_items_path's
                # unique index the moment both rows share the same path.
                existing = cur.execute(
                    "SELECT id, path, etag FROM items WHERE drive_id=? AND remote_id=? AND id != ?",
                    (drive_id, remote_id, remote_id),
                ).fetchone()
            item_id = existing["id"] if existing else remote_id
            old_path = existing["path"] if existing else None

            if is_root:
                new_path = ""
            elif parent_id is None:
                new_path = old_path  # can't resolve without a parent reference
            else:
                parent_row = cur.execute(
                    "SELECT path FROM items WHERE drive_id=? AND id=?", (drive_id, parent_id)
                ).fetchone()
                if parent_row and parent_row["path"] is not None:
                    new_path = parent_row["path"] + "/" + name
                else:
                    new_path = old_path  # parent not seen yet; resolved in a later pass

            # keep 'ready' content but mark it stale if the remote item actually changed,
            # so an in-flight open() keeps working off the last-good bytes
            content_state_sql = (
                "CASE WHEN content_state='ready' AND etag IS NOT ? THEN 'stale' ELSE content_state END"
            )

            try:
                cur.execute(
                    f"""
                    INSERT INTO items (drive_id, id, remote_id, parent_id, name, path, is_folder, size, etag, ctag,
                                        mtime, ctime, quickxor_hash, child_count, deleted, last_synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(drive_id, id) DO UPDATE SET
                        remote_id=excluded.remote_id,
                        parent_id=excluded.parent_id,
                        name=excluded.name,
                        path=excluded.path,
                        is_folder=excluded.is_folder,
                        size=excluded.size,
                        content_state={content_state_sql},
                        etag=excluded.etag,
                        ctag=excluded.ctag,
                        mtime=excluded.mtime,
                        ctime=excluded.ctime,
                        quickxor_hash=excluded.quickxor_hash,
                        child_count=excluded.child_count,
                        deleted=0,
                        last_synced_at=excluded.last_synced_at
                    """,
                    (
                        drive_id, item_id, remote_id, parent_id, name, new_path, int(is_folder), size,
                        etag, ctag, mtime, ctime, quickxor, child_count, now_iso(),
                        etag,
                    ),
                )
            except sqlite3.IntegrityError:
                # idx_items_path (drive_id, path) rejected this - some OTHER
                # row already claims new_path under a different id. Confirmed
                # as a real, reachable case (not just theoretical): Graph's
                # own delta feed returning two distinct items claiming the
                # same path, which crashed the whole delta pass on every
                # single retry - because the exception aborted _sync_once()
                # before its delta_link save, the NEXT poll re-fetched the
                # exact same problem item and crashed identically, forever.
                # Rolling back and skipping this one item lets the rest of
                # the batch - and delta_link's advancement - proceed, rather
                # than the one bad item taking down the whole account sync
                # indefinitely. Logged with enough detail to investigate
                # later; not auto-resolved, since deciding which of two
                # same-path items is "right" isn't safe to guess here.
                self._conn.rollback()
                colliding = cur.execute(
                    "SELECT id, remote_id FROM items WHERE drive_id=? AND path=? AND deleted=0",
                    (drive_id, new_path),
                ).fetchone()
                logger.warning(
                    "upsert_item: path collision for %r - incoming id=%s remote_id=%s vs already-cached "
                    "id=%s remote_id=%s at that path; skipping this item this pass",
                    new_path, item_id, remote_id,
                    colliding["id"] if colliding else None,
                    colliding["remote_id"] if colliding else None,
                )
                return

            if old_path is not None and new_path is not None and old_path != new_path:
                self._cascade_path_update(cur, drive_id, item_id, old_path, new_path)

            self._conn.commit()

    def _cascade_path_update(self, cur, drive_id: str, item_id: str, old_path: str, new_path: str) -> None:
        children = cur.execute(
            "SELECT id, path FROM items WHERE drive_id=? AND parent_id=?", (drive_id, item_id)
        ).fetchall()
        for child in children:
            child_old = child["path"]
            if child_old is None:
                continue
            child_new = new_path + child_old[len(old_path):]
            cur.execute(
                "UPDATE items SET path=? WHERE drive_id=? AND id=?",
                (child_new, drive_id, child["id"]),
            )
            self._cascade_path_update(cur, drive_id, child["id"], child_old, child_new)

    def rename_item_local(self, drive_id: str, item_id: str, new_parent_id: str, new_name: str) -> None:
        """Updates an item's name/parent/path locally, immediately, no
        network - used by the FUSE mount's rename() so an offline move/
        rename is visible right away. The actual Graph move_or_rename call
        (if the item is already synced) happens later via a queued
        pending_mount_ops row."""
        with self._lock:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT path FROM items WHERE drive_id=? AND id=?", (drive_id, item_id)
            ).fetchone()
            if row is None:
                return
            old_path = row["path"]
            parent_row = cur.execute(
                "SELECT path FROM items WHERE drive_id=? AND id=?", (drive_id, new_parent_id)
            ).fetchone()
            new_path = (
                parent_row["path"] + "/" + new_name
                if parent_row and parent_row["path"] is not None
                else old_path
            )
            cur.execute(
                "UPDATE items SET parent_id=?, name=?, path=? WHERE drive_id=? AND id=?",
                (new_parent_id, new_name, new_path, drive_id, item_id),
            )
            if old_path is not None and new_path is not None and old_path != new_path:
                self._cascade_path_update(cur, drive_id, item_id, old_path, new_path)
            self._conn.commit()

    def update_local_content(self, drive_id: str, item_id: str, size: int, mtime: str) -> None:
        """Called immediately when an offline edit is flushed - keeps
        size/mtime in sync with the locally-staged bytes right away,
        independent of whether the queued Graph upload has actually run
        yet. Without this, ensure_cached's cache-hit check
        (content_state=='ready' AND local size == db size) fails against a
        stale DB size if the file is reopened before MountSyncWorker
        catches up, incorrectly re-triggering a "download" of an item
        that's already fully present locally."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET size=?, mtime=?, content_state='ready' WHERE drive_id=? AND id=?",
                (size, mtime, drive_id, item_id),
            )
            self._conn.commit()

    def insert_pending_item(
        self, drive_id: str, item_id: str, parent_id: str, name: str, is_folder: bool
    ) -> None:
        """Inserts a brand-new, not-yet-synced row for an offline mount
        create()/mkdir() - deliberately NOT routed through upsert_item(),
        which always sets remote_id equal to the graph_item dict's "id" and
        has no way to leave it NULL. Here remote_id must stay NULL until
        confirm_synced_item() sets it once the real Graph create/upload
        actually succeeds."""
        with self._lock:
            cur = self._conn.cursor()
            parent_row = cur.execute(
                "SELECT path FROM items WHERE drive_id=? AND id=?", (drive_id, parent_id)
            ).fetchone()
            parent_path = parent_row["path"] if parent_row else None
            path = (parent_path + "/" + name) if parent_path is not None else None
            cur.execute(
                "INSERT INTO items (drive_id, id, remote_id, parent_id, name, path, is_folder, "
                "size, content_state, deleted, last_synced_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, 0, 'ready', 0, ?)",
                (drive_id, item_id, parent_id, name, path, int(is_folder), now_iso()),
            )
            self._conn.commit()

    def confirm_synced_item(self, drive_id: str, item_id: str, graph_item: dict) -> None:
        """Called by MountSyncWorker right after a create/upload (or a plain
        content re-upload of an already-synced item) succeeds. Updates
        content-related fields only (remote_id, etag/ctag/size/timestamps,
        content_state) and deliberately leaves name/parent_id/path alone -
        for two distinct reasons depending on the caller:

        - For the first-ever confirmation of a "pending:xxx" item:
          upsert_item()'s remote_id fallback lookup can't be used here - it
          searches for a row whose remote_id already equals the incoming id,
          but this is the exact call that's supposed to SET remote_id for
          the first time, so that lookup can never match, and inserting
          under the real Graph id instead would collide with idx_items_path
          (same bug this whole design exists to avoid). Since the caller
          already knows item_id precisely, this updates that exact row
          directly. (upsert_item's fallback lookup is still worth keeping -
          it's what protects the NEXT delta poll, once this call has
          already set remote_id, from inserting a second row.)
        - For a content-only re-upload of an item that was ALREADY synced:
          a plain content replace's Graph response reflects the item's
          CURRENT server-side name/parent, which can be stale relative to a
          rename that's been applied locally but is still queued behind
          this op (not yet sent to Graph) - using the general-purpose
          upsert_item() here would silently revert that not-yet-synced
          local rename back to the old name. Reproduced directly: edit a
          file then immediately rename it offline: the queued write ran
          before the queued rename could reach Graph, its stale response
          named the file with the OLD name, and upsert_item() stomped the
          already-renamed local row back to it - the rename op then read
          that same corrupted name and "renamed" the file to itself."""
        with self._lock:
            remote_id = graph_item["id"]
            etag = graph_item.get("eTag")
            ctag = graph_item.get("cTag")
            size = graph_item.get("size", 0)
            fsi = graph_item.get("fileSystemInfo") or {}
            mtime = fsi.get("lastModifiedDateTime") or graph_item.get("lastModifiedDateTime")
            ctime = fsi.get("createdDateTime") or graph_item.get("createdDateTime")
            quickxor = ((graph_item.get("file") or {}).get("hashes") or {}).get("quickXorHash")
            self._conn.execute(
                "UPDATE items SET remote_id=?, etag=?, ctag=?, size=?, mtime=?, ctime=?, "
                "quickxor_hash=?, content_state='ready', last_synced_at=? WHERE drive_id=? AND id=?",
                (remote_id, etag, ctag, size, mtime, ctime, quickxor, now_iso(), drive_id, item_id),
            )
            self._conn.commit()

    def resolve_pending_paths(self, drive_id: str) -> None:
        """After applying a batch of delta items, some children may have been
        upserted before their parent (path left unresolved as NULL/stale-old).
        Repeatedly resolves any item whose parent now has a known path."""
        with self._lock:
            cur = self._conn.cursor()
            while True:
                cur.execute(
                    """
                    UPDATE items
                    SET path = (
                        SELECT p.path || '/' || items.name
                        FROM items p
                        WHERE p.drive_id = items.drive_id AND p.id = items.parent_id
                    )
                    WHERE drive_id = ?
                      AND path IS NULL
                      AND parent_id IN (
                          SELECT id FROM items WHERE drive_id = ? AND path IS NOT NULL
                      )
                    """,
                    (drive_id, drive_id),
                )
                changed = cur.rowcount
                self._conn.commit()
                if changed <= 0:
                    break

    def list_descendants(self, drive_id: str, root_id: str) -> list[Item]:
        """All items (any depth) under root_id, purely from the local cache -
        zero network calls. This is how Folder Pairs reuses DeltaSyncWorker's
        continuously-fresh whole-account cache for remote-side reconciliation
        instead of polling /delta separately per pair.

        Deliberately NOT a SQL recursive CTE: on a large account (~150k+
        items) SQLite's query planner picks the wrong index for the
        recursive step's join (idx_items_path instead of idx_items_parent,
        confirmed via EXPLAIN QUERY PLAN, and an INDEXED BY hint doesn't fix
        it either), turning it into a near-full-table-scan per recursion
        level - 60+ seconds for a 681-item subtree out of 158k rows, run
        on every single reconciliation pass. This was the actual cause of
        Folder Pairs pegging the CPU. A plain breadth-first walk using the
        same simple indexed parent_id lookup DeltaSyncWorker already relies
        on elsewhere runs in single-digit milliseconds for the same query."""
        with self._lock:
            result: list[Item] = []
            frontier = [root_id]
            while frontier:
                placeholders = ",".join("?" * len(frontier))
                rows = self._conn.execute(
                    f"SELECT * FROM items WHERE drive_id=? AND parent_id IN ({placeholders}) AND deleted=0",
                    (drive_id, *frontier),
                ).fetchall()
                result.extend(Item.from_row(r) for r in rows)
                frontier = [r["id"] for r in rows]
            return result

    # --- folder pairs -------------------------------------------------------

    def create_pair(
        self,
        local_path: str,
        drive_id: str,
        remote_item_id: str,
        remote_path: str,
        exclude_patterns: str = DEFAULT_EXCLUDE_PATTERNS,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO folder_pairs (local_path, drive_id, remote_item_id, remote_path, "
                "created_at, exclude_patterns) VALUES (?, ?, ?, ?, ?, ?)",
                (local_path, drive_id, remote_item_id, remote_path, now_iso(), exclude_patterns),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_pairs(self) -> list[Pair]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM folder_pairs ORDER BY id").fetchall()
            return [Pair.from_row(r) for r in rows]

    def get_pair(self, pair_id: int) -> Pair | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM folder_pairs WHERE id=?", (pair_id,)
            ).fetchone()
            return Pair.from_row(row) if row else None

    def set_pair_enabled(self, pair_id: int, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE folder_pairs SET enabled=? WHERE id=?", (int(enabled), pair_id)
            )
            self._conn.commit()

    def delete_pair(self, pair_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pair_files WHERE pair_id=?", (pair_id,))
            self._conn.execute("DELETE FROM folder_pairs WHERE id=?", (pair_id,))
            self._conn.commit()

    def update_pair_mapping(self, pair_id: int, local_path: str, remote_item_id: str, remote_path: str) -> None:
        """Re-maps an existing pair to a (possibly different) local and/or
        remote folder - requested directly, there was no way to edit an
        existing pair's mapping before this. Clears pair_files and
        last_sync_at so the next sync pass treats it as a fresh bootstrap
        (reconcile_pair's existing same-size-both-sides-trusted heuristic)
        rather than comparing against now-meaningless synced state left
        over from the old mapping."""
        with self._lock:
            self._conn.execute("DELETE FROM pair_files WHERE pair_id=?", (pair_id,))
            self._conn.execute(
                "UPDATE folder_pairs SET local_path=?, remote_item_id=?, remote_path=?, "
                "last_sync_at=NULL WHERE id=?",
                (local_path, remote_item_id, remote_path, pair_id),
            )
            self._conn.commit()

    def update_pair_status(self, pair_id: int, status: str, last_sync_at: str | None = None) -> None:
        with self._lock:
            if last_sync_at is not None:
                self._conn.execute(
                    "UPDATE folder_pairs SET last_sync_status=?, last_sync_at=? WHERE id=?",
                    (status, last_sync_at, pair_id),
                )
            else:
                self._conn.execute(
                    "UPDATE folder_pairs SET last_sync_status=? WHERE id=?", (status, pair_id)
                )
            self._conn.commit()

    def increment_conflict_count(self, pair_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE folder_pairs SET conflict_count = conflict_count + 1 WHERE id=?",
                (pair_id,),
            )
            self._conn.commit()

    # --- pair files -----------------------------------------------------

    def get_pair_file(self, pair_id: int, rel_path: str) -> PairFile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pair_files WHERE pair_id=? AND rel_path=? AND deleted=0",
                (pair_id, rel_path),
            ).fetchone()
            return PairFile.from_row(row) if row else None

    def list_pair_files(self, pair_id: int) -> list[PairFile]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pair_files WHERE pair_id=? AND deleted=0", (pair_id,)
            ).fetchall()
            return [PairFile.from_row(r) for r in rows]

    def upsert_pair_file(
        self,
        pair_id: int,
        rel_path: str,
        *,
        remote_item_id: str | None,
        last_synced_etag: str | None,
        last_synced_mtime: str | None,
        last_synced_size: int | None,
        is_folder: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pair_files (pair_id, rel_path, remote_item_id, last_synced_etag,
                                         last_synced_mtime, last_synced_size, is_folder, deleted,
                                         updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(pair_id, rel_path) DO UPDATE SET
                    remote_item_id=excluded.remote_item_id,
                    last_synced_etag=excluded.last_synced_etag,
                    last_synced_mtime=excluded.last_synced_mtime,
                    last_synced_size=excluded.last_synced_size,
                    is_folder=excluded.is_folder,
                    deleted=0,
                    updated_at=excluded.updated_at
                """,
                (
                    pair_id, rel_path, remote_item_id, last_synced_etag,
                    last_synced_mtime, last_synced_size, int(is_folder), now_iso(),
                ),
            )
            self._conn.commit()

    def mark_pair_file_deleted(self, pair_id: int, rel_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pair_files SET deleted=1, updated_at=? WHERE pair_id=? AND rel_path=?",
                (now_iso(), pair_id, rel_path),
            )
            self._conn.commit()

    def purge_pair_file(self, pair_id: int, rel_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM pair_files WHERE pair_id=? AND rel_path=?", (pair_id, rel_path)
            )
            self._conn.commit()

    def rename_pair_file(self, pair_id: int, old_rel_path: str, new_rel_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pair_files SET rel_path=?, updated_at=? WHERE pair_id=? AND rel_path=?",
                (new_rel_path, now_iso(), pair_id, old_rel_path),
            )
            self._conn.commit()

    # --- activity log -----------------------------------------------------

    def log_activity(
        self, event_type: str, name: str, path: str | None, source: str, is_folder: bool = False
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO activity_log (ts, event_type, name, path, source, is_folder) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso(), event_type, name, path, source, int(is_folder)),
            )
            # Keep the log from growing unbounded - recent activity only needs
            # a rolling window - but never prune conflicts: they're rare and
            # important, while routine events (upload/download, especially
            # from a large bulk sync) can be frequent enough to push a
            # conflict logged minutes earlier out of even a 500-row window
            # well before list_conflicts() would ever have a chance to show
            # it (confirmed happening: a 745-file bulk sync evicted 4 real
            # conflict records within the same sync pass that created them).
            self._conn.execute(
                "DELETE FROM activity_log WHERE event_type != 'conflict' AND id NOT IN "
                "(SELECT id FROM activity_log WHERE event_type != 'conflict' ORDER BY id DESC LIMIT 500)"
            )
            self._conn.commit()

    def list_recent_activity(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, event_type, name, path, source, is_folder FROM activity_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_conflicts(self, source: str, limit: int = 200) -> list[dict]:
        """Individual, not-yet-reviewed conflict records for one source (e.g.
        "pair:3" or "mount") - the count alone (folder_pairs.conflict_count)
        doesn't say which files were actually affected. `name` is the
        original file, `path` is the conflicted-copy rel_path it was
        preserved under (see pair_worker._resolve_conflict /
        mount_sync_worker's equivalent). Excludes rows already dismissed via
        dismiss_conflict() - resolving or acknowledging a conflict in the
        Conflicts dialog removes it from this list without deleting the
        underlying row, so the permanent history/count is unaffected."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, name, path FROM activity_log "
                "WHERE event_type='conflict' AND source=? AND dismissed=0 "
                "ORDER BY id DESC LIMIT ?",
                (source, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_conflicts(self, source: str) -> int:
        """How many conflicts for this source are actually reviewable right
        now (list_conflicts() would return this many rows) - NOT the same as
        folder_pairs.conflict_count, which is a permanent lifetime counter
        that never decreases. GUI badges/menus should use this one: showing
        a lifetime count that promises detail list_conflicts() can no longer
        produce (evicted from the log before conflicts were exempted from
        pruning, or already reviewed) is misleading - confirmed directly by
        the user clicking into a "4 conflicts" badge that had nothing to
        show."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM activity_log WHERE event_type='conflict' "
                "AND source=? AND dismissed=0",
                (source,),
            ).fetchone()
            return row["n"]

    def dismiss_conflict(self, row_id: int) -> None:
        """Marks one activity_log conflict row as reviewed, so it drops out
        of list_conflicts() without losing the permanent audit trail (that
        table's pruning logic never deletes event_type='conflict' rows)."""
        with self._lock:
            self._conn.execute("UPDATE activity_log SET dismissed=1 WHERE id=?", (row_id,))
            self._conn.commit()

    # --- offline mount write queue ------------------------------------------

    def enqueue_mount_op(self, drive_id: str, op_type: str, item_id: str) -> None:
        """Adds a pending_mount_ops row for MountSyncWorker to pick up.
        'write' is deduped against ANY already-pending upload for this item -
        not just an existing 'write', but also a still-pending
        'create_file'/'create_dir' - since whichever one is queued will read
        the item's live on-disk content when it actually runs, so a second
        one would just be redundant. This matters for a file that's edited
        again on the same still-open handle after its *first* write has
        already synced (no pending op left to piggyback on): without
        checking op_type broadly here, flush() would need to track that
        transition itself, and getting it wrong either loses an edit (no op
        ever queued) or spams duplicate ops. 'create_file'/'create_dir'/
        'delete' are never deduped since each represents a distinct,
        meaningful FUSE-level event that already can't be issued twice for
        the same still-pending item (the FUSE layer itself enforces that)."""
        with self._lock:
            if op_type == "write":
                existing = self._conn.execute(
                    "SELECT 1 FROM pending_mount_ops WHERE drive_id=? AND item_id=? "
                    "AND op_type IN ('create_file', 'create_dir', 'write')",
                    (drive_id, item_id),
                ).fetchone()
                if existing:
                    return
            self._conn.execute(
                "INSERT INTO pending_mount_ops (drive_id, op_type, item_id, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (drive_id, op_type, item_id, now_iso()),
            )
            self._conn.commit()

    def enqueue_mount_delete(self, drive_id: str, item_id: str, remote_id: str, etag: str | None) -> None:
        """Delete is the one op type that needs its target snapshotted -
        mark_deleted makes the item row invisible to every getter that
        filters deleted=0, so the worker has nothing else to read it from
        once mark_deleted has already run."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO pending_mount_ops "
                "(drive_id, op_type, item_id, snapshot_remote_id, snapshot_etag, status, created_at) "
                "VALUES (?, 'delete', ?, ?, ?, 'pending', ?)",
                (drive_id, item_id, remote_id, etag, now_iso()),
            )
            self._conn.commit()

    def cancel_pending_op_for_item(self, drive_id: str, item_id: str) -> None:
        """Deletes any not-yet-started op for this item - called before
        enqueueing a delete, so e.g. an offline edit followed by an offline
        delete (before reconnecting) never uploads content the user already
        discarded. Deliberately only cancels 'pending' ops, never
        'in_progress' ones - an op already mid-network-call can't be safely
        un-issued from here (see the in-flight-race note in operations.py's
        unlink/rmdir)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM pending_mount_ops WHERE drive_id=? AND item_id=? AND status='pending'",
                (drive_id, item_id),
            )
            self._conn.commit()

    def has_in_progress_op(self, drive_id: str, item_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM pending_mount_ops WHERE drive_id=? AND item_id=? AND status='in_progress'",
                (drive_id, item_id),
            ).fetchone()
            return row is not None

    def list_pending_mount_ops(self, drive_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, op_type, item_id, snapshot_remote_id, snapshot_etag, status, last_error "
                "FROM pending_mount_ops WHERE drive_id=? AND status != 'in_progress' ORDER BY seq",
                (drive_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_op_in_progress(self, seq: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE pending_mount_ops SET status='in_progress' WHERE seq=?", (seq,))
            self._conn.commit()

    def mark_op_error(self, seq: int, message: str) -> None:
        # Stays 'pending' (not a dead-end 'error' status) so the worker's
        # next pass retries it automatically - only last_error is there for
        # the GUI/logs to show what went wrong.
        with self._lock:
            self._conn.execute(
                "UPDATE pending_mount_ops SET status='pending', last_error=? WHERE seq=?",
                (message, seq),
            )
            self._conn.commit()

    def delete_op(self, seq: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pending_mount_ops WHERE seq=?", (seq,))
            self._conn.commit()

    def delete_item_row(self, drive_id: str, item_id: str) -> None:
        """Purges an item row outright (not a tombstone) - only used for a
        still-unsynced (remote_id IS NULL) locally-created item that gets
        deleted before it ever reached the network, since it never existed
        on OneDrive and there's nothing to reconcile away later."""
        with self._lock:
            self._conn.execute("DELETE FROM items WHERE drive_id=? AND id=?", (drive_id, item_id))
            self._conn.commit()

    def reset_in_progress_mount_ops(self, drive_id: str) -> None:
        """Called once at MountSyncWorker startup - an op left 'in_progress'
        means the process died mid-network-call last run. Whether that call
        actually landed server-side or not is unknown either way (matching
        pair_worker's existing retry-idempotency precedent, see plan notes),
        so it's simply retried."""
        with self._lock:
            self._conn.execute(
                "UPDATE pending_mount_ops SET status='pending' WHERE drive_id=? AND status='in_progress'",
                (drive_id,),
            )
            self._conn.commit()

    def count_mount_op_errors(self, drive_id: str) -> int:
        """Ops that have failed at least once and are still silently
        retrying forever with no GUI visibility - requested directly
        ("Simdilik 3 ve 4 kaldirabiliriz. 1 numara cok onemli" - the "stuck
        sync operations have no visibility" gap was picked as the top
        priority). mark_op_error() leaves status='pending' (so the worker
        keeps retrying automatically) and only records last_error, which
        is exactly what this counts."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM pending_mount_ops WHERE drive_id=? AND last_error IS NOT NULL",
                (drive_id,),
            ).fetchone()
            return row["c"]

    def count_pending_mount_ops(self, drive_id: str) -> int:
        """Everything MountSyncWorker still has left to drain for this
        drive - both 'pending' and 'in_progress' rows count (an op mid
        network-call right now is just as "not done yet" as one still
        waiting its turn). Feeds the tray popup's "N items left" counter,
        the mount-write equivalent of the same figure Folder Pairs already
        surfaces via pair.last_sync_status's "(done/total)" progress."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM pending_mount_ops WHERE drive_id=?", (drive_id,)
            ).fetchone()
            return row["c"]

    def list_mount_op_errors(self, drive_id: str) -> list[dict]:
        """Same rows count_mount_op_errors() counts, resolved to a display
        name/path via get_item_by_id_any (not the normal deleted=0-filtered
        getter) - a 'delete' op's own target is routinely already gone from
        every normal lookup by the time it shows up here."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, op_type, item_id, last_error, created_at FROM pending_mount_ops "
                "WHERE drive_id=? AND last_error IS NOT NULL ORDER BY seq",
                (drive_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = self.get_item_by_id_any(drive_id, row["item_id"])
            result.append({
                "seq": row["seq"],
                "op_type": row["op_type"],
                "name": item.name if item else row["item_id"],
                "path": item.path if item else "",
                "last_error": row["last_error"],
                "created_at": row["created_at"],
            })
        return result

    def dismiss_mount_op(self, seq: int) -> None:
        """Gives up on a stuck op entirely - removes it from the queue
        without ever completing it. For a genuinely permanent failure (e.g.
        a filename OneDrive itself will never accept) where retrying
        forever would just repeat the same error indefinitely, and the
        user would rather stop seeing it than wait for a fix."""
        with self._lock:
            self._conn.execute("DELETE FROM pending_mount_ops WHERE seq=?", (seq,))
            self._conn.commit()
