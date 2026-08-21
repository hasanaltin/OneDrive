#!/usr/bin/env python3
"""Crash-safety regression test: create+write+flush+release an offline file
through one OneDriveOperations/Database pair, then drop both references
without ever syncing (simulating `kill -9` before MountSyncWorker got a
chance to run) - and prove a completely independent second Database +
MountSyncWorker, constructed fresh against the same on-disk files, can
still locate the staged content (via content_cache.path_for's canonical,
id-derivable path) and sync it correctly. Also forces an op to
status='in_progress' before the "restart" and confirms startup recovery
(reset_in_progress_mount_ops) resets and retries it instead of leaving it
stuck forever.

Runs against the real signed-in account. Usage:
python scripts/verify_mount_crash_recovery.py
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
    auth = AuthManager()
    graph = GraphClient(auth)
    db1 = Database()
    drive_id = db1.get_sync_state("drive_id")
    if not drive_id:
        print("No drive_id cached - sign in via the app first.")
        return 1
    account_root = db1.get_item_by_path(drive_id, "")
    if account_root is None:
        print("No root item cached - let DeltaSyncWorker finish an initial crawl first.")
        return 1

    test_name = f"verify-mount-crash-{int(time.time())}"
    print(f"Creating throwaway remote test folder: {test_name}")
    test_folder = graph.create_folder(drive_id, account_root.id, test_name)
    db1.upsert_item(drive_id, test_folder)

    ok = True
    try:
        content_cache1 = ContentCache(db1, graph)
        ops1 = OneDriveOperations(db1, content_cache1, drive_id, account_root.id, graph)
        harness1 = MountTestHarness(ops1)
        test_folder_inode = ops1._get_or_assign_inode(test_folder["id"])

        print("\n1. Offline create+write+flush+release, then dropping this process's "
              "references entirely (simulating kill -9 before any sync)...")
        real_session = go_offline(graph)
        try:
            file_id = harness1.write_new_file(test_folder_inode, "crash_test.txt", b"survives a crash")
        finally:
            go_online(graph, real_session)

        staged_path = path_for(drive_id, file_id)
        if not staged_path.exists():
            print(f"FAIL - staged content not found at the canonical path {staged_path}")
            return 1
        print(f"   staged content confirmed on disk at {staged_path} ({staged_path.stat().st_size} bytes)")

        # Force one op into 'in_progress' to simulate dying mid-network-call
        # on a PRIOR op too (e.g. the create had already started once before).
        row = db1._conn.execute(
            "SELECT seq FROM pending_mount_ops WHERE drive_id=? AND item_id=?", (drive_id, file_id)
        ).fetchone()
        db1.mark_op_in_progress(row["seq"])
        stuck = db1._conn.execute(
            "SELECT status FROM pending_mount_ops WHERE seq=?", (row["seq"],)
        ).fetchone()
        print(f"   forced op {row['seq']} to status={stuck['status']!r} (simulating a mid-flight crash)")

        del ops1, harness1, content_cache1
        db1.close()
        del db1

        print("\n2. Constructing a completely independent second Database + MountSyncWorker "
              "against the same on-disk files...")
        db2 = Database()
        stuck_after_restart = db2._conn.execute(
            "SELECT status FROM pending_mount_ops WHERE drive_id=? AND item_id=?", (drive_id, file_id)
        ).fetchone()
        print(f"   op status as seen fresh from disk: {stuck_after_restart['status']!r}")

        worker = MountSyncWorker(db2, graph, drive_id)
        # run() calls this itself on startup - called directly here so this
        # script can drive one drain pass without spinning up the thread
        db2.reset_in_progress_mount_ops(drive_id)
        recovered = db2._conn.execute(
            "SELECT status FROM pending_mount_ops WHERE drive_id=? AND item_id=?", (drive_id, file_id)
        ).fetchone()
        print(f"   after reset_in_progress_mount_ops: {recovered['status']!r}")
        if recovered["status"] != "pending":
            print("FAIL - stuck 'in_progress' op wasn't reset to 'pending' on startup recovery")
            ok = False

        worker._drain()
        file_row = db2.get_item_by_id(drive_id, file_id)
        remaining = db2.list_pending_mount_ops(drive_id)
        print(f"   after drain: remote_id={file_row.remote_id}, remaining ops={len(remaining)}")
        if file_row.remote_id is None:
            print("FAIL - the crash-surviving file never synced")
            ok = False
        if remaining:
            print(f"FAIL - {len(remaining)} ops still pending: {remaining}")
            ok = False

        try:
            remote_item = graph.get_item(drive_id, file_row.remote_id)
            downloaded = Path("/tmp") / f"{file_id.replace(':', '_')}.crashtest"
            graph.download_content(drive_id, file_row.remote_id, downloaded)
            content = downloaded.read_bytes()
            downloaded.unlink(missing_ok=True)
            if content != b"survives a crash":
                print(f"FAIL - content mismatch after crash recovery: {content!r}")
                ok = False
            else:
                print(f"   independently verified on Graph: name={remote_item['name']}, content matches")
        except Exception as e:
            print(f"FAIL - couldn't independently verify: {e}")
            ok = False

        print("\nPASS" if ok else "\nFAIL")
    finally:
        print(f"\nCleaning up remote test folder {test_name}...")
        try:
            graph.delete_item(drive_id, test_folder["id"])
        except Exception as e:
            print(f"   (cleanup warning: {e})")
        cleanup_db = Database()
        cleanup_db._conn.execute(
            "DELETE FROM pending_mount_ops WHERE item_id IN "
            "(SELECT id FROM items WHERE drive_id=? AND path LIKE ?)",
            (drive_id, f"/{test_name}%"),
        )
        cleanup_db._conn.execute(
            "DELETE FROM items WHERE drive_id=? AND path LIKE ?", (drive_id, f"/{test_name}%")
        )
        cleanup_db._conn.commit()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
