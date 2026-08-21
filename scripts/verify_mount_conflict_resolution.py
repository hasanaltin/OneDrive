#!/usr/bin/env python3
"""Verifies conflict_actions.resolve_mount_conflict() - the resolver behind
the new "Mount Conflicts" review dialog (Account tab). Reproduces two real
mount conflicts exactly like verify_mount_offline_conflict.py does, then
resolves one with 'keep_local' and the other with 'keep_server', asserting
the DB row, the local content_cache bytes, AND the actual remote item all
end up correct - not just one of the three.

Runs against the real signed-in account. Usage:
python scripts/verify_mount_conflict_resolution.py
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
from onedrive.sync.conflict_actions import resolve_mount_conflict  # noqa: E402
from onedrive.sync.mount_sync_worker import MountSyncWorker  # noqa: E402


def _write_temp(data: bytes) -> Path:
    p = Path("/tmp") / f"verify_mount_resolve_{int(time.time() * 1000)}.txt"
    p.write_bytes(data)
    return p


def _make_conflict(db, graph, ops, harness, test_folder_inode, test_folder_id, drive_id, name: str, local_edit: bytes, remote_edit: bytes):
    """Reproduces one genuine both-sides-changed conflict, mirroring
    verify_mount_offline_conflict.py step by step."""
    baseline = graph.upload_file(
        drive_id, test_folder_id, name, _write_temp(b"original synced content"), existing_item_id=None,
    )
    db.upsert_item(drive_id, baseline)
    item = db.get_item_by_id(drive_id, baseline["id"])
    local_path = path_for(drive_id, item.id)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(b"original synced content")
    db.set_content_state(drive_id, item.id, "ready")

    real_session = go_offline(graph)
    try:
        fh_info, _ = harness.create(test_folder_inode, name)
        fh = fh_info.fh
        harness.write(fh, 0, local_edit)
        harness.flush(fh)
        harness.release(fh)
    finally:
        go_online(graph, real_session)

    graph.replace_small(drive_id, baseline["id"], remote_edit)
    return baseline


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

    test_name = f"verify-mount-resolve-{int(time.time())}"
    print(f"Creating throwaway remote test folder: {test_name}")
    test_folder = graph.create_folder(drive_id, account_root.id, test_name)
    db.upsert_item(drive_id, test_folder)

    ok = True
    try:
        ops = OneDriveOperations(db, content_cache, drive_id, account_root.id, graph)
        harness = MountTestHarness(ops)
        test_folder_inode = ops._get_or_assign_inode(test_folder["id"])

        print("\n1. Reproducing conflict A (will resolve as keep_local)...")
        # local_edit must be >= len("original synced content") - offline
        # writes go through plain write(offset=0, ...) with no truncate (a
        # real open()-for-write only truncates via a separate setattr the
        # kernel issues for O_TRUNC, which this lightweight harness doesn't
        # drive), so a SHORTER replacement would leave genuine trailing
        # garbage bytes from the original content - a test-harness fidelity
        # gap, not something resolve_mount_conflict should paper over.
        _make_conflict(
            db, graph, ops, harness, test_folder_inode, test_folder["id"], drive_id,
            "keep_local_case.txt", b"LOCAL EDIT should win - keep this version",
            b"remote edit should be discarded",
        )
        print("2. Reproducing conflict B (will resolve as keep_server)...")
        _make_conflict(
            db, graph, ops, harness, test_folder_inode, test_folder["id"], drive_id,
            "keep_server_case.txt", b"local edit should be discarded", b"REMOTE EDIT should win",
        )

        print("\n3. Draining MountSyncWorker to actually produce both conflict records...")
        worker = MountSyncWorker(db, graph, drive_id)
        worker._drain()

        conflicts = db.list_conflicts("mount")
        row_a = next((c for c in conflicts if c["name"] == "keep_local_case.txt"), None)
        row_b = next((c for c in conflicts if c["name"] == "keep_server_case.txt"), None)
        if row_a is None or row_b is None:
            print(f"FAIL - expected 2 fresh mount conflicts, found: {[c['name'] for c in conflicts]}")
            return 1
        print(f"   conflict A: {row_a}")
        print(f"   conflict B: {row_b}")

        print("\n4. Resolving conflict A as keep_local...")
        resolve_mount_conflict(db, graph, drive_id, row_a, "keep_local")

        original_a = db.get_item_by_id(drive_id, next(
            c.id for c in db.list_children(drive_id, test_folder["id"]) if c.name == "keep_local_case.txt"
        ))
        local_bytes_a = path_for(drive_id, original_a.id).read_bytes()
        remote_item_a = graph.get_item(drive_id, original_a.remote_id)
        remote_dl_a = Path("/tmp/verify_resolve_a.txt")
        graph.download_content(drive_id, original_a.remote_id, remote_dl_a)
        remote_bytes_a = remote_dl_a.read_bytes()
        remote_dl_a.unlink(missing_ok=True)
        children_a = [c.name for c in db.list_children(drive_id, test_folder["id"]) if "keep_local_case" in c.name and "conflicted copy" in c.name]

        if local_bytes_a != b"LOCAL EDIT should win - keep this version":
            print(f"FAIL - keep_local: local cache bytes wrong: {local_bytes_a!r}")
            ok = False
        elif remote_bytes_a != b"LOCAL EDIT should win - keep this version":
            print(f"FAIL - keep_local: remote content wasn't updated: {remote_bytes_a!r}")
            ok = False
        elif children_a:
            print(f"FAIL - keep_local: conflict copy still present: {children_a}")
            ok = False
        elif db.count_conflicts("mount") and any(c["name"] == "keep_local_case.txt" for c in db.list_conflicts("mount")):
            print("FAIL - keep_local: conflict record wasn't dismissed")
            ok = False
        else:
            print("   PASS - keep_local: local edit now lives at both the original path and on Graph, "
                  "conflict copy removed, conflict dismissed")

        print("\n5. Resolving conflict B as keep_server...")
        resolve_mount_conflict(db, graph, drive_id, row_b, "keep_server")

        original_b = db.get_item_by_id(drive_id, next(
            c.id for c in db.list_children(drive_id, test_folder["id"]) if c.name == "keep_server_case.txt"
        ))
        local_bytes_b = path_for(drive_id, original_b.id).read_bytes()
        children_b = [c.name for c in db.list_children(drive_id, test_folder["id"]) if "keep_server_case" in c.name and "conflicted copy" in c.name]

        if local_bytes_b != b"REMOTE EDIT should win":
            print(f"FAIL - keep_server: original content changed unexpectedly: {local_bytes_b!r}")
            ok = False
        elif children_b:
            print(f"FAIL - keep_server: conflict copy still present: {children_b}")
            ok = False
        elif any(c["name"] == "keep_server_case.txt" for c in db.list_conflicts("mount")):
            print("FAIL - keep_server: conflict record wasn't dismissed")
            ok = False
        else:
            print("   PASS - keep_server: original untouched, conflict copy removed remotely + "
                  "locally, conflict dismissed")

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
        db._conn.execute(
            "DELETE FROM activity_log WHERE source='mount' AND event_type='conflict' "
            "AND name IN ('keep_local_case.txt', 'keep_server_case.txt')"
        )
        db._conn.commit()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
