#!/usr/bin/env python3
"""Confirms a local-only new file gets uploaded to the paired remote folder.
Usage: python scripts/verify_pair_upload.py <local_dir> <remote_folder_item_id>
(remote_folder_item_id: use the id shown when adding a pair in the GUI, or
any folder id already cached in the DB - see scripts/verify_auth.py's output
or the app's Folder Pairs tab.)
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onedrive.auth import AuthManager
from onedrive.db import Database
from onedrive.graph_client import GraphClient
from onedrive.logging_setup import setup_logging
from onedrive.sync.pair_worker import PairSyncWorker


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: verify_pair_upload.py <local_dir> <remote_folder_item_id>")
        return 1
    local_dir = Path(sys.argv[1]).expanduser().resolve()
    remote_item_id = sys.argv[2]
    local_dir.mkdir(parents=True, exist_ok=True)

    setup_logging()
    db = Database()
    auth = AuthManager()
    graph = GraphClient(auth)
    drive_id = db.get_sync_state("drive_id")
    if not drive_id:
        print("No drive_id cached - sign in via the app first.")
        return 1

    pair_id = db.create_pair(str(local_dir), drive_id, remote_item_id, "(verify script)")
    worker = PairSyncWorker(db, graph, drive_id)

    test_name = f"verify-upload-{int(time.time())}.txt"
    (local_dir / test_name).write_text("hello from verify_pair_upload")
    print(f"Wrote local file: {local_dir / test_name}")

    worker._sync_one_pair(pair_id)

    pf = db.get_pair_file(pair_id, test_name)
    if pf is None or pf.remote_item_id is None:
        print("FAIL - file was not tracked/uploaded")
        db.delete_pair(pair_id)
        return 1

    remote_item = graph.get_item(drive_id, pf.remote_item_id)
    print(f"Remote item: {remote_item['name']}, size={remote_item.get('size')}")
    ok = remote_item.get("size") == (local_dir / test_name).stat().st_size
    print("\nPASS" if ok else "\nFAIL")

    graph.delete_item(drive_id, pf.remote_item_id)
    db.delete_pair(pair_id)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
