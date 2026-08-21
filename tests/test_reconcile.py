"""Unit tests for sync/reconcile.py - the three-way (local/remote/synced)
classification logic that decides what gets uploaded, downloaded, deleted,
or flagged as a conflict for every file in a Folder Pair.

Deliberately the one file in this project with a real automated test suite:
reconcile_pair() is pure logic (no filesystem, no network, no DB) that
directly decides whether a file gets overwritten or deleted, so a wrong
classification is the single most consequential kind of bug this project
can ship - and, unlike everything else here, it needs no real OneDrive
account to test at all. Every other module either does real I/O (verified
via scripts/verify_*.py against a live account) or is thin GUI plumbing
around this logic.

Covers every branch of the classification table in reconcile.py's own
module docstring/plan notes: unchanged, local-only change, remote-only
change, both-changed conflict, every delete/tombstone combination, new
files with no synced baseline (including the first-pairing "bootstrap"
same-size heuristic), and the execution-order guarantees (dirs before
files, parents before children, deletes deepest-first).
"""

from onedrive.sync.reconcile import (
    ActionType,
    LocalEntry,
    RemoteEntry,
    SyncedEntry,
    reconcile_pair,
)


def _local(size=100, mtime="2026-01-01T00:00:00+00:00", is_folder=False, quickxor_hash=None):
    return LocalEntry(is_folder=is_folder, size=size, mtime=mtime, quickxor_hash=quickxor_hash)


def _remote(size=100, etag="etag-1", remote_item_id="R1", is_folder=False, quickxor_hash=None):
    return RemoteEntry(
        is_folder=is_folder, size=size, etag=etag, remote_item_id=remote_item_id, quickxor_hash=quickxor_hash
    )


def _synced(size=100, mtime="2026-01-01T00:00:00+00:00", etag="etag-1",
            remote_item_id="R1", is_folder=False):
    return SyncedEntry(
        remote_item_id=remote_item_id, last_synced_etag=etag,
        last_synced_mtime=mtime, last_synced_size=size, is_folder=is_folder,
    )


def _types(actions):
    return {a.rel_path: a.type for a in actions}


# --- files: no synced baseline (S is None) ---------------------------------

def test_new_file_local_only_uploads():
    actions = reconcile_pair({"a.txt": _local()}, {}, {})
    assert _types(actions) == {"a.txt": ActionType.UPLOAD}


def test_new_file_remote_only_downloads():
    actions = reconcile_pair({}, {"a.txt": _remote()}, {})
    assert _types(actions) == {"a.txt": ActionType.DOWNLOAD}


def test_new_file_both_sides_no_baseline_is_conflict():
    actions = reconcile_pair({"a.txt": _local(size=100)}, {"a.txt": _remote(size=200)}, {})
    assert _types(actions) == {"a.txt": ActionType.CONFLICT}


def test_bootstrap_same_size_trusted_as_already_synced():
    # First-ever pairing pass: both sides already have the file, same size -
    # trusted as already in sync rather than flagged as a conflict, so
    # re-pairing an existing folder doesn't produce a wall of false
    # conflicts for content that was never actually touched by this pair.
    actions = reconcile_pair(
        {"a.txt": _local(size=100)}, {"a.txt": _remote(size=100)}, {}, is_bootstrap=True,
    )
    assert actions == []


def test_bootstrap_different_size_is_still_conflict():
    # The bootstrap heuristic only trusts a size match - different sizes on
    # first pairing genuinely can't be reconciled without picking a side.
    actions = reconcile_pair(
        {"a.txt": _local(size=100)}, {"a.txt": _remote(size=200)}, {}, is_bootstrap=True,
    )
    assert _types(actions) == {"a.txt": ActionType.CONFLICT}


def test_bootstrap_same_size_same_hash_trusted_as_already_synced():
    # Hardened heuristic: when both sides report a quickXorHash, same size
    # AND same hash is trusted - same outcome as the size-only case, just
    # with actual content confirmation this time.
    actions = reconcile_pair(
        {"a.txt": _local(size=100, quickxor_hash="AAAA")},
        {"a.txt": _remote(size=100, quickxor_hash="AAAA")},
        {}, is_bootstrap=True,
    )
    assert actions == []


def test_bootstrap_same_size_different_hash_is_conflict():
    # The gap the hardening exists to close: two coincidentally-same-size
    # but genuinely different files must NOT be silently treated as
    # already-synced just because their byte size happens to match.
    actions = reconcile_pair(
        {"a.txt": _local(size=100, quickxor_hash="AAAA")},
        {"a.txt": _remote(size=100, quickxor_hash="BBBB")},
        {}, is_bootstrap=True,
    )
    assert _types(actions) == {"a.txt": ActionType.CONFLICT}


def test_bootstrap_same_size_hash_unavailable_falls_back_to_size_only():
    # Hash missing on either side (e.g. local file too large to hash, or
    # Graph hasn't computed one for the remote item) - falls back to the
    # original, narrower same-size-only trust rather than refusing to
    # bootstrap at all.
    actions = reconcile_pair(
        {"a.txt": _local(size=100, quickxor_hash=None)},
        {"a.txt": _remote(size=100, quickxor_hash="BBBB")},
        {}, is_bootstrap=True,
    )
    assert actions == []


# --- files: synced baseline exists ------------------------------------------

def test_unchanged_file_is_noop():
    actions = reconcile_pair({"a.txt": _local()}, {"a.txt": _remote()}, {"a.txt": _synced()})
    assert actions == []


