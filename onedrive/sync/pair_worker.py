import datetime
import fnmatch
import logging
import os
import queue
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from onedrive import constants
from onedrive.db import DEFAULT_EXCLUDE_PATTERNS, Database, now_iso
from onedrive.graph_client import GraphAuthError, GraphClient, GraphConflictError
from onedrive.quickxorhash import quickxorhash_base64
from onedrive.sync.local_watcher import LocalWatcher
from onedrive.sync.reconcile import (
    Action,
    ActionType,
    LocalEntry,
    RemoteEntry,
    SyncedEntry,
    reconcile_pair,
)

logger = logging.getLogger(__name__)


def _mtime_iso(stat_result) -> str:
    return datetime.datetime.fromtimestamp(
        stat_result.st_mtime, tz=datetime.timezone.utc
    ).isoformat()


def _parse_patterns(patterns_text: str) -> list[str]:
    return [p.strip() for p in patterns_text.splitlines() if p.strip()]


def _is_excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


class PairSyncWorker(threading.Thread):
    """Executes reconcile.py's classification for every enabled Folder Pair:
    builds the three input maps (local via os.walk, remote via the existing
    items cache - zero network, synced via pair_files), turns the resulting
    actions into real GraphClient/filesystem/DB calls, and resolves conflicts
    with the crash-safe keep-both rename dance. Runs a full pass on startup
    and every PAIR_SYNC_INTERVAL_SECONDS regardless, plus a debounced pass
    whenever the local watcher sees activity for a pair - both paths share
    this exact same reconciliation code, so there's no separate fast/slow
    path to disagree with each other."""

    def __init__(
        self,
        db: Database,
        graph_client: GraphClient,
        drive_id: str,
        interval: int = constants.PAIR_SYNC_INTERVAL_SECONDS,
        on_status: Callable[[int, str], None] | None = None,
        on_auth_required: Callable[[], None] | None = None,
        on_conflict: Callable[[str, str], None] | None = None,
    ):
        super().__init__(daemon=True, name="PairSyncWorker")
        self.db = db
        self.graph = graph_client
        self.drive_id = drive_id
        self.interval = interval
        self.on_status = on_status or (lambda _pid, _msg: None)
        self.on_auth_required = on_auth_required or (lambda: None)
        self.on_conflict = on_conflict or (lambda _name, _path: None)

        self._last_progress_update: dict[int, float] = {}

        self._stop = threading.Event()
        self._event_queue: "queue.Queue[tuple[int, float]]" = queue.Queue()
        self._watcher = LocalWatcher(self._event_queue)
        self._pending_deadline: dict[int, float] = {}
        self._force_full_sweep = threading.Event()
        self._force_full_sweep.set()  # always do a full pass on startup

    def run(self) -> None:
        self.refresh_pairs()
        last_full_sweep = 0.0
        while not self._stop.is_set():
            try:
                pair_id, _ts = self._event_queue.get(timeout=0.5)
                self._pending_deadline[pair_id] = time.monotonic() + constants.LOCAL_WATCH_DEBOUNCE_SECONDS
            except queue.Empty:
                pass

            now = time.monotonic()
            due = [pid for pid, deadline in self._pending_deadline.items() if deadline <= now]
            for pid in due:
                del self._pending_deadline[pid]
                self._sync_one_pair_safely(pid)

            if self._force_full_sweep.is_set() or (now - last_full_sweep) >= self.interval:
                self._force_full_sweep.clear()
                for pair in self.db.list_pairs():
                    if pair.enabled:
                        self._sync_one_pair_safely(pair.id)
                last_full_sweep = now

    def stop(self) -> None:
        self._stop.set()
        self._watcher.stop()

    def wake(self, pair_id: int | None = None) -> None:
        if pair_id is None:
            self._force_full_sweep.set()
        else:
            self._pending_deadline[pair_id] = 0.0

    def refresh_pairs(self) -> None:
        """Sync the set of watched local paths with what's currently in the
        DB - call after adding/removing/enabling/disabling a pair."""
        pairs = self.db.list_pairs()
        watched_ids = set(self._watcher._watches.keys())
        enabled_ids = {p.id for p in pairs if p.enabled}
        for pair in pairs:
            if pair.enabled and pair.id not in watched_ids:
                self._watcher.watch(pair.id, Path(pair.local_path))
        for stale_id in watched_ids - enabled_ids:
            self._watcher.unwatch(stale_id)

    # --- one pair, one pass -----------------------------------------------

    def _set_status(self, pair_id: int, message: str) -> None:
        """Persist to the DB (so pairs_panel's status subtitle - re-read from
        db.list_pairs() on every refresh - shows live progress, not just a
        static "syncing" placeholder) and emit the signal (drives the GUI
        refresh itself)."""
        self.db.update_pair_status(pair_id, message)
        self.on_status(pair_id, message)

    def _set_progress_status(self, pair_id: int, message: str) -> None:
        """Same as _set_status, but rate-limited to at most a few times a
        second per pair - meant for the per-file "Uploading/Downloading X"
        message inside a large batch, where the underlying action itself can
        take well under a second (e.g. a small file, or a 404-fast-path
        delete). Calling _set_status for every single one of those means a
        DB write plus a cross-thread Qt signal per file - harmless for a
        handful of files, but a real burden on both the DB (lock contention
        with the GUI thread's own reads) and the GUI thread (a flood of
        signal deliveries to process) once a batch reaches into the
        thousands, exactly the kind of large batch this app can legitimately
        have (a big pinned folder, a first-time pair bootstrap). The status
        line only needs to be fresh enough for a human to read, not updated
        every single file - not calling this for most files in a fast batch
        doesn't lose anything the user could actually perceive."""
        now = time.monotonic()
        if now - self._last_progress_update.get(pair_id, 0.0) < constants.PAIR_PROGRESS_UPDATE_INTERVAL_SECONDS:
            return
        self._last_progress_update[pair_id] = now
        self._set_status(pair_id, message)

    def _sync_one_pair_safely(self, pair_id: int) -> None:
        try:
            self._sync_one_pair(pair_id)
        except GraphAuthError:
            logger.warning("pair %s sync needs re-authentication", pair_id)
            self._set_status(pair_id, "Sign-in required")
            self.on_auth_required()
        except Exception as e:
            logger.exception("pair %s sync failed", pair_id)
            # lowercase "error" prefix is load-bearing - pairs_panel's
            # _status_icon() matches on it (case-sensitively) to show red
            self._set_status(pair_id, f"error: {e}")

    def _sync_one_pair(self, pair_id: int) -> None:
        pair = self.db.get_pair(pair_id)
        if pair is None or not pair.enabled:
            return

        self._set_status(pair.id, "Checking for changes...")

        root_item = self.db.get_item_by_id(pair.drive_id, pair.remote_item_id)
        if root_item is None:
            raise RuntimeError("remote pair folder no longer exists")
        root_path = root_item.path

        # One global list (Settings tab), not a per-pair one - requested
        # directly. pair.exclude_patterns (the old per-pair column) is no
        # longer read here; left in the schema unused rather than migrated
        # away, since nothing else depends on it.
        global_patterns = self.db.get_sync_state("global_exclude_patterns") or DEFAULT_EXCLUDE_PATTERNS
        patterns = _parse_patterns(global_patterns)
        local_map = self._build_local_map(Path(pair.local_path), patterns)
        remote_map = self._build_remote_map(pair.drive_id, pair.remote_item_id, root_path, patterns)
        synced_map = self._build_synced_map(pair.id, patterns)

        is_bootstrap = pair.last_sync_at is None
        if is_bootstrap:
            self._attach_bootstrap_hashes(Path(pair.local_path), local_map, remote_map, synced_map)
        actions = reconcile_pair(local_map, remote_map, synced_map, is_bootstrap=is_bootstrap)

        total = len(actions)
        if total:
            self._set_status(pair.id, f"Syncing {total} item{'s' if total != 1 else ''}...")

        created_remote_ids: dict[str, str] = {}
        conflicts = 0
        stopped_early = False
        unresolved_new_item = False
        for idx, action in enumerate(actions, start=1):
            # Without this, pausing (manually, or via the metered/battery
            # auto-pause) didn't actually stop an in-progress pair sync at
            # all until it had processed every single one of its actions -
            # .stop() only sets a flag that the outer run() loop checks
            # between pairs, never between individual actions within one
            # pair's own batch. Reported directly, live, against a real
            # backlog of thousands of items ("sync paused diyor ama sync
            # ediyor" - it says paused but it's still syncing). Stopping
            # here mid-batch is safe - reconcile_pair() re-derives the
            # remaining actions fresh from current state on the next pass,
            # so nothing is lost, just deferred.
            if self._stop.is_set():
                logger.info(
                    "pair %s: stopping mid-batch (%d/%d done) - pause or shutdown requested",
                    pair.id, idx - 1, total,
                )
                stopped_early = True
                break
            if idx % 20 == 0:
                # A deliberate scheduling point, not a real delay - CPython's
                # GIL is released on bytecode-count/time slices regardless,
                # but that doesn't guarantee the GUI thread specifically gets
                # picked next rather than another worker thread. A tight loop
                # over thousands of actions (each doing real work: a Graph
                # call, a DB write, a log line) reproducibly left the GUI
                # feeling unresponsive during a large batch even though it
                # was never actually blocked - sleep(0) hands control to the
                # scheduler explicitly instead of hoping it gets there soon
                # enough on its own. Every 20th action, not every one -
                # frequent enough to matter, rare enough that the syscall
                # overhead is nothing next to the actual work being done.
                time.sleep(0)
            try:
                if action.type == ActionType.CONFLICT:
                    self._resolve_conflict(pair, action, local_map, remote_map)
                    conflicts += 1
                else:
                    self._execute_action(pair, action, local_map, remote_map, created_remote_ids, progress=(idx, total))
            except GraphConflictError:
                logger.info(
                    "pair %s: %s raced with a remote change, refreshing and will re-check next pass",
                    pair.id, action.rel_path,
                )
                # Without this, a stale cached etag reproduces the exact same
                # 409/412 forever - every future pass would keep building
                # remote_map from the same outdated etag and retry the same
                # doomed request. Refreshing now means the next pass sees
                # the item's real current state and reclassifies correctly
                # (could resolve to unchanged, a real conflict, or just
                # succeed outright with the fresh etag).
                if action.remote_item_id:
                    try:
                        current = self.graph.get_item(pair.drive_id, action.remote_item_id)
                        self.db.upsert_item(pair.drive_id, current)
                    except Exception:
                        logger.debug(
                            "pair %s: couldn't refresh %s after conflict (may have been deleted)",
                            pair.id, action.rel_path, exc_info=True,
                        )
                else:
                    # No remote_item_id means this was a fresh create this
                    # pass's remote_map (built from our own delta-sync cache,
                    # not a live call) didn't know about yet - graph_client's
                    # own retry-as-replace already tried and failed to find
                    # it via a live lookup too. If this is still the
                    # bootstrap pass, don't let it stamp last_sync_at below:
                    # doing so would permanently flip is_bootstrap False for
                    # this pair, and once delta sync does catch up, this
                    # exact same file - even if byte-identical on both sides
                    # - would hit reconcile_pair() as a same-size match with
                    # no is_bootstrap left to trust it, so it'd be flagged a
                    # real CONFLICT instead of silently recognized as already
                    # synced. Deferring instead lets the next pass retry
                    # bootstrap-eligible, once the cache has caught up.
                    unresolved_new_item = True
            except OSError:
                logger.exception("pair %s: local filesystem op failed for %s", pair.id, action.rel_path)

        if conflicts:
            for _ in range(conflicts):
                self.db.increment_conflict_count(pair.id)

        if stopped_early or unresolved_new_item:
            # Don't mark this pass "idle" or stamp last_sync_at - it's
            # genuinely incomplete, and last_sync_at also drives the next
            # pass's is_bootstrap check, which assumes a truthful "did a
            # first full pass ever actually finish" signal. The next pass
            # re-derives the remaining actions fresh via reconcile_pair()
            # and only marks idle once it actually gets all the way
            # through - the "Syncing N items..." status set before this
            # loop started is left standing, an accurate enough "not done
            # yet" for the UI in the meantime.
            return

        if is_bootstrap:
            # write synced baselines for the "trusted as already-synced" pairs
            # the bootstrap heuristic silently skipped (no action was emitted
            # for them, so nothing above would have recorded a baseline).
            # acted_paths excludes anything that DID get an action this pass
            # (including CONFLICT) - those already have a correct, fresh
            # baseline written by their own handler above (_resolve_conflict/
            # _execute_action); re-deriving from the pre-loop local_map/
            # remote_map snapshot here would clobber it with stale
            # pre-resolution data instead.
            acted_paths = {action.rel_path for action in actions}
            self._write_bootstrap_baselines(pair, local_map, remote_map, synced_map, acted_paths)

        self.db.update_pair_status(pair.id, "idle", last_sync_at=now_iso())
        self.on_status(pair.id, "Idle")

    # --- building the three maps -------------------------------------------

    def _build_local_map(self, local_root: Path, patterns: list[str]) -> dict[str, LocalEntry]:
        result: dict[str, LocalEntry] = {}
        if not local_root.exists():
            return result
        for dirpath, dirnames, filenames in os.walk(local_root, followlinks=False):
            # filtering dirnames in-place also stops os.walk descending into
            # an excluded folder's subtree entirely, not just hiding it
            dirnames[:] = [
                d for d in dirnames
                if not os.path.islink(os.path.join(dirpath, d)) and not _is_excluded(d, patterns)
            ]
            for name in dirnames:
                abs_path = Path(dirpath) / name
                rel = abs_path.relative_to(local_root).as_posix()
                result[rel] = LocalEntry(is_folder=True, size=0, mtime="")
            for name in filenames:
                if _is_excluded(name, patterns):
                    continue
                abs_path = Path(dirpath) / name
                if abs_path.is_symlink():
                    continue
                rel = abs_path.relative_to(local_root).as_posix()
                st = abs_path.stat()
                result[rel] = LocalEntry(is_folder=False, size=st.st_size, mtime=_mtime_iso(st))
        return result

    def _build_remote_map(
        self, drive_id: str, root_id: str, root_path: str, patterns: list[str]
    ) -> dict[str, RemoteEntry]:
        result: dict[str, RemoteEntry] = {}
        prefix = root_path + "/"
        for item in self.db.list_descendants(drive_id, root_id):
            if item.path == root_path:
                continue
            if not item.path.startswith(prefix):
                continue  # shouldn't happen, but don't misfile it under the wrong rel_path
            rel = item.path[len(prefix):]
            # Checked against every path component, not just item.name -
            # _build_local_map's os.walk prunes an excluded directory's
            # whole subtree, so a pattern like ".venv" excludes everything
            # under it too, not just an item literally named ".venv".
            # Checking item.name alone here missed that: a deeply-nested
            # file whose own name doesn't match any pattern (e.g. a vendored
            # package's resolver.py under .venv/lib/.../site-packages/...)
            # stayed in remote_map while local_map correctly excluded the
            # whole tree, so the mismatch got reconciled as a real deletion/
            # download instead of being left alone like the local side was.
            if any(_is_excluded(part, patterns) for part in rel.split("/")):
                continue
            if item.remote_id is None:
                # Not yet synced - most commonly an item created through the
                # offline-tolerant on-demand mount (see fuse/operations.py)
                # that also happens to fall under this pair's remote
                # subtree, still queued for its own MountSyncWorker. It
                # doesn't actually exist on Graph yet, `item.id` is a local
                # synthetic "pending:..." placeholder rather than a real
                # Graph id, and the mount's own worker already owns getting
                # it there - reconciling it here too would be wrong (and,
                # reproduced directly, sends that synthetic id straight into
                # a Graph URL and 400s every single pass).
                continue
            result[rel] = RemoteEntry(
                is_folder=item.is_folder, size=item.size, etag=item.etag or "", remote_item_id=item.remote_id,
                quickxor_hash=item.quickxor_hash,
            )
        return result

    def _attach_bootstrap_hashes(
        self, local_root: Path, local_map: dict[str, LocalEntry], remote_map: dict[str, RemoteEntry],
        synced_map: dict[str, SyncedEntry],
    ) -> None:
        """Computes quickXorHash for the (usually small) set of files that
        could actually hit reconcile_pair's bootstrap same-size heuristic -
        present on both sides, same size, no baseline yet - so that
        heuristic can cross-check content instead of trusting size alone.
        Bounded to exactly the files that matter, not every local file."""
        for rel_path, local in local_map.items():
            if local.is_folder or rel_path in synced_map:
                continue
            remote = remote_map.get(rel_path)
            if remote is None or remote.is_folder or remote.size != local.size:
                continue
            if remote.quickxor_hash is None or local.size > constants.PAIR_BOOTSTRAP_HASH_MAX_BYTES:
                continue
            try:
                data = (local_root / rel_path).read_bytes()
            except OSError:
                continue
            local_map[rel_path] = replace(local, quickxor_hash=quickxorhash_base64(data))

    def _build_synced_map(self, pair_id: int, patterns: list[str]) -> dict[str, SyncedEntry]:
        result = {}
        for pf in self.db.list_pair_files(pair_id):
            # Excluded paths must stay completely invisible to reconcile_pair -
            # not just missing from local_map/remote_map. Real bug, reported
            # live: adding "*.db" to the global ignore list made
            # _build_local_map/_build_remote_map correctly drop those paths,
            # but this map still carried their baseline, so reconcile_pair
            # saw L=None, R=None, S=present and purged the baseline outright
            # (PURGE_TOMBSTONE) - harmless to the actual files in the moment,
            # but the *next* time the pattern was removed, those same
            # already-in-sync files came back with no baseline at all and
            # were misclassified as a brand-new both-sides-present conflict
            # instead of being recognized as already synced. Filtering the
            # baseline out here too means an excluded path's pair_files row
            # is left completely untouched while excluded, so removing the
            # pattern later just resumes normal reconciliation against the
            # still-valid old baseline - not a fake conflict.
            if any(_is_excluded(part, patterns) for part in pf.rel_path.split("/")):
                continue
            result[pf.rel_path] = SyncedEntry(
                remote_item_id=pf.remote_item_id,
                last_synced_etag=pf.last_synced_etag,
                last_synced_mtime=pf.last_synced_mtime,
                last_synced_size=pf.last_synced_size,
                is_folder=pf.is_folder,
            )
        return result

    def _write_bootstrap_baselines(self, pair, local_map, remote_map, synced_map, acted_paths) -> None:
        for rel_path, L in local_map.items():
            if rel_path in synced_map or rel_path in acted_paths or L.is_folder:
                continue
            R = remote_map.get(rel_path)
            if R is None or R.size != L.size:
                continue
            self.db.upsert_pair_file(
                pair.id, rel_path, remote_item_id=R.remote_item_id, last_synced_etag=R.etag,
                last_synced_mtime=L.mtime, last_synced_size=L.size, is_folder=False,
            )
        for rel_path, L in local_map.items():
            if L.is_folder and rel_path in remote_map and rel_path not in synced_map:
                R = remote_map[rel_path]
                self.db.upsert_pair_file(
                    pair.id, rel_path, remote_item_id=R.remote_item_id, last_synced_etag=R.etag,
                    last_synced_mtime=None, last_synced_size=None, is_folder=True,
                )

    # --- resolving a parent's remote id for creates ------------------------

    def _resolve_parent_remote_id(
        self, pair, rel_path: str, remote_map: dict[str, RemoteEntry], created: dict[str, str]
    ) -> str | None:
        parent_rel = os.path.dirname(rel_path)
        if parent_rel == "":
            return pair.remote_item_id
        if parent_rel in created:
            return created[parent_rel]
        if parent_rel in remote_map:
            return remote_map[parent_rel].remote_item_id
        return None

    # --- executing one action ----------------------------------------------

    def _execute_action(
        self,
        pair,
        action: Action,
        local_map: dict[str, LocalEntry],
        remote_map: dict[str, RemoteEntry],
        created_remote_ids: dict[str, str],
        progress: tuple[int, int] | None = None,
    ) -> None:
        local_abs = Path(pair.local_path) / action.rel_path
        name = os.path.basename(action.rel_path)

        _ACTION_VERBS = {
            ActionType.UPLOAD: "Uploading",
            ActionType.DOWNLOAD: "Downloading",
            ActionType.CREATE_REMOTE_DIR: "Creating remote folder",
            ActionType.CREATE_LOCAL_DIR: "Creating local folder",
            ActionType.DELETE_REMOTE: "Deleting remotely",
            ActionType.DELETE_LOCAL: "Deleting locally",
        }
        verb = _ACTION_VERBS.get(action.type)
        if verb:
            logger.info("pair %s: %s %s", pair.id, verb, action.rel_path)
            prefix = f"{verb} ({progress[0]}/{progress[1]})" if progress else verb
            self._set_progress_status(pair.id, f"{prefix}: {action.rel_path}")

        if action.type == ActionType.CREATE_REMOTE_DIR:
            parent_id = self._resolve_parent_remote_id(pair, action.rel_path, remote_map, created_remote_ids)
            if parent_id is None:
                logger.warning("pair %s: no remote parent yet for %s, skipping this pass", pair.id, action.rel_path)
                return
            result = self.graph.create_folder(pair.drive_id, parent_id, name)
            self.db.upsert_item(pair.drive_id, result)
            created_remote_ids[action.rel_path] = result["id"]
            self.db.upsert_pair_file(
                pair.id, action.rel_path, remote_item_id=result["id"], last_synced_etag=result.get("eTag"),
                last_synced_mtime=None, last_synced_size=None, is_folder=True,
            )
            self.db.log_activity("created", name, action.rel_path, f"pair:{pair.id}", is_folder=True)

        elif action.type == ActionType.CREATE_LOCAL_DIR:
            local_abs.mkdir(parents=True, exist_ok=True)
            R = remote_map[action.rel_path]
            self.db.upsert_pair_file(
                pair.id, action.rel_path, remote_item_id=R.remote_item_id, last_synced_etag=R.etag,
                last_synced_mtime=None, last_synced_size=None, is_folder=True,
            )
            self.db.log_activity("created", name, action.rel_path, f"pair:{pair.id}", is_folder=True)

        elif action.type == ActionType.UPLOAD:
            parent_id = self._resolve_parent_remote_id(pair, action.rel_path, remote_map, created_remote_ids)
            if parent_id is None:
                logger.warning("pair %s: no remote parent yet for %s, skipping this pass", pair.id, action.rel_path)
                return
            result = self.graph.upload_file(
                pair.drive_id, parent_id, name, local_abs,
                existing_item_id=action.remote_item_id, if_match=action.last_synced_etag,
            )
            self.db.upsert_item(pair.drive_id, result)
            st = local_abs.stat()
            self.db.upsert_pair_file(
                pair.id, action.rel_path, remote_item_id=result["id"], last_synced_etag=result.get("eTag"),
                last_synced_mtime=_mtime_iso(st), last_synced_size=st.st_size, is_folder=False,
            )
            # remote_item_id is only set on a replace (_upload_replace) -
            # a brand-new file (_create_or_upload) leaves it None, which is
            # the only signal available here for "created" vs "changed".
            event_type = "uploaded" if action.remote_item_id else "created"
            self.db.log_activity(event_type, name, action.rel_path, f"pair:{pair.id}")

        elif action.type == ActionType.DOWNLOAD:
            local_abs.parent.mkdir(parents=True, exist_ok=True)
            self.graph.download_content(pair.drive_id, action.remote_item_id, local_abs)
            st = local_abs.stat()
            R = remote_map[action.rel_path]
            self.db.upsert_pair_file(
                pair.id, action.rel_path, remote_item_id=R.remote_item_id, last_synced_etag=R.etag,
                last_synced_mtime=_mtime_iso(st), last_synced_size=st.st_size, is_folder=False,
            )
            self.db.log_activity("downloaded", name, action.rel_path, f"pair:{pair.id}")

        elif action.type == ActionType.DELETE_REMOTE:
            # Use the etag from THIS pass's remote_map (fresh, built from the
            # continuously-updated items cache), not pair_files.last_synced_etag
            # (the baseline from whenever this file was first synced, which can
            # be long stale - a folder's etag in particular drifts on every
            # child change, so If-Matching against an old baseline reliably
            # 412s forever even though the item isn't actually locked).
            R = remote_map.get(action.rel_path)
            current_etag = R.etag if R is not None else None
            self.graph.delete_item(pair.drive_id, action.remote_item_id, if_match=current_etag)
            # Tombstone it in the items cache immediately, not just pair_files -
            # otherwise the very next reconciliation pass (60s later) still
            # sees this path in list_descendants() (stale items row), now with
            # no pair_files baseline since it was just purged below, misclassifies
            # it as "new, remote-only", and tries to download an item that's
            # already gone - a 404 that repeats every pass until DeltaSyncWorker's
            # independent, slower poll eventually catches up. Confirmed live via
            # a user-shared tail of exactly this repeating 404 on a duplicate
            # file this same DELETE_REMOTE branch had just deleted.
            self.db.mark_deleted(pair.drive_id, action.remote_item_id)
            self.db.purge_pair_file(pair.id, action.rel_path)
            self.db.log_activity("deleted", name, action.rel_path, f"pair:{pair.id}", is_folder=action.is_folder)

        elif action.type == ActionType.DELETE_LOCAL:
            if local_abs.is_dir() and not local_abs.is_symlink():
                local_abs.rmdir()
            elif local_abs.exists():
                local_abs.unlink()
            self.db.purge_pair_file(pair.id, action.rel_path)
            self.db.log_activity("deleted", name, action.rel_path, f"pair:{pair.id}", is_folder=action.is_folder)

        elif action.type == ActionType.PURGE_TOMBSTONE:
            self.db.purge_pair_file(pair.id, action.rel_path)

    # --- conflict resolution: keep-both, crash-safe ordering ----------------

    def _resolve_conflict(
        self, pair, action: Action, local_map: dict[str, LocalEntry], remote_map: dict[str, RemoteEntry]
    ) -> None:
        logger.info("pair %s: conflict on %s - both sides changed, keeping both", pair.id, action.rel_path)
        self._set_status(pair.id, f"Conflict: {action.rel_path}")

        local_abs = Path(pair.local_path) / action.rel_path
        R = remote_map[action.rel_path]

        conflict_rel = self._unique_conflict_path(pair, action.rel_path)
        conflict_abs = Path(pair.local_path) / conflict_rel

        # Step 1: atomic local rename - this is the step that "claims" the
        # conflict; once done, the local edit is permanently preserved.
        os.rename(local_abs, conflict_abs)

        # Step 2: upload the renamed copy as a brand-new remote file.
        parent_id = self._resolve_parent_remote_id(pair, conflict_rel, remote_map, {})
        if parent_id is None:
            parent_id = pair.remote_item_id
        result = self.graph.upload_file(
            pair.drive_id, parent_id, os.path.basename(conflict_rel), conflict_abs
        )
        self.db.upsert_item(pair.drive_id, result)
        st = conflict_abs.stat()
        self.db.upsert_pair_file(
            pair.id, conflict_rel, remote_item_id=result["id"], last_synced_etag=result.get("eTag"),
            last_synced_mtime=_mtime_iso(st), last_synced_size=st.st_size, is_folder=False,
        )

        # Step 3: download the current remote version to the now-free
        # original path. If the process dies before this, the next pass
        # re-derives it correctly via the ordinary "new remote-only" case.
        local_abs.parent.mkdir(parents=True, exist_ok=True)
        self.graph.download_content(pair.drive_id, R.remote_item_id, local_abs)
        st2 = local_abs.stat()
        self.db.upsert_pair_file(
            pair.id, action.rel_path, remote_item_id=R.remote_item_id, last_synced_etag=R.etag,
            last_synced_mtime=_mtime_iso(st2), last_synced_size=st2.st_size, is_folder=False,
        )
        self.db.log_activity("conflict", os.path.basename(action.rel_path), conflict_rel, f"pair:{pair.id}")
        self.on_conflict(os.path.basename(action.rel_path), conflict_rel)
        logger.info(
            "pair %s: conflict resolved for %s - local edit preserved as %s",
            pair.id, action.rel_path, conflict_rel,
        )

    def _unique_conflict_path(self, pair, rel_path: str) -> str:
        p = Path(rel_path)
        stem, suffix = p.stem, p.suffix
        parent = p.parent
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H%M%S")
        base_name = constants.CONFLICT_COPY_SUFFIX_FORMAT.format(stem=stem, ts=ts, suffix=suffix)
        candidate = (parent / base_name).as_posix() if str(parent) != "." else base_name
        n = 1
        while (Path(pair.local_path) / candidate).exists():
            numbered = f"{base_name} ({n})" if not suffix else f"{stem} (conflicted copy {ts}) ({n}){suffix}"
            candidate = (parent / numbered).as_posix() if str(parent) != "." else numbered
            n += 1
        return candidate
