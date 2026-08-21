#!/usr/bin/env python3
"""The important one: reproduces a genuine both-sides-changed conflict and
proves neither version is silently lost.
Usage: python scripts/verify_pair_conflict.py <local_dir> <remote_folder_item_id> [--interactive]

Default mode edits the remote copy directly via Graph. --interactive instead
pauses so you can edit it yourself via the OneDrive web UI (closer to a real
scenario), then press Enter to continue.
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
        print("Usage: verify_pair_conflict.py <local_dir> <remote_folder_item_id> [--interactive]")
        return 1
    local_dir = Path(sys.argv[1]).expanduser().resolve()
    remote_item_id = sys.argv[2]
    interactive = "--interactive" in sys.argv
    local_dir.mkdir(parents=True, exist_ok=True)

    setup_logging()
    db = Database()
    auth = AuthManager()
    graph = GraphClient(auth)
    drive_id = db.get_sync_state("drive_id")
    if not drive_id:
        print("No drive_id cached - sign in via the app first.")
        return 1

    name = f"verify-conflict-{int(time.time())}.txt"
    original_content = b"original synced content"
    (local_dir / name).write_bytes(original_content)

    pair_id = db.create_pair(str(local_dir), drive_id, remote_item_id, "(verify script)")
    worker = PairSyncWorker(db, graph, drive_id)
    worker._sync_one_pair(pair_id)  # establish clean baseline
    pf = db.get_pair_file(pair_id, name)
    print(f"Baseline established: etag={pf.last_synced_etag}")

    print("\n1. Editing local copy...")
    local_edit = b"MY LOCAL EDIT - should survive"
    (local_dir / name).write_bytes(local_edit)

    if interactive:
        print(f"\n2. Now edit '{name}' via the OneDrive web UI (in folder {remote_item_id}).")
        input("   Press Enter once you've saved your change there...")
        DeltaSyncWorker(db, graph, drive_id)._sync_once()
    else:
        print("2. Editing remote copy directly via Graph...")
        remote_edit = b"REMOTE EDIT - should also survive"
        result = graph.replace_small(drive_id, pf.remote_item_id, remote_edit)
        db.upsert_item(drive_id, result)

    print("\n3. Running one reconciliation pass...")
    worker._sync_one_pair(pair_id)

    files = sorted(f.name for f in local_dir.iterdir())
    conflict_files = [f for f in files if "conflicted copy" in f]
    print(f"\nFiles now in {local_dir}: {files}")

    ok = True
    if name not in files:
        print(f"FAIL - original filename {name} missing")
        ok = False
    if len(conflict_files) != 1:
        print(f"FAIL - expected exactly 1 conflict copy, found {len(conflict_files)}")
        ok = False
    else:
        conflict_content = (local_dir / conflict_files[0]).read_bytes()
        if conflict_content != local_edit:
            print("FAIL - conflict copy doesn't contain the local edit")
            ok = False
        else:
            print(f"Conflict copy correctly preserves local edit: {conflict_files[0]}")

    pair_after = db.get_pair(pair_id)
    print(f"conflict_count: {pair_after.conflict_count}")

    print("\nPASS - both versions preserved" if ok else "\nFAIL")

    # cleanup
    for pf2 in db.list_pair_files(pair_id):
        if pf2.remote_item_id:
            try:
                graph.delete_item(drive_id, pf2.remote_item_id)
            except Exception:
                pass
    db.delete_pair(pair_id)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
