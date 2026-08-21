import datetime
import errno
import hashlib
import logging
import os
import stat
import time
import uuid
from collections import defaultdict
from pathlib import Path

import trio

from onedrive import constants  # noqa: F401  (import order: resolves FUSE_LIBRARY_PATH first)
import pyfuse3
from pyfuse3 import EntryAttributes, FileInfo, FUSEError, StatvfsData

from onedrive.content_cache import ContentCache, path_for
from onedrive.db import Database
from onedrive.graph_client import GraphClient

# File-manager-internal tools that open a file's content purely to figure
# out what it IS (MIME-type sniffing from magic bytes for files whose
# extension doesn't already say), not because a person opened it. Confirmed
# live via pyfuse3's RequestContext.pid: browsing a folder in Dolphin that
# contains an extensionless/ambiguous-named file spawns kmimetypefinder,
# which opens it - with zero user action - and was showing up as "You
# opened <file>" in the tray popup's activity list. The content still needs
# downloading either way (open() has to return real bytes), only the
# misleading activity-log entry is what's suppressed for these.
_MIME_PROBE_PROCESS_NAMES = frozenset({"kmimetypefinder", "kmimetypefinder5", "kmimetypefinder6"})


def _log_exception_safely(message: str, *args) -> None:
    """logger.exception() wrapped so a failure IN the logging call itself
    can never take down whatever's calling this - confirmed live as a real
    crash, not a hypothetical: a plain `logger.exception(...)` inside an
    `except Exception:` block here raised its own `NameError: name
    'logger' is not defined` (root cause not fully pinned down - the
    surrounding frame is a Cython/trio boundary inside pyfuse3's own
    coroutine-resumption machinery, where module-global lookups apparently
    aren't always reliable), and that NEW exception propagated straight
    through pyfuse3's session loop and killed the entire FUSE mount ("Transport
    endpoint is not connected" until the process was restarted) - exactly
    the failure mode this file's other exception handlers exist to prevent
    in the first place. Whatever the exact cause, the fix is the same
    regardless: logging a diagnostic must never be able to escalate into a
    worse failure than the one it's reporting."""
    try:
        logger.exception(message, *args)
    except Exception:
        pass


def _is_mime_probe(pid: int) -> bool:
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return False
    return comm in _MIME_PROBE_PROCESS_NAMES


def _parse_iso_ns(value: str | None) -> int:
    if not value:
        return 0
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1_000_000_000)
    except ValueError:
        return 0


def _decode_name(name) -> str:
    return name.decode("utf-8") if isinstance(name, bytes) else name


