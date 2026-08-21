#!/usr/bin/env python3
"""Verifies the hardened bootstrap heuristic (reconcile.py's
_bootstrap_trusted + pair_worker._attach_bootstrap_hashes): on a pair's
first-ever sync pass, two same-size files with IDENTICAL content should
still be trusted as already-synced with no upload/download, but two
same-size files with DIFFERENT content - the exact false-positive the old
size-only heuristic could never catch - must now be flagged and resolved
as a genuine conflict instead of silently treated as already in sync.

Runs against the real signed-in account, exercising the real PairSyncWorker
path end to end (not just reconcile_pair() in isolation).
Usage: python scripts/verify_pair_bootstrap_hash.py
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onedrive.auth import AuthManager  # noqa: E402
from onedrive.db import Database  # noqa: E402
from onedrive.graph_client import GraphClient  # noqa: E402
from onedrive.sync.pair_worker import PairSyncWorker  # noqa: E402


def main() -> int:
    db = Database()
    auth = AuthManager()
    graph = GraphClient(auth)
    drive_id = db.get_sync_state("drive_id")
    if not drive_id:
        print("No drive_id cached - sign in via the app first.")
        return 1
    account_root = db.get_item_by_path(drive_id, "")
    if account_root is None:
        print("No root item cached - let DeltaSyncWorker finish an initial crawl first.")
        return 1

    test_name = f"verify-bootstrap-hash-{int(time.time())}"
    local_dir = Path(tempfile.mkdtemp(prefix="verify_bootstrap_hash_"))
    print(f"Local dir: {local_dir}")
    print(f"Creating throwaway remote test folder: {test_name}")
    test_folder = graph.create_folder(drive_id, account_root.id, test_name)
    db.upsert_item(drive_id, test_folder)

    ok = True
    pair_id = None
    try:
        # Same size, same content - must still be trusted as already-synced.
        same_bytes = b"X" * 500
        (local_dir / "identical.bin").write_bytes(same_bytes)
        remote_identical = graph.upload_file(
            drive_id, test_folder["id"], "identical.bin", _tmp(same_bytes), existing_item_id=None,
        )
        db.upsert_item(drive_id, remote_identical)

        # Same size, DIFFERENT content - the exact false-positive case the
        # hardening exists to catch. Old size-only heuristic would have
        # silently trusted this and never noticed the divergence.
        local_bytes = b"L" * 500
        remote_bytes = b"R" * 500
        (local_dir / "diverged.bin").write_bytes(local_bytes)
        remote_diverged = graph.upload_file(
            drive_id, test_folder["id"], "diverged.bin", _tmp(remote_bytes), existing_item_id=None,
        )
        db.upsert_item(drive_id, remote_diverged)

        pair_id = db.create_pair(str(local_dir), drive_id, test_folder["id"], f"/{test_name}")
        worker = PairSyncWorker(db, graph, drive_id)

        print("\nRunning bootstrap pass (PairSyncWorker._sync_one_pair)...")
        worker._sync_one_pair(pair_id)

        identical_pf = db.get_pair_file(pair_id, "identical.bin")
        conflicts = db.list_conflicts(f"pair:{pair_id}")
        diverged_conflict = next((c for c in conflicts if c["name"] == "diverged.bin"), None)

        if identical_pf is None:
            print("FAIL - identical.bin got no synced baseline at all")
            ok = False
        elif remote_identical.get("eTag") and identical_pf.last_synced_etag not in (None, remote_identical["eTag"]):
            print(f"FAIL - identical.bin baseline etag mismatch: {identical_pf.last_synced_etag!r}")
            ok = False
        else:
            print("PASS - identical.bin (same size, same content) correctly trusted, no upload/download")

        if diverged_conflict is None:
            print("FAIL - diverged.bin (same size, DIFFERENT content) was NOT flagged as a conflict - "
                  "this is exactly the false-positive the hardening was meant to fix")
            ok = False
        else:
            print(f"PASS - diverged.bin (same size, different content) correctly caught as a conflict: "
                  f"{diverged_conflict}")
            # Confirm both versions actually survived (keep-both), not that
            # one silently clobbered the other.
            children = {c.name for c in db.list_children(drive_id, test_folder["id"])}
            has_conflict_copy = any("conflicted copy" in n for n in children)
            if not has_conflict_copy:
                print(f"FAIL - no conflicted-copy file found remotely among: {children}")
                ok = False
            else:
                print("PASS - a conflicted-copy file exists remotely, both versions preserved")

        print("\nPASS" if ok else "\nFAIL")
    finally:
        print(f"\nCleaning up remote test folder {test_name} and pair...")
        try:
            graph.delete_item(drive_id, test_folder["id"])
        except Exception as e:
            print(f"   (cleanup warning: {e})")
        if pair_id is not None:
            db.delete_pair(pair_id)
        db._conn.execute("DELETE FROM items WHERE drive_id=? AND path LIKE ?", (drive_id, f"/{test_name}%"))
        db._conn.execute(
            "DELETE FROM activity_log WHERE source=? AND event_type='conflict'", (f"pair:{pair_id}",)
        )
        db._conn.commit()
        shutil.rmtree(local_dir, ignore_errors=True)

    return 0 if ok else 1


def _tmp(data: bytes) -> Path:
    p = Path(tempfile.mkstemp(prefix="verify_bootstrap_hash_src_")[1])
    p.write_bytes(data)
    return p


if __name__ == "__main__":
    sys.exit(main())
