#!/usr/bin/env python3
"""Conflict-safety regression test for the mount's relocated keep-both
resolver: sync a baseline file online, go offline and edit it locally
through the mount, edit the SAME remote item directly via Graph (simulating
another device), reconnect and run one drain pass - assert a "(conflicted
copy ...)" file was created with the LOCAL bytes, the original path now has
the REMOTE bytes (both the DB row AND the actual local staged file, which
is the specific gap this v4 version fixes over the old operations.py
_resolve_conflict_upload), and a source="mount" conflict activity entry was
logged.

Runs against the real signed-in account. Usage:
python scripts/verify_mount_offline_conflict.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _offline_sim import MountTestHarness, go_offline, go_online  # noqa: E402

from onedrive.auth import AuthManager  # noqa: E402
from onedrive.content_cache import ContentCache, path_for  # noqa: E402
from onedrive.db import Database  # noqa: E402
from onedrive.fuse.operations import OneDriveOperations  # noqa: E402
from onedrive.graph_client import GraphClient  # noqa: E402
from onedrive.sync.mount_sync_worker import MountSyncWorker  # noqa: E402


def main() -> int:
    db = Database()
    auth = AuthManager()
    graph = GraphClient(auth)
    content_cache = ContentCache(db, graph)
    drive_id = db.get_sync_state("drive_id")
    if not drive_id:
        print("No drive_id cached - sign in via the app first.")
        return 1
    account_root = db.get_item_by_path(drive_id, "")
    if account_root is None:
        print("No root item cached - let DeltaSyncWorker finish an initial crawl first.")
        return 1

    test_name = f"verify-mount-conflict-{int(time.time())}"
    print(f"Creating throwaway remote test folder: {test_name}")
    test_folder = graph.create_folder(drive_id, account_root.id, test_name)
    db.upsert_item(drive_id, test_folder)

    ok = True
    try:
        ops = OneDriveOperations(db, content_cache, drive_id, account_root.id, graph)
        harness = MountTestHarness(ops)
        test_folder_inode = ops._get_or_assign_inode(test_folder["id"])

        print("\n1. Creating and syncing a baseline file (online)...")
        baseline = graph.upload_file(
            drive_id, test_folder["id"], "shared.txt",
            _write_temp(b"original synced content"), existing_item_id=None,
        )
        db.upsert_item(drive_id, baseline)
        item = db.get_item_by_id(drive_id, baseline["id"])
        # stage local content so open()-for-write can find it without a
        # real download - mirrors what a prior real open()/read would leave behind
        local_path = path_for(drive_id, item.id)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"original synced content")
        db.set_content_state(drive_id, item.id, "ready")

        print("2. Going offline and editing the local copy through the mount...")
        real_session = go_offline(graph)
        try:
            fh_info, _ = harness.create(test_folder_inode, "shared.txt")
            fh = fh_info.fh
            harness.write(fh, 0, b"MY LOCAL EDIT - should survive")
            harness.flush(fh)
            harness.release(fh)
        finally:
            go_online(graph, real_session)

        print("3. Editing the remote copy directly via Graph (simulating another device)...")
        remote_edit = graph.replace_small(drive_id, baseline["id"], b"REMOTE EDIT - should also survive")

        print("\n4. Running one MountSyncWorker drain pass...")
        worker = MountSyncWorker(db, graph, drive_id)
        worker._drain()

        children = db.list_children(drive_id, test_folder["id"])
        names = sorted(c.name for c in children)
        conflict_names = [n for n in names if "conflicted copy" in n]
        print(f"   children now: {names}")

        if "shared.txt" not in names:
            print("FAIL - original filename missing")
            ok = False
        if len(conflict_names) != 1:
            print(f"FAIL - expected exactly 1 conflict copy, found {len(conflict_names)}")
            ok = False
        else:
            conflict_item = next(c for c in children if c.name == conflict_names[0])
            conflict_bytes = graph.get_item(drive_id, conflict_item.remote_id)
            downloaded = Path("/tmp") / "verify_conflict_copy.txt"
            graph.download_content(drive_id, conflict_item.remote_id, downloaded)
            content = downloaded.read_bytes()
            downloaded.unlink(missing_ok=True)
            if content != b"MY LOCAL EDIT - should survive":
                print(f"FAIL - conflict copy doesn't contain the local edit: {content!r}")
                ok = False
            else:
                print(f"   conflict copy '{conflict_item.name}' correctly preserves the local edit "
                      f"(remote name confirmed: {conflict_bytes['name']})")

        original_row = db.get_item_by_id(drive_id, baseline["id"])
        original_local_path = path_for(drive_id, baseline["id"])
        original_local_bytes = original_local_path.read_bytes() if original_local_path.exists() else None
        print(f"   original DB row: etag={original_row.etag}, content_state={original_row.content_state}")
        print(f"   original LOCAL STAGED FILE bytes: {original_local_bytes!r}")

        if original_row.etag != remote_edit.get("eTag"):
            print("FAIL - original item's DB row wasn't refreshed to the current remote etag")
            ok = False
        if original_local_bytes != b"REMOTE EDIT - should also survive":
            print("FAIL - original item's LOCAL STAGED FILE still has stale bytes - this is exactly "
                  "the gap the v4 relocated resolver was supposed to fix over the old "
                  "operations.py._resolve_conflict_upload (which never re-downloaded)")
            ok = False
        else:
            print("   original local file correctly holds the fresh remote content - OK")

        conflicts = db.list_conflicts("mount")
        matching = [c for c in conflicts if c["name"] == "shared.txt"]
        if not matching:
            print("FAIL - no source='mount' conflict activity entry was logged")
            ok = False
        else:
            print(f"   conflict activity logged: {matching[0]}")

        print("\nPASS" if ok else "\nFAIL")
    finally:
        print(f"\nCleaning up remote test folder {test_name}...")
        try:
            graph.delete_item(drive_id, test_folder["id"])
        except Exception as e:
            print(f"   (cleanup warning: {e})")
        db._conn.execute(
            "DELETE FROM pending_mount_ops WHERE item_id IN "
            "(SELECT id FROM items WHERE drive_id=? AND path LIKE ?)",
            (drive_id, f"/{test_name}%"),
        )
        db._conn.execute(
            "DELETE FROM items WHERE drive_id=? AND path LIKE ?", (drive_id, f"/{test_name}%")
        )
        # the conflict log entry's "path" field is just the bare conflict-copy
        # filename (no folder prefix), so it can't be matched by test_name -
        # match on the narrower (source, name) combination instead
        db._conn.execute(
            "DELETE FROM activity_log WHERE source='mount' AND event_type='conflict' AND name='shared.txt'"
        )
        db._conn.commit()

    return 0 if ok else 1


def _write_temp(data: bytes) -> Path:
    p = Path("/tmp") / f"verify_mount_conflict_baseline_{int(time.time() * 1000)}.txt"
    p.write_bytes(data)
    return p


if __name__ == "__main__":
    sys.exit(main())
