#!/usr/bin/env python3
"""End-to-end happy path for the v4 offline-tolerant on-demand mount:
mkdir/create/write/unlink through OneDriveOperations while "offline" (no
FUSEError, no hang), then one MountSyncWorker drain pass after "reconnecting"
uploads everything to a real, throwaway OneDrive test folder - verified via
an independent graph.get_item()/download_content() call, not just DB state.

Runs against the real signed-in account (same pattern as the existing
scripts/verify_pair_*.py scripts) - creates one throwaway remote folder
under the account root, cleans it up at the end regardless of outcome.

Usage: python scripts/verify_mount_offline_writes.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _offline_sim import MountTestHarness, go_offline, go_online  # noqa: E402

from onedrive.auth import AuthManager  # noqa: E402
from onedrive.content_cache import ContentCache  # noqa: E402
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

    test_name = f"verify-mount-offline-{int(time.time())}"
    print(f"Creating throwaway remote test folder: {test_name}")
    test_folder = graph.create_folder(drive_id, account_root.id, test_name)
    db.upsert_item(drive_id, test_folder)

    ok = True
    try:
        ops = OneDriveOperations(db, content_cache, drive_id, account_root.id, graph)
        harness = MountTestHarness(ops)
        test_folder_inode = ops._get_or_assign_inode(test_folder["id"])

        print("\n1. Going offline and performing mkdir/create/write/unlink through the mount...")
        real_session = go_offline(graph)
        try:
            dir_attrs = harness.mkdir(test_folder_inode, "offline_dir")
            dir_inode = dir_attrs.st_ino
            file_id = harness.write_new_file(test_folder_inode, "offline_file.txt", b"hello offline world")
            nested_id = harness.write_new_file(dir_inode, "nested.txt", b"nested content")
            # a file created and then deleted entirely offline should never
            # reach the network at all
            throwaway_id = harness.write_new_file(test_folder_inode, "throwaway.txt", b"discard me")
            harness.unlink(test_folder_inode, "throwaway.txt")
        finally:
            go_online(graph, real_session)

        file_row = db.get_item_by_id(drive_id, file_id)
        nested_row = db.get_item_by_id(drive_id, nested_id)
        throwaway_row = db.get_item_by_id(drive_id, throwaway_id)
        pending_ops = db.list_pending_mount_ops(drive_id)

        print(f"   file_row: id={file_row.id} remote_id={file_row.remote_id} size={file_row.size}")
        print(f"   nested_row: id={nested_row.id} remote_id={nested_row.remote_id}")
        print(f"   throwaway_row (should be gone): {throwaway_row}")
        print(f"   pending ops queued: {len(pending_ops)} -> {[o['op_type'] for o in pending_ops]}")

        if file_row.remote_id is not None or nested_row.remote_id is not None:
            print("FAIL - an offline-created item already has a remote_id before any sync ran")
            ok = False
        if throwaway_row is not None:
            print("FAIL - throwaway.txt (created+deleted offline) should have been purged locally, "
                  "never reaching pending_mount_ops at all")
            ok = False
        if any(o["item_id"] == throwaway_id for o in pending_ops):
            print("FAIL - throwaway.txt's create leaked into the sync queue despite being deleted offline")
            ok = False
        children = {c.name for c in db.list_children(drive_id, test_folder["id"])}
        if "offline_dir" not in children or "offline_file.txt" not in children:
            print(f"FAIL - offline-created items aren't visible via list_children (readdir): {children}")
            ok = False
        else:
            print("   offline-created items are immediately visible via list_children - OK")

        print("\n2. Reconnecting and running one MountSyncWorker drain pass...")
        worker = MountSyncWorker(db, graph, drive_id)
        worker._drain()

        file_row = db.get_item_by_id(drive_id, file_id)
        nested_row = db.get_item_by_id(drive_id, nested_id)
        remaining_ops = db.list_pending_mount_ops(drive_id)
        print(f"   file_row after sync: remote_id={file_row.remote_id} etag={file_row.etag}")
        print(f"   nested_row after sync: remote_id={nested_row.remote_id}")
        print(f"   remaining pending ops: {len(remaining_ops)}")

        if file_row.remote_id is None or nested_row.remote_id is None:
            print("FAIL - remote_id still not set after a drain pass")
            ok = False
        if remaining_ops:
            print(f"FAIL - {len(remaining_ops)} ops still pending after one drain pass: {remaining_ops}")
            ok = False

        print("\n3. Verifying independently via a fresh Graph GET + download...")
        try:
            remote_file = graph.get_item(drive_id, file_row.remote_id)
            downloaded_path = Path("/tmp") / f"{file_id.replace(':', '_')}.verify"
            graph.download_content(drive_id, file_row.remote_id, downloaded_path)
            content = downloaded_path.read_bytes()
            downloaded_path.unlink(missing_ok=True)
            if content != b"hello offline world":
                print(f"FAIL - uploaded content mismatch: {content!r}")
                ok = False
            else:
                print(f"   remote item confirmed: name={remote_file['name']} size={remote_file['size']} "
                      f"content matches")
        except Exception as e:
            print(f"FAIL - couldn't independently verify remote state: {e}")
            ok = False

        print("\nPASS" if ok else "\nFAIL")
    finally:
        print(f"\nCleaning up remote test folder {test_name}...")
        try:
            graph.delete_item(drive_id, test_folder["id"])
        except Exception as e:
            print(f"   (cleanup warning: {e})")
        # Scoped to this test's own path prefix only - a blanket "id LIKE
        # pending:%" wipe would delete any OTHER offline-created item a real
        # user has genuinely pending right now.
        db._conn.execute(
            "DELETE FROM pending_mount_ops WHERE item_id IN "
            "(SELECT id FROM items WHERE drive_id=? AND path LIKE ?)",
            (drive_id, f"/{test_name}%"),
        )
        db._conn.execute(
            "DELETE FROM items WHERE drive_id=? AND path LIKE ?",
            (drive_id, f"/{test_name}%"),
        )
        db._conn.commit()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
