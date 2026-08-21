#!/usr/bin/env python3
"""Verifies the Conflicts-dialog resolution actions (keep_local / keep_server
/ dismiss) end-to-end against real Graph calls, reusing
verify_pair_conflict.py's approach to manufacture a genuine both-sides-changed
conflict. Uses an isolated temp local dir + temp remote subfolder so it never
touches the user's real folder pairs.

Usage: python scripts/verify_conflict_resolution.py <remote_parent_item_id>
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onedrive.auth import AuthManager
from onedrive.db import Database
from onedrive.graph_client import GraphClient
from onedrive.logging_setup import setup_logging
from onedrive.sync.conflict_actions import resolve_pair_conflict
from onedrive.sync.delta_worker import DeltaSyncWorker
from onedrive.sync.pair_worker import PairSyncWorker


def make_conflict(db, graph, worker, pair_id, local_dir, tag):
    name = f"verify-resolve-{tag}-{int(time.time())}.txt"
    (local_dir / name).write_bytes(b"original synced content")
    worker._sync_one_pair(pair_id)
    pf = db.get_pair_file(pair_id, name)

    (local_dir / name).write_bytes(f"LOCAL EDIT {tag}".encode())
    result = graph.replace_small(db.get_sync_state("drive_id"), pf.remote_item_id, f"REMOTE EDIT {tag}".encode())
    db.upsert_item(db.get_sync_state("drive_id"), result)

    worker._sync_one_pair(pair_id)

    conflicts = [c for c in db.list_conflicts(f"pair:{pair_id}") if c["name"] == name]
    assert len(conflicts) == 1, f"expected exactly 1 fresh conflict for {name}, got {conflicts}"
    return conflicts[0]


def item_exists(graph, drive_id, item_id):
    try:
        graph.get_item(drive_id, item_id)
        return True
    except Exception:
        return False


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: verify_conflict_resolution.py <remote_parent_item_id>")
        return 1
    remote_item_id = sys.argv[1]

    setup_logging()
    db = Database()
    auth = AuthManager()
    graph = GraphClient(auth)
    drive_id = db.get_sync_state("drive_id")
    if not drive_id:
        print("No drive_id cached - sign in via the app first.")
        return 1

    DeltaSyncWorker(db, graph, drive_id)._sync_once()  # pick up a just-created remote folder

    local_dir = Path(tempfile.mkdtemp(prefix="verify-conflict-resolution-"))
    pair_id = db.create_pair(str(local_dir), drive_id, remote_item_id, "(verify script)")
    worker = PairSyncWorker(db, graph, drive_id)

    ok = True
    try:
        # --- dismiss: both files untouched, just drops off the review list --
        c = make_conflict(db, graph, worker, pair_id, local_dir, "dismiss")
        conflict_abs = local_dir / c["path"]
        original_abs = local_dir / c["name"]
        assert conflict_abs.exists() and original_abs.exists()
        resolve_pair_conflict(db, graph, db.get_pair(pair_id), c, "dismiss")
        still_listed = any(x["id"] == c["id"] for x in db.list_conflicts(f"pair:{pair_id}"))
        if still_listed or not conflict_abs.exists() or not original_abs.exists():
            print("FAIL dismiss: expected both files untouched and conflict off the review list")
            ok = False
        else:
            print("PASS dismiss")

        # --- keep_server: conflict copy gone (local + remote), original untouched --
        c = make_conflict(db, graph, worker, pair_id, local_dir, "keepsrv")
        conflict_abs = local_dir / c["path"]
        original_abs = local_dir / c["name"]
        conflict_pf = db.get_pair_file(pair_id, c["path"])
        original_content_before = original_abs.read_bytes()
        resolve_pair_conflict(db, graph, db.get_pair(pair_id), c, "keep_server")
        remote_gone = not item_exists(graph, drive_id, conflict_pf.remote_item_id)
        if (conflict_abs.exists() or not original_abs.exists()
                or original_abs.read_bytes() != original_content_before or not remote_gone):
            print("FAIL keep_server: expected conflict copy gone (local+remote), original untouched")
            ok = False
        else:
            print("PASS keep_server")

        # --- keep_local: original now holds the local edit, copy gone (local + remote) --
        c = make_conflict(db, graph, worker, pair_id, local_dir, "keeploc")
        conflict_abs = local_dir / c["path"]
        original_abs = local_dir / c["name"]
        conflict_pf = db.get_pair_file(pair_id, c["path"])
        local_edit_bytes = conflict_abs.read_bytes()
        resolve_pair_conflict(db, graph, db.get_pair(pair_id), c, "keep_local")
        remote_gone = not item_exists(graph, drive_id, conflict_pf.remote_item_id)
        original_pf = db.get_pair_file(pair_id, c["name"])
        remote_matches = False
        if original_pf is not None:
            remote_item = graph.get_item(drive_id, original_pf.remote_item_id)
            remote_matches = remote_item.get("size") == len(local_edit_bytes)
        if (conflict_abs.exists() or not original_abs.exists()
                or original_abs.read_bytes() != local_edit_bytes or not remote_gone or not remote_matches):
            print("FAIL keep_local: expected original to hold the local edit, copy gone (local+remote)")
            ok = False
        else:
            print("PASS keep_local")

    finally:
        for pf in db.list_pair_files(pair_id):
            if pf.remote_item_id:
                try:
                    graph.delete_item(drive_id, pf.remote_item_id)
                except Exception:
                    pass
        db.delete_pair(pair_id)
        shutil.rmtree(local_dir, ignore_errors=True)

    print("\nPASS - all conflict resolution actions verified" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
