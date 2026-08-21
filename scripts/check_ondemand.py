#!/usr/bin/env python3
"""Proves content only downloads on open(), never just from listing.

Usage: python scripts/check_ondemand.py <relative_path_under_drive_root>
Example: python scripts/check_ondemand.py Documents/some_file.txt

Assumes the app is already running with the mount active at the default
mountpoint (~/OneDrive) and signed in (reads the same on-disk DB).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onedrive import constants
from onedrive.db import Database


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: check_ondemand.py <relative_path_under_drive_root>")
        return 1
    rel_path = sys.argv[1]
    db_path = "/" + rel_path if not rel_path.startswith("/") else rel_path

    db = Database()
    drive_id = db.get_sync_state("drive_id")
    if not drive_id:
        print("Not signed in yet (no drive_id in sync_state) - run the app and sign in first.")
        return 1

    item = db.get_item_by_path(drive_id, db_path)
    if item is None:
        print(f"No item cached at path {db_path!r} yet - has the initial crawl reached it?")
        return 1

    print(f"1. content_state before anything: {item.content_state!r} (expect 'none' or 'stale')")

    mount_path = constants.DEFAULT_MOUNTPOINT / rel_path
    parent_dir = mount_path.parent
    print(f"2. Listing {parent_dir} through the mount...")
    os.listdir(parent_dir)
    item = db.get_item_by_id(drive_id, item.id)
    print(f"   content_state after listing: {item.content_state!r} (expect UNCHANGED - listing must not download)")

    print(f"3. Opening {mount_path} through the mount (triggers download)...")
    with open(mount_path, "rb") as f:
        f.read(1)
    item = db.get_item_by_id(drive_id, item.id)
    print(f"   content_state after open+read: {item.content_state!r} (expect 'ready')")

    local_cache_path = constants.CONTENT_CACHE_DIR / drive_id / item.id[:2] / item.id
    print(f"4. Local cache file: {local_cache_path} exists={local_cache_path.exists()}")

    ok = item.content_state == "ready" and local_cache_path.exists()
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
