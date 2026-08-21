#!/usr/bin/env python3
"""Targets the subtlest bug found during the v4 design review directly: an
offline-created item's create can still be mid-flight (Graph call already
sent, response not back yet) at the exact moment the user deletes it. The
delete-side short-circuit ("still-unsynced item, just purge locally, no
network involved") must NOT fire in that window - otherwise the in-flight
create's eventual success calls confirm_synced_item/upsert_item and
resurrects a file the user already deleted. Instead it must fall through to
a real queued delete that, once the create settles, finds the item's
now-known remote id (even though mark_deleted has already made it invisible
to every normal getter) and issues a real delete against it.

Runs against the real signed-in account - actually creates the file on
Graph mid-test to prove the final delete is real, not simulated. Usage:
python scripts/verify_mount_delete_race.py
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

    test_name = f"verify-mount-race-{int(time.time())}"
    print(f"Creating throwaway remote test folder: {test_name}")
    test_folder = graph.create_folder(drive_id, account_root.id, test_name)
    db.upsert_item(drive_id, test_folder)

    ok = True
    try:
        ops = OneDriveOperations(db, content_cache, drive_id, account_root.id, graph)
        harness = MountTestHarness(ops)
        test_folder_inode = ops._get_or_assign_inode(test_folder["id"])

        print("\n1. Offline-creating a file (its create is queued but not yet synced)...")
        real_session = go_offline(graph)
        try:
            file_id = harness.write_new_file(test_folder_inode, "raced.txt", b"racy content")
        finally:
            go_online(graph, real_session)

        create_op = db._conn.execute(
            "SELECT seq FROM pending_mount_ops WHERE drive_id=? AND item_id=? AND op_type='create_file'",
            (drive_id, file_id),
        ).fetchone()
        print(f"   create_file op queued at seq={create_op['seq']}")

        print("2. Simulating the worker having already picked up this create and being "
              "mid-network-call on it (marking the op 'in_progress')...")
        db.mark_op_in_progress(create_op["seq"])

        print("3. Deleting the file through the mount WHILE that create is still in flight...")
        harness.unlink(test_folder_inode, "raced.txt")

        visible = db.get_item_by_id(drive_id, file_id)
        hidden_row = db.get_item_by_id_any(drive_id, file_id)
        delete_op = db._conn.execute(
            "SELECT seq, snapshot_remote_id FROM pending_mount_ops WHERE drive_id=? AND item_id=? "
            "AND op_type='delete'",
            (drive_id, file_id),
        ).fetchone()
        print(f"   get_item_by_id (normal getter): {visible}")
        print(f"   get_item_by_id_any (deleted-inclusive): remote_id={hidden_row.remote_id if hidden_row else None}")
        print(f"   queued delete op: seq={delete_op['seq'] if delete_op else None}, "
              f"snapshot_remote_id={delete_op['snapshot_remote_id'] if delete_op else None}")

        if visible is not None:
            print("FAIL - item still visible via the normal getter after unlink()")
            ok = False
        if hidden_row is None:
            print("FAIL - item was purged outright (took the fast local-purge path) instead of "
                  "falling through to a queued delete - this is exactly the race this test targets")
            ok = False
        if delete_op is None:
            print("FAIL - no 'delete' op was queued")
            ok = False
        elif delete_op["snapshot_remote_id"] is not None:
            print("FAIL - expected snapshot_remote_id=None (unknown at enqueue time, create still in flight)")
            ok = False
        else:
            print("   correctly fell through to a queued delete with remote_id unknown - OK")

        print("\n4. Simulating the in-flight create finally succeeding (real Graph call)...")
        local_path = path_for(drive_id, file_id)
        result = graph.upload_file(
            drive_id, test_folder["id"], "raced.txt", local_path, existing_item_id=None,
        )
        db.confirm_synced_item(drive_id, file_id, result)
        db.delete_op(create_op["seq"])
        print(f"   create settled: real remote_id={result['id']}")

        still_hidden = db.get_item_by_id(drive_id, file_id)
        if still_hidden is not None:
            print("FAIL - resurrected! confirm_synced_item's UPDATE brought the deleted item back "
                  "to visible (deleted=0) - this is the exact resurrection bug this design avoids")
            ok = False
        else:
            print("   item correctly stays invisible after the create settles - not resurrected")

        print("\n5. Running one MountSyncWorker drain pass to process the queued delete...")
        worker = MountSyncWorker(db, graph, drive_id)
        worker._drain()

        remaining = db.list_pending_mount_ops(drive_id)
        print(f"   remaining pending ops: {len(remaining)}")
        if remaining:
            print(f"FAIL - ops still pending after drain: {remaining}")
            ok = False

        try:
            graph.get_item(drive_id, result["id"])
            print("FAIL - item still exists on Graph after the drain pass - delete never actually ran")
            ok = False
        except Exception:
            print("   independently confirmed via Graph: the item is genuinely deleted, not resurrected")

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
        db._conn.commit()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
