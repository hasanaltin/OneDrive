#!/usr/bin/env python3
"""Pure-DB regression test for the single most important fix in the v4
offline-mount design: once MountSyncWorker confirms a "pending:<uuid>" row
via confirm_synced_item(), the item's real Graph id becomes known
(items.remote_id). The very next time it's seen through a normal channel -
DeltaSyncWorker's next /delta poll, in practice - that channel calls
upsert_item() with the item's REAL id, not "pending:<uuid>". Without the
remote_id-fallback merge in upsert_item(), that call can't find the
existing row (its primary lookup is keyed on id) and inserts a second one
for the same logical file, colliding with idx_items_path's unique index and
crashing every subsequent delta pass.

Uses an in-memory SQLite DB - no real files, no network, no risk to any
real data. Usage: python scripts/verify_mount_remote_id_merge.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onedrive.db import Database  # noqa: E402


def main() -> int:
    db = Database(":memory:")
    drive_id = "drive1"

    # Seed a root folder (parent_id lookups need something to resolve against).
    db._conn.execute(
        "INSERT INTO items (drive_id, id, remote_id, parent_id, name, path, is_folder, "
        "size, content_state, deleted, last_synced_at) "
        "VALUES (?, 'root', 'root', NULL, '', '', 1, 0, 'ready', 0, '2026-01-01')",
        (drive_id,),
    )
    db._conn.commit()

    ok = True

    # --- Step 1: create() would insert a pending row like this ---
    pending_id = "pending:11111111-1111-1111-1111-111111111111"
    db.insert_pending_item(drive_id, pending_id, "root", "report.docx", is_folder=False)
    row = db.get_item_by_id(drive_id, pending_id)
    if row is None or row.remote_id is not None:
        print(f"FAIL - expected a pending row with remote_id=None, got {row}")
        return 1
    print(f"1. Inserted pending row: id={row.id} remote_id={row.remote_id} path={row.path}")

    # --- Step 2: MountSyncWorker's own confirmation path ---
    real_id = "AAAABBBBCCCC123"
    graph_item_from_create = {
        "id": real_id,
        "eTag": "etag-v1",
        "cTag": "ctag-v1",
        "size": 4096,
        "fileSystemInfo": {
            "lastModifiedDateTime": "2026-01-02T00:00:00Z",
            "createdDateTime": "2026-01-02T00:00:00Z",
        },
    }
    db.confirm_synced_item(drive_id, pending_id, graph_item_from_create)
    row = db.get_item_by_id(drive_id, pending_id)
    if row is None or row.remote_id != real_id:
        print(f"FAIL - confirm_synced_item didn't set remote_id, got {row}")
        return 1
    all_rows_after_confirm = db._conn.execute(
        "SELECT id FROM items WHERE drive_id=?", (drive_id,)
    ).fetchall()
    print(f"2. confirm_synced_item merged in-place: id={row.id} remote_id={row.remote_id} "
          f"(total rows in items: {len(all_rows_after_confirm)})")
    if len(all_rows_after_confirm) != 2:  # root + this one item
        print(f"FAIL - expected exactly 2 rows (root + item), found {len(all_rows_after_confirm)}")
        ok = False

    # --- Step 3: the scenario upsert_item's fallback exists to protect -
    # a later poller (DeltaSyncWorker) seeing the SAME item under its real
    # id, with no idea a "pending:" row for it ever existed.
    graph_item_from_delta = {
        "id": real_id,
        "name": "report.docx",
        "parentReference": {"id": "root"},
        "file": {},
        "eTag": "etag-v2",
        "cTag": "ctag-v2",
        "size": 4200,
        "fileSystemInfo": {
            "lastModifiedDateTime": "2026-01-02T00:05:00Z",
            "createdDateTime": "2026-01-02T00:00:00Z",
        },
    }
    db.upsert_item(drive_id, graph_item_from_delta)
    all_rows_after_upsert = db._conn.execute(
        "SELECT id, remote_id, path FROM items WHERE drive_id=?", (drive_id,)
    ).fetchall()
    print(f"3. upsert_item (simulating a delta re-poll) - rows now: "
          f"{[(r['id'], r['remote_id'], r['path']) for r in all_rows_after_upsert]}")

    if len(all_rows_after_upsert) != 2:
        print(f"FAIL - upsert_item created a duplicate row! {len(all_rows_after_upsert)} rows found, expected 2")
        ok = False
    else:
        item_row = db.get_item_by_id(drive_id, pending_id)
        if item_row is None:
            print("FAIL - the pending_id row vanished instead of being merged into")
            ok = False
        elif item_row.etag != "etag-v2":
            print(f"FAIL - upsert_item's fresh etag wasn't merged in, got {item_row.etag}")
            ok = False
        else:
            print(f"   merged correctly: id stayed {item_row.id}, etag refreshed to {item_row.etag}")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