def test_local_only_change_uploads():
    actions = reconcile_pair(
        {"a.txt": _local(size=999)}, {"a.txt": _remote()}, {"a.txt": _synced()},
    )
    assert _types(actions) == {"a.txt": ActionType.UPLOAD}


def test_remote_only_change_downloads():
    actions = reconcile_pair(
        {"a.txt": _local()}, {"a.txt": _remote(etag="etag-2")}, {"a.txt": _synced()},
    )
    assert _types(actions) == {"a.txt": ActionType.DOWNLOAD}


def test_both_sides_changed_is_conflict():
    actions = reconcile_pair(
        {"a.txt": _local(size=999)}, {"a.txt": _remote(etag="etag-2")}, {"a.txt": _synced()},
    )
    assert _types(actions) == {"a.txt": ActionType.CONFLICT}


def test_deleted_both_sides_purges_tombstone():
    actions = reconcile_pair({}, {}, {"a.txt": _synced()})
    assert _types(actions) == {"a.txt": ActionType.PURGE_TOMBSTONE}


def test_local_delete_propagates_to_remote():
    actions = reconcile_pair({}, {"a.txt": _remote()}, {"a.txt": _synced()})
    assert _types(actions) == {"a.txt": ActionType.DELETE_REMOTE}


def test_remote_delete_propagates_to_local():
    actions = reconcile_pair({"a.txt": _local()}, {}, {"a.txt": _synced()})
    assert _types(actions) == {"a.txt": ActionType.DELETE_LOCAL}


def test_deleted_locally_but_remote_also_changed_redownloads_not_deletes():
    # A stale local delete must never win over remote content the user
    # hasn't even seen yet - re-download instead of deleting remotely.
    actions = reconcile_pair(
        {}, {"a.txt": _remote(etag="etag-2")}, {"a.txt": _synced()},
    )
    assert _types(actions) == {"a.txt": ActionType.DOWNLOAD}


def test_deleted_remotely_but_local_also_changed_reuploads_not_deletes():
    # A remote delete must never destroy an unseen local edit - re-upload
    # (as a fresh create) instead of deleting the local copy.
    actions = reconcile_pair(
        {"a.txt": _local(size=999)}, {}, {"a.txt": _synced()},
    )
    assert _types(actions) == {"a.txt": ActionType.UPLOAD}


# --- folders: presence/absence only, never diffed for content --------------

def test_folder_present_both_sides_is_noop():
    actions = reconcile_pair(
        {"dir": _local(is_folder=True)}, {"dir": _remote(is_folder=True)}, {},
    )
    assert actions == []


def test_folder_local_only_creates_remote_dir():
    actions = reconcile_pair({"dir": _local(is_folder=True)}, {}, {})
    assert _types(actions) == {"dir": ActionType.CREATE_REMOTE_DIR}


def test_folder_remote_only_creates_local_dir():
    actions = reconcile_pair({}, {"dir": _remote(is_folder=True)}, {})
    assert _types(actions) == {"dir": ActionType.CREATE_LOCAL_DIR}


def test_folder_local_only_but_previously_synced_means_remote_deleted_it():
    actions = reconcile_pair(
        {"dir": _local(is_folder=True)}, {}, {"dir": _synced(is_folder=True)},
    )
    assert _types(actions) == {"dir": ActionType.DELETE_LOCAL}


def test_folder_remote_only_but_previously_synced_means_local_deleted_it():
    actions = reconcile_pair(
        {}, {"dir": _remote(is_folder=True)}, {"dir": _synced(is_folder=True)},
    )
    assert _types(actions) == {"dir": ActionType.DELETE_REMOTE}


def test_folder_gone_both_sides_purges_tombstone():
    actions = reconcile_pair({}, {}, {"dir": _synced(is_folder=True)})
    assert _types(actions) == {"dir": ActionType.PURGE_TOMBSTONE}


def test_folder_gone_both_sides_no_baseline_is_noop():
    actions = reconcile_pair({}, {}, {})
    assert actions == []


# --- execution ordering ------------------------------------------------------

def test_folders_created_before_files():
    actions = reconcile_pair(
        {"file.txt": _local(), "dir": _local(is_folder=True)}, {}, {},
    )
    assert [a.rel_path for a in actions] == ["dir", "file.txt"]


def test_creates_ordered_parents_before_children():
    actions = reconcile_pair(
        {
            "a/b/deep.txt": _local(),
            "a/mid.txt": _local(),
            "shallow.txt": _local(),
        },
        {}, {},
    )
    paths = [a.rel_path for a in actions]
    assert paths.index("shallow.txt") < paths.index("a/mid.txt")
    assert paths.index("a/mid.txt") < paths.index("a/b/deep.txt")


def test_deletes_ordered_deepest_first():
    actions = reconcile_pair(
        {}, {},
        {
            "a/b/deep.txt": _synced(),
            "a/mid.txt": _synced(),
            "shallow.txt": _synced(),
        },
    )
    paths = [a.rel_path for a in actions]
    assert paths.index("a/b/deep.txt") < paths.index("a/mid.txt")
    assert paths.index("a/mid.txt") < paths.index("shallow.txt")


def test_deletes_always_ordered_after_creates_and_writes():
    actions = reconcile_pair(
        {"new.txt": _local()}, {},
        {"gone.txt": _synced()},
    )
    types = [a.type for a in actions]
    assert types == [ActionType.UPLOAD, ActionType.PURGE_TOMBSTONE]
