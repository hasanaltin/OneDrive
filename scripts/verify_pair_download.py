#!/usr/bin/env python3
"""Confirms a remote-only new file gets downloaded into the paired local folder.
Usage: python scripts/verify_pair_download.py <local_dir> <remote_folder_item_id>
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
        print("Usage: verify_pair_download.py <local_dir> <remote_folder_item_id>")
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

    test_name = f"verify-download-{int(time.time())}.txt"
    content = b"hello from verify_pair_download"
    created = graph.upload_small(drive_id, remote_item_id, test_name, content)
    print(f"Created remote file: {created['id']}")

    # give DeltaSyncWorker (in the real app) or this script's own poll a
    # moment - if the app isn't running to keep the cache fresh, force one
    # incremental delta pass here directly.
    from onedrive.sync.delta_worker import DeltaSyncWorker

    delta = DeltaSyncWorker(db, graph, drive_id)
    delta._sync_once()

    pair_id = db.create_pair(str(local_dir), drive_id, remote_item_id, "(verify script)")
    worker = PairSyncWorker(db, graph, drive_id)
    worker._sync_one_pair(pair_id)

    local_path = local_dir / test_name
    ok = local_path.exists() and local_path.read_bytes() == content
    print(f"Local file exists: {local_path.exists()}, content matches: {ok}")
    print("\nPASS" if ok else "\nFAIL")

    graph.delete_item(drive_id, created["id"])
    db.delete_pair(pair_id)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
