"""Pure three-way (local / remote / last-synced) diff and classification for
one Folder Pair. No I/O, no threading, no DB/network calls in this module -
callers build the three input maps and this just decides what to do. That
makes this the one file worth unit-testing directly and trusting completely;
everything else is plumbing around it.
"""

from dataclasses import dataclass
from enum import Enum


class ActionType(str, Enum):
    NOOP = "noop"
    UPLOAD = "upload"  # local -> remote (new file, or replacing existing remote content)
    DOWNLOAD = "download"  # remote -> local (new file, or replacing existing local content)
    CREATE_LOCAL_DIR = "create_local_dir"
    CREATE_REMOTE_DIR = "create_remote_dir"
    DELETE_REMOTE = "delete_remote"
    DELETE_LOCAL = "delete_local"
    PURGE_TOMBSTONE = "purge_tombstone"
    CONFLICT = "conflict"


@dataclass
class LocalEntry:
    is_folder: bool
    size: int
    mtime: str  # ISO8601, from os.stat
    quickxor_hash: str | None = None  # only populated for bootstrap same-size candidates


@dataclass
class RemoteEntry:
    is_folder: bool
    size: int
    etag: str
    remote_item_id: str
    quickxor_hash: str | None = None  # Graph's file.hashes.quickXorHash, when it has one


@dataclass
class SyncedEntry:
    remote_item_id: str | None
    last_synced_etag: str | None
    last_synced_mtime: str | None
    last_synced_size: int | None
    is_folder: bool


@dataclass
class Action:
    rel_path: str
    type: ActionType
    is_folder: bool = False
    remote_item_id: str | None = None
    last_synced_etag: str | None = None


def _local_matches_synced(local: LocalEntry, synced: SyncedEntry) -> bool:
    return local.mtime == synced.last_synced_mtime and local.size == synced.last_synced_size


def _remote_matches_synced(remote: RemoteEntry, synced: SyncedEntry) -> bool:
    return remote.etag == synced.last_synced_etag


def reconcile_pair(
    local: dict[str, LocalEntry],
    remote: dict[str, RemoteEntry],
    synced: dict[str, SyncedEntry],
    *,
    is_bootstrap: bool = False,
) -> list[Action]:
    """Classifies every rel_path present in any of the three maps and returns
    the actions needed, in a safe execution order: directory creates before
    file writes, parents before children (by path depth), deletes last and
    deepest-first so a folder is never removed while a pending child action
    still references it."""
    actions: list[Action] = []
    all_paths = set(local) | set(remote) | set(synced)

    for rel_path in all_paths:
        L = local.get(rel_path)
        R = remote.get(rel_path)
        S = synced.get(rel_path)

        # Folders have no content to diff - etag/mtime drift on a directory
        # is noise (etag changes when children change; mtime is whatever the
        # OS feels like). Presence/absence is the only thing that matters:
        # create on whichever side is missing, delete-tracking if both
        # vanished, otherwise leave alone - never upload/download/conflict.
        is_folder = (L and L.is_folder) or (R and R.is_folder) or (S and S.is_folder)
        if is_folder:
            if L is not None and R is not None:
                continue  # exists both sides - nothing to reconcile for a folder
            if L is None and R is None:
                if S is not None:
                    actions.append(Action(rel_path, ActionType.PURGE_TOMBSTONE, is_folder=True))
                continue
            if L is not None:  # R missing
                if S is None:
                    actions.append(Action(rel_path, ActionType.CREATE_REMOTE_DIR, is_folder=True))
                else:
                    actions.append(Action(rel_path, ActionType.DELETE_LOCAL, is_folder=True))
            else:  # L missing, R present
                if S is None:
                    actions.append(
                        Action(rel_path, ActionType.CREATE_LOCAL_DIR, is_folder=True, remote_item_id=R.remote_item_id)
                    )
                else:
                    actions.append(Action(rel_path, ActionType.DELETE_REMOTE, is_folder=True, remote_item_id=R.remote_item_id))
            continue

        if S is None:
            if L is not None and R is not None:
                # new on both sides, no baseline - conflict, EXCEPT the
                # bootstrap heuristic: re-pairing already-in-sync content,
                # trust as "already synced" so it doesn't all look like a
                # wall of conflicts on first pairing.
                if is_bootstrap and _bootstrap_trusted(L, R):
                    continue  # treated as already-synced; caller writes a synced baseline
                actions.append(_conflict(rel_path, L))
            elif L is not None:
                actions.append(_create_or_upload(rel_path, L))
            elif R is not None:
                actions.append(_create_or_download(rel_path, R))
            continue

        if L is None and R is None:
            actions.append(Action(rel_path, ActionType.PURGE_TOMBSTONE))
            continue

        if L is not None and R is not None:
            local_changed = not _local_matches_synced(L, S)
            remote_changed = not _remote_matches_synced(R, S)
            if local_changed and remote_changed:
                actions.append(_conflict(rel_path, L))
            elif local_changed:
                actions.append(_upload_replace(rel_path, L, R))
            elif remote_changed:
                actions.append(_download_replace(rel_path, R))
            # else: unchanged, nothing to do
            continue

        if L is None and R is not None:
            # remote absent case handled above; here R present, L absent
            remote_changed = not _remote_matches_synced(R, S)
            if remote_changed:
                # deleted locally, modified remotely - never let a stale
                # local delete win over remote content the user hasn't seen
                actions.append(_create_or_download(rel_path, R))
            else:
                actions.append(Action(rel_path, ActionType.DELETE_REMOTE, remote_item_id=R.remote_item_id))
            continue

        if L is not None and R is None:
            local_changed = not _local_matches_synced(L, S)
            if local_changed:
                # modified locally, deleted remotely - never destroy an
                # unseen local edit to match a remote delete
                actions.append(_create_or_upload(rel_path, L))
            else:
                actions.append(Action(rel_path, ActionType.DELETE_LOCAL))
            continue

    return _sorted_for_execution(actions)