class OneDriveOperations(pyfuse3.Operations):
    """On-demand, read-write FUSE view over the locally cached OneDrive
    metadata. lookup/getattr/readdir are served only from SQLite - zero
    network calls for browsing, which is what keeps directory listings
    instant. Content downloads happen only inside open() (offloaded off the
    trio event loop via trio.to_thread.run_sync, so a slow transfer never
    blocks other concurrent FUSE requests).

    Every write-family operation (create/mkdir/write/unlink/rmdir/rename) is
    local-first and offline-tolerant: it updates the DB/local filesystem
    immediately and queues a pending_mount_ops row for MountSyncWorker to
    execute in the background, instead of making a synchronous Graph call
    on the FUSE thread. A brand-new file/folder gets a real, permanent DB
    row under a synthetic "pending:<uuid>" id the moment it's created -
    there is no separate virtual-inode bookkeeping to reconcile later, since
    that id never changes even once the real Graph id is known (see
    items.remote_id)."""

    def __init__(
        self,
        db: Database,
        content_cache: ContentCache,
        drive_id: str,
        root_item_id: str,
        graph_client: GraphClient,
        mount_sync_worker_ref=None,
    ):
        super().__init__()
        self.db = db
        self.cache = content_cache
        self.drive_id = drive_id
        self.graph = graph_client
        # callable returning the MountSyncWorker instance (or None before
        # it's started) - loose/duck-typed, mirroring pairs_panel.py's
        # pair_worker_ref pattern, so a queued op can wake the worker
        # immediately instead of waiting for its poll interval
        self._mount_sync_worker_ref = mount_sync_worker_ref

        self._inode_to_id: dict[int, str] = {pyfuse3.ROOT_INODE: root_item_id}
        self._id_to_inode: dict[str, int] = {root_item_id: pyfuse3.ROOT_INODE}
        self._next_inode = pyfuse3.ROOT_INODE + 1
        self._lookup_cnt: dict[int, int] = defaultdict(int)
        self._lookup_cnt[pyfuse3.ROOT_INODE] = 1

        self._fh_map: dict[int, object] = {}
        self._next_fh = 1

        # open-file bookkeeping only (which local staging path/fh backs
        # which inode) - the durable state itself (the item's existence,
        # its content) lives in the DB/content cache from the moment
        # create()/write() happens, not only once flush() runs, so a crash
        # here loses at most whatever's sitting in an OS write buffer that
        # was never flush()'d, never the file or edit itself
        self._pending_writes: dict[int, dict] = {}  # fh -> record, see _new_pending()
        self._inode_pending_fh: dict[int, int] = {}  # inode -> the fh currently tracking it

    def _wake_sync_worker(self) -> None:
        if self._mount_sync_worker_ref is not None:
            worker = self._mount_sync_worker_ref()
            if worker is not None:
                worker.wake()

    # --- inode bookkeeping -------------------------------------------------

    def _get_or_assign_inode(self, item_id: str) -> int:
        inode = self._id_to_inode.get(item_id)
        if inode is not None:
            return inode
        inode = self._next_inode
        self._next_inode += 1
        self._id_to_inode[item_id] = inode
        self._inode_to_id[inode] = item_id
        return inode

    def _item_for_inode(self, inode: int):
        item_id = self._inode_to_id.get(inode)
        if item_id is None:
            raise FUSEError(errno.ENOENT)
        item = self.db.get_item_by_id(self.drive_id, item_id)
        if item is None:
            raise FUSEError(errno.ENOENT)
        return item

    def _resolve_child_by_name(self, parent_item, name_str: str):
        for child in self.db.list_children(self.drive_id, parent_item.id):
            if child.name == name_str:
                return child
        return None

    async def forget(self, inode_list) -> None:
        for inode, nlookup in inode_list:
            if inode == pyfuse3.ROOT_INODE:
                continue
            self._lookup_cnt[inode] -= nlookup
            if self._lookup_cnt[inode] <= 0:
                item_id = self._inode_to_id.pop(inode, None)
                if item_id is not None:
                    self._id_to_inode.pop(item_id, None)
                self._lookup_cnt.pop(inode, None)

    # --- attributes: DB-only for real items, live-stat for in-progress writes ---

    def _attrs_for(self, item, inode: int, entry_timeout: int = 60, attr_timeout: int = 60) -> EntryAttributes:
        entry = EntryAttributes()
        entry.st_ino = inode
        entry.generation = 0
        entry.entry_timeout = entry_timeout
        entry.attr_timeout = attr_timeout
        entry.st_mode = (stat.S_IFDIR | 0o755) if item.is_folder else (stat.S_IFREG | 0o644)
        entry.st_nlink = 2 if item.is_folder else 1
        entry.st_uid = os.getuid()
        entry.st_gid = os.getgid()
        entry.st_size = item.size
        entry.st_blksize = 512
        entry.st_blocks = (item.size + 511) // 512
        mtime_ns = _parse_iso_ns(item.mtime) or int(time.time() * 1_000_000_000)
        entry.st_atime_ns = mtime_ns
        entry.st_mtime_ns = mtime_ns
        entry.st_ctime_ns = mtime_ns
        return entry

    def _attrs_for_pending(self, pw: dict, inode: int) -> EntryAttributes:
        """Reflects the live local staging file, not the (possibly stale)
        DB row - needed because fstat() immediately after write()/create()
        is extremely common and must see the in-progress size, not the old
        remote one."""
        entry = EntryAttributes()
        entry.st_ino = inode
        entry.generation = 0
        entry.entry_timeout = 0
        entry.attr_timeout = 0
        entry.st_mode = stat.S_IFREG | 0o644
        entry.st_nlink = 1
        entry.st_uid = os.getuid()
        entry.st_gid = os.getgid()
        st = os.fstat(pw["file"].fileno())
        entry.st_size = st.st_size
        entry.st_blksize = 512
        entry.st_blocks = (st.st_size + 511) // 512
        mtime_ns = int(st.st_mtime * 1_000_000_000)
        entry.st_atime_ns = mtime_ns
        entry.st_mtime_ns = mtime_ns
        entry.st_ctime_ns = mtime_ns
        return entry

    async def lookup(self, parent_inode, name, ctx=None) -> EntryAttributes:
        name_str = _decode_name(name)
        parent_item = self._item_for_inode(parent_inode)
        child = self._resolve_child_by_name(parent_item, name_str)
        if child is None:
            raise FUSEError(errno.ENOENT)
        inode = self._get_or_assign_inode(child.id)
        self._lookup_cnt[inode] += 1
        return self._attrs_for(child, inode)

    async def getattr(self, inode, ctx=None) -> EntryAttributes:
        fh = self._inode_pending_fh.get(inode)
        if fh is not None:
            return self._attrs_for_pending(self._pending_writes[fh], inode)
        item = self._item_for_inode(inode)
        return self._attrs_for(item, inode)

    async def opendir(self, inode, ctx) -> int:
        self._item_for_inode(inode)  # validates existence / raises ENOENT
        return inode

    async def readdir(self, fh, start_id, token) -> None:
        item = self._item_for_inode(fh)
        # sorted by a stable key (id), not by inode - inode assignment order
        # isn't stable across paginated readdir() calls on large directories
        children = sorted(self.db.list_children(self.drive_id, item.id), key=lambda c: c.id)
        for i, child in enumerate(children):
            if i < start_id:
                continue
            inode = self._get_or_assign_inode(child.id)
            attrs = self._attrs_for(child, inode)
            if not pyfuse3.readdir_reply(token, child.name.encode("utf-8"), attrs, i + 1):
                break
            self._lookup_cnt[inode] += 1

    async def releasedir(self, fh) -> None:
        pass

    async def statfs(self, ctx) -> StatvfsData:
        result = StatvfsData()
        total = int(self.db.get_sync_state("drive_quota_total") or 0)
        remaining = int(self.db.get_sync_state("drive_quota_remaining") or 0)
        bsize = 4096
        result.f_bsize = bsize
        result.f_frsize = bsize
        result.f_blocks = total // bsize
        result.f_bfree = remaining // bsize
        result.f_bavail = remaining // bsize
        result.f_files = self.db.item_count(self.drive_id)
        result.f_ffree = 1_000_000
        result.f_favail = 1_000_000
        return result

    # --- content: open()/create() are the only places a download can happen ---

    def _new_pending(
        self, *, local_path, file, item_id, parent_id, name, etag, dirty, inode, is_new, original_hash
    ) -> dict:
        return {
            "local_path": local_path, "file": file, "item_id": item_id, "parent_id": parent_id,
            "name": name, "etag": etag, "dirty": dirty, "inode": inode, "is_new": is_new,
            "original_hash": original_hash,
        }

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    async def _ensure_cached(self, item, log_open_activity: bool = True) -> Path:
        """Wraps ContentCache.ensure_cached() for every FUSE handler that
        needs it. This translation is not optional: an exception left
        uncaught here doesn't just fail the one open()/create() call, it
        propagates straight through pyfuse3's session loop and kills the
        ENTIRE FUSE session for every file, not just this one - confirmed
        via a real crash (opening a single not-yet-cached file while
        offline took the whole mount down, kernel left the mountpoint
        reporting "Transport endpoint is not connected" until the process
        was restarted, even though every other file's metadata was sitting
        right there in the DB and should have kept working)."""
        try:
            return await trio.to_thread.run_sync(self.cache.ensure_cached, item, log_open_activity)
        except FUSEError:
            raise
        except Exception:
            _log_exception_safely("failed to download content for %s", item.path)
            raise FUSEError(errno.EIO) from None

    async def open(self, inode, flags, ctx) -> FileInfo:
        item = self._item_for_inode(inode)
        if item.is_folder:
            raise FUSEError(errno.EISDIR)

        existing_pw_fh = self._inode_pending_fh.get(inode)

        if flags & (os.O_WRONLY | os.O_RDWR):
            if existing_pw_fh is not None:
                # already open for writing elsewhere - share the same local
                # staging file rather than re-downloading (which would
                # otherwise clobber the in-progress edit, see note in
                # ensure_cached's freshness check), and reuse its baseline
                # hash rather than re-hashing the (possibly already-dirty)
                # shared file, which would wrongly adopt an in-progress
                # edit as the "unchanged" baseline
                local_path = self._pending_writes[existing_pw_fh]["local_path"]
                original_hash = self._pending_writes[existing_pw_fh]["original_hash"]
            else:
                local_path = await self._ensure_cached(item)
                # Many apps (LibreOffice included - it's what left the
                # .~lock.* file behind that led to this) open O_RDWR just to
                # probe writability or hold a lock, with no intent to
                # change content. Hashing the just-downloaded bytes now
                # gives flush() a baseline to diff against, so a "changed"
                # upload/activity-log entry only fires for a genuine edit,
                # not merely opening the file.
                original_hash = await trio.to_thread.run_sync(self._hash_file, local_path)
            f = open(local_path, "r+b")
            if flags & os.O_TRUNC:
                f.truncate(0)
            fh = self._next_fh
            self._next_fh += 1
            self._fh_map[fh] = f
            self._pending_writes[fh] = self._new_pending(
                local_path=local_path, file=f, item_id=item.id, parent_id=item.parent_id,
                name=item.name, etag=item.etag, dirty=bool(flags & os.O_TRUNC),
                inode=inode, is_new=False, original_hash=original_hash,
            )
            self._inode_pending_fh[inode] = fh
            return FileInfo(fh=fh)

        if existing_pw_fh is not None:
            local_path = self._pending_writes[existing_pw_fh]["local_path"]
        else:
            local_path = await self._ensure_cached(item, log_open_activity=not _is_mime_probe(ctx.pid))
        f = open(local_path, "rb")
        fh = self._next_fh
        self._next_fh += 1
        self._fh_map[fh] = f
        return FileInfo(fh=fh)

    async def create(self, parent_inode, name, mode, flags, ctx):
        name_str = _decode_name(name)
        parent_item = self._item_for_inode(parent_inode)
        existing = self._resolve_child_by_name(parent_item, name_str)

        if existing is not None:
            if flags & os.O_EXCL:
                raise FUSEError(errno.EEXIST)
            inode = self._get_or_assign_inode(existing.id)
            self._lookup_cnt[inode] += 1
            existing_pw_fh = self._inode_pending_fh.get(inode)
            local_path = await self._ensure_cached(existing)
            if existing_pw_fh is not None:
                original_hash = self._pending_writes[existing_pw_fh]["original_hash"]
            else:
                original_hash = await trio.to_thread.run_sync(self._hash_file, local_path)
            f = open(local_path, "r+b")
            if flags & os.O_TRUNC:
                f.truncate(0)
            fh = self._next_fh
            self._next_fh += 1
            self._fh_map[fh] = f
            self._pending_writes[fh] = self._new_pending(
                local_path=local_path, file=f, item_id=existing.id, parent_id=existing.parent_id,
                name=existing.name, etag=existing.etag, dirty=bool(flags & os.O_TRUNC),
                inode=inode, is_new=False, original_hash=original_hash,
            )
            self._inode_pending_fh[inode] = fh
            return FileInfo(fh=fh), self._attrs_for(existing, inode)

        # genuinely new file: real, permanent DB row under a synthetic id
        # right away (offline-tolerant - shows up in readdir/getattr for
        # free, and the queued op survives a crash since it's persisted,
        # not just sitting in this process's memory)
        pending_id = f"pending:{uuid.uuid4()}"
        self.db.insert_pending_item(self.drive_id, pending_id, parent_item.id, name_str, is_folder=False)
        inode = self._get_or_assign_inode(pending_id)
        self._lookup_cnt[inode] += 1  # create() increases the lookup count, same as lookup()/mkdir()

        local_path = path_for(self.drive_id, pending_id)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(local_path, "w+b")
        fh = self._next_fh
        self._next_fh += 1
        self._fh_map[fh] = f
        self._pending_writes[fh] = self._new_pending(
            local_path=local_path, file=f, item_id=pending_id, parent_id=parent_item.id,
            name=name_str, etag=None, dirty=True, inode=inode, is_new=True,
            original_hash=hashlib.sha256(b"").hexdigest(),
        )
        self._inode_pending_fh[inode] = fh

        self.db.enqueue_mount_op(self.drive_id, "create_file", pending_id)
        self._wake_sync_worker()

        item = self.db.get_item_by_id(self.drive_id, pending_id)
        return FileInfo(fh=fh), self._attrs_for(item, inode, entry_timeout=0, attr_timeout=0)

    async def read(self, fh, off, size) -> bytes:
        pw = self._pending_writes.get(fh)
        f = pw["file"] if pw is not None else self._fh_map.get(fh)
        if f is None:
            raise FUSEError(errno.EBADF)
        f.seek(off)
        return f.read(size)

    async def write(self, fh, off, buf) -> int:
        pw = self._pending_writes.get(fh)
        if pw is None:
            raise FUSEError(errno.EBADF)
        f = pw["file"]
        f.seek(off)
        n = f.write(buf)
        f.flush()  # local OS buffer only, so a concurrent fstat sees the new size immediately
        # A 0-byte write is a common no-op probe (some apps issue one just
        # to confirm the fd is writable) - skip even setting dirty for it so
        # a pure probe-write session never triggers flush()'s hash check
        # below. Any real write (n > 0) still goes through that hash check
        # too, since identical bytes rewritten in place are also a no-op.
        if n:
            pw["dirty"] = True
        return n

    async def flush(self, fh) -> None:
        pw = self._pending_writes.get(fh)
        if pw is None or not pw["dirty"]:
            return
        current_hash = await trio.to_thread.run_sync(self._hash_file, pw["local_path"])
        if current_hash == pw["original_hash"]:
            # Content ended up byte-identical to what it was before this fd
            # was opened for writing - e.g. LibreOffice (and others) open
            # O_RDWR just to probe writability/hold a lock, with no actual
            # edit. Treating this as a real change would upload it and log
            # a false "You changed X" activity entry for content the user
            # never touched.
            pw["dirty"] = False
            return
        st = os.fstat(pw["file"].fileno())
        mtime_iso = datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc).isoformat()
        self.db.update_local_content(self.drive_id, pw["item_id"], st.st_size, mtime_iso)
        # enqueue_mount_op dedupes against any already-pending
        # create_file/create_dir/write for this item, so this is safe to
        # call unconditionally on every dirty flush - covers both "still
        # mid-initial-sync, the pending create will pick up these bytes"
        # and "already synced once, this edit needs its own fresh op"
        # without flush() itself needing to know which case it's in
        self.db.enqueue_mount_op(self.drive_id, "write", pw["item_id"])
        self._wake_sync_worker()
        pw["dirty"] = False
        # rebase the baseline so a later flush on this same still-open fh
        # only reports a change relative to what was just queued, not the
        # original pre-edit content
        pw["original_hash"] = current_hash

    async def release(self, fh) -> None:
        pw = self._pending_writes.pop(fh, None)
        if pw is not None:
            if self._inode_pending_fh.get(pw["inode"]) == fh:
                self._inode_pending_fh.pop(pw["inode"], None)
            if pw["dirty"]:
                # flush() should already have run before release() on a
                # normal close(2) - this is only a last-resort safety net
                # for the rare case release() fires without one
                try:
                    current_hash = await trio.to_thread.run_sync(self._hash_file, pw["local_path"])
                    if current_hash != pw["original_hash"]:
                        st = os.fstat(pw["file"].fileno())
                        mtime_iso = datetime.datetime.fromtimestamp(
                            st.st_mtime, tz=datetime.timezone.utc
                        ).isoformat()
                        self.db.update_local_content(self.drive_id, pw["item_id"], st.st_size, mtime_iso)
                        self.db.enqueue_mount_op(self.drive_id, "write", pw["item_id"])
                        self._wake_sync_worker()
                except Exception:
                    _log_exception_safely("final local-content update failed for %s", pw["name"])
            pw["file"].close()
            # The staged file is now the durable, authoritative local
            # content for a (possibly still-unsynced) item - it must
            # survive release(), unlike the old design where it was
            # scratch space cleaned up here.
            return
        f = self._fh_map.pop(fh, None)
        if f is not None:
            f.close()

    # --- metadata writes ---------------------------------------------------

    async def setattr(self, inode, attr, fields, fh, ctx) -> EntryAttributes:
        if fields.update_size:
            target_size = attr.st_size
            pw = self._pending_writes.get(fh) if fh is not None else None
            if pw is None:
                existing_fh = self._inode_pending_fh.get(inode)
                if existing_fh is not None:
                    pw = self._pending_writes.get(existing_fh)
            if pw is not None:
                pw["file"].truncate(target_size)
                pw["dirty"] = True
            else:
                item = self._item_for_inode(inode)
                if item.is_folder:
                    raise FUSEError(errno.EISDIR)
                try:
                    await trio.to_thread.run_sync(self._truncate_and_upload, item, target_size)
                except Exception:
                    _log_exception_safely("truncate/upload failed for %s", item.path)
                    raise FUSEError(errno.EIO) from None
        # other fields (atime/mtime/mode/uid/gid): no remote equivalent worth
        # tracking - just echo back current attrs rather than raising, since
        # apps commonly touch these reflexively around a save
        return await self.getattr(inode, ctx)

    def _truncate_and_upload(self, item, target_size: int) -> None:
        local_path = self.cache.ensure_cached(item)
        with open(local_path, "r+b") as f:
            f.truncate(target_size)
        result = self.graph.upload_file(
            self.drive_id, item.parent_id, item.name, local_path,
            existing_item_id=item.id, if_match=item.etag,
        )
        self.db.upsert_item(self.drive_id, result)

    # --- directory / namespace writes ---------------------------------------

    async def mkdir(self, parent_inode, name, mode, ctx) -> EntryAttributes:
        name_str = _decode_name(name)
        parent_item = self._item_for_inode(parent_inode)
        if self._resolve_child_by_name(parent_item, name_str) is not None:
            raise FUSEError(errno.EEXIST)
        pending_id = f"pending:{uuid.uuid4()}"
        self.db.insert_pending_item(self.drive_id, pending_id, parent_item.id, name_str, is_folder=True)
        inode = self._get_or_assign_inode(pending_id)
        self._lookup_cnt[inode] += 1
        self.db.enqueue_mount_op(self.drive_id, "create_dir", pending_id)
        self._wake_sync_worker()
        item = self.db.get_item_by_id(self.drive_id, pending_id)
        return self._attrs_for(item, inode, entry_timeout=0, attr_timeout=0)

    def _delete_item_offline_safe(self, item) -> None:
        """Shared by unlink()/rmdir(). Cancels any not-yet-started pending
        op for this item first - e.g. a queued 'write' for an offline edit
        must never end up uploading content the user already deleted. If
        the item was never synced (remote_id is still NULL) AND nothing is
        currently mid-network-call for it, it never existed on OneDrive at
        all: purge it locally with zero network involvement, including its
        staged content file. The in-progress check matters because a
        create can be mid-flight right now with remote_id still NULL right
        up until it succeeds - taking the fast local-purge path in that
        window and then letting the in-flight create's eventual success
        upsert the row back in (deleted=0) would resurrect a file the user
        just deleted. Otherwise, fall through to a real queued delete,
        snapshotting the remote identity needed since mark_deleted makes
        the item invisible to every normal getter from this point on."""
        self.db.cancel_pending_op_for_item(self.drive_id, item.id)
        if item.remote_id is None and not self.db.has_in_progress_op(self.drive_id, item.id):
            self.db.delete_item_row(self.drive_id, item.id)
            try:
                path_for(self.drive_id, item.id).unlink(missing_ok=True)
            except OSError:
                pass
            return
        self.db.mark_deleted(self.drive_id, item.id)
        self.db.enqueue_mount_delete(self.drive_id, item.id, item.remote_id, item.etag)
        self._wake_sync_worker()

    async def unlink(self, parent_inode, name, ctx) -> None:
        name_str = _decode_name(name)
        parent_item = self._item_for_inode(parent_inode)
        child = self._resolve_child_by_name(parent_item, name_str)
        if child is None:
            raise FUSEError(errno.ENOENT)
        if child.is_folder:
            raise FUSEError(errno.EISDIR)
        self._delete_item_offline_safe(child)
        self.db.log_activity("deleted", child.name, child.path, "mount")

    async def rmdir(self, parent_inode, name, ctx) -> None:
        name_str = _decode_name(name)
        parent_item = self._item_for_inode(parent_inode)
        child = self._resolve_child_by_name(parent_item, name_str)
        if child is None:
            raise FUSEError(errno.ENOENT)
        if not child.is_folder:
            raise FUSEError(errno.ENOTDIR)
        if self.db.list_children(self.drive_id, child.id):
            raise FUSEError(errno.ENOTEMPTY)
        self._delete_item_offline_safe(child)
        self.db.log_activity("deleted", child.name, child.path, "mount", is_folder=True)

    async def rename(self, parent_inode_old, name_old, parent_inode_new, name_new, flags, ctx) -> None:
        if flags & pyfuse3.RENAME_EXCHANGE:
            raise FUSEError(errno.EINVAL)
        name_old_str = _decode_name(name_old)
        name_new_str = _decode_name(name_new)
        old_parent = self._item_for_inode(parent_inode_old)
        new_parent = self._item_for_inode(parent_inode_new)
        child = self._resolve_child_by_name(old_parent, name_old_str)
        if child is None:
            raise FUSEError(errno.ENOENT)
        target = self._resolve_child_by_name(new_parent, name_new_str)
        if target is not None and flags & pyfuse3.RENAME_NOREPLACE:
            raise FUSEError(errno.EEXIST)

        self.db.rename_item_local(self.drive_id, child.id, new_parent.id, name_new_str)
        if child.remote_id is not None:
            # a still-unsynced item's create/write op reads the item's live
            # DB state (including its current name/parent) at execution
            # time, so a rename issued before it's even synced needs no
            # extra op queued at all - it's picked up automatically
            self.db.enqueue_mount_op(self.drive_id, "rename", child.id)
            self._wake_sync_worker()
