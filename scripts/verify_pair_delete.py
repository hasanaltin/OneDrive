#!/usr/bin/env python3
"""Confirms deletes propagate in both directions.
Usage: python scripts/verify_pair_delete.py <local_dir> <remote_folder_item_id> [local|remote]
Defaults to testing local-delete-propagates-to-remote if no direction given.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onedrive.auth import AuthManager
from onedrive.db import Database
from onedrive.graph_client import GraphClient
from onedrive.logging_setup import setup_logging
from onedrive.sync.delta_worker import DeltaSyncWorker
from onedrive.sync.pair_worker import PairSyncWorker


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: verify_pair_delete.py <local_dir> <remote_folder_item_id> [local|remote]")
        return 1
    local_dir = Path(sys.argv[1]).expanduser().resolve()
    remote_item_id = sys.argv[2]
    direction = sys.argv[3] if len(sys.argv) > 3 else "local"
    local_dir.mkdir(parents=True, exist_ok=True)

    setup_logging()
    db = Database()
    auth = AuthManager()
    graph = GraphClient(auth)
    drive_id = db.get_sync_state("drive_id")
    if not drive_id:
        print("No drive_id cached - sign in via the app first.")
        return 1

    name = f"verify-delete-{int(time.time())}.txt"
    (local_dir / name).write_text("will be deleted")
    pair_id = db.create_pair(str(local_dir), drive_id, remote_item_id, "(verify script)")
    worker = PairSyncWorker(db, graph, drive_id)
    worker._sync_one_pair(pair_id)  # establish baseline (uploads it)

    pf = db.get_pair_file(pair_id, name)
    if pf is None:
        print("FAIL - baseline never established")
        return 1
    remote_id = pf.remote_item_id

    if direction == "local":
        print("Deleting locally...")
        (local_dir / name).unlink()
    else:
        print("Deleting remotely...")
        graph.delete_item(drive_id, remote_id)
        DeltaSyncWorker(db, graph, drive_id)._sync_once()

    worker._sync_one_pair(pair_id)

    if direction == "local":
        try:
            graph.get_item(drive_id, remote_id)
            ok = False
        except Exception:
            ok = True
        print(f"Remote item gone: {ok}")
    else:
        ok = not (local_dir / name).exists()
        print(f"Local file gone: {ok}")

    print("\nPASS" if ok else "\nFAIL")
    db.delete_pair(pair_id)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