def _bootstrap_trusted(local: LocalEntry, remote: RemoteEntry) -> bool:
    """Re-pairing already-in-sync content, first pass only. Same size used
    to be trusted outright; now additionally cross-checked by quickXorHash
    when both sides report one, so two different files that happen to
    share a byte size are correctly caught as a genuine conflict instead of
    silently treated as already-synced. Hash unavailable on either side
    (caller skips hashing local files over PAIR_BOOTSTRAP_HASH_MAX_BYTES,
    or Graph simply hasn't computed one for the remote item) falls back to
    the original same-size-only trust - same documented risk as before,
    just narrower now."""
    if local.size != remote.size:
        return False
    if local.quickxor_hash is not None and remote.quickxor_hash is not None:
        return local.quickxor_hash == remote.quickxor_hash
    return True


def _conflict(rel_path: str, local: LocalEntry) -> Action:
    return Action(rel_path, ActionType.CONFLICT, is_folder=local.is_folder)


def _create_or_upload(rel_path: str, local: LocalEntry) -> Action:
    if local.is_folder:
        return Action(rel_path, ActionType.CREATE_REMOTE_DIR, is_folder=True)
    return Action(rel_path, ActionType.UPLOAD, is_folder=False)


def _create_or_download(rel_path: str, remote: RemoteEntry) -> Action:
    if remote.is_folder:
        return Action(rel_path, ActionType.CREATE_LOCAL_DIR, is_folder=True, remote_item_id=remote.remote_item_id)
    return Action(rel_path, ActionType.DOWNLOAD, is_folder=False, remote_item_id=remote.remote_item_id)


def _upload_replace(rel_path: str, local: LocalEntry, remote: RemoteEntry) -> Action:
    return Action(
        rel_path, ActionType.UPLOAD, is_folder=local.is_folder,
        remote_item_id=remote.remote_item_id, last_synced_etag=remote.etag,
    )


def _download_replace(rel_path: str, remote: RemoteEntry) -> Action:
    return Action(
        rel_path, ActionType.DOWNLOAD, is_folder=remote.is_folder,
        remote_item_id=remote.remote_item_id,
    )


_DELETE_TYPES = {ActionType.DELETE_LOCAL, ActionType.DELETE_REMOTE, ActionType.PURGE_TOMBSTONE}


def _sorted_for_execution(actions: list[Action]) -> list[Action]:
    creates_and_writes = [a for a in actions if a.type not in _DELETE_TYPES]
    deletes = [a for a in actions if a.type in _DELETE_TYPES]

    # dirs before files, then shallow before deep (parents before children)
    creates_and_writes.sort(key=lambda a: (0 if a.is_folder else 1, a.rel_path.count("/")))
    # deepest first, so a folder isn't removed while a child delete is pending
    deletes.sort(key=lambda a: -a.rel_path.count("/"))

    return creates_and_writes + deletes
