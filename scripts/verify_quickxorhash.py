#!/usr/bin/env python3
"""Empirically verifies onedrive/quickxorhash.py against real Graph-reported
quickXorHash values before it's trusted anywhere in reconcile.py. Picks a
handful of already-cached files with a known hash, recomputes it locally,
and compares byte-for-byte.

Usage: python scripts/verify_quickxorhash.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onedrive.content_cache import path_for  # noqa: E402
from onedrive.db import Database  # noqa: E402
from onedrive.quickxorhash import quickxorhash_base64  # noqa: E402


def main() -> int:
    db = Database()
    drive_id = db.get_sync_state("drive_id")
    if not drive_id:
        print("No drive_id cached - sign in via the app first.")
        return 1

    rows = db._conn.execute(
        "SELECT id, path, size, quickxor_hash, content_state FROM items "
        "WHERE drive_id=? AND is_folder=0 AND quickxor_hash IS NOT NULL AND content_state='ready' "
        "AND deleted=0 ORDER BY size ASC LIMIT 2000",
        (drive_id,),
    ).fetchall()
    print(f"Found {len(rows)} cached files with a known quickxor_hash to sample from.")

    # Sample a spread of sizes (including a couple of larger ones) rather
    # than just whatever's smallest, to exercise the bit-position wraparound
    # logic across more than one 160-byte ring cycle.
    candidates = []
    seen_buckets = set()
    for r in rows:
        local_path = path_for(drive_id, r["id"])
        if not local_path.exists() or local_path.stat().st_size != r["size"]:
            continue
        # log2-ish bucket so small (<160 byte, single ring pass) and large
        # (multi-ring-cycle) files both get exercised, not just whatever's
        # smallest.
        bucket = r["size"].bit_length()
        if bucket in seen_buckets:
            continue
        seen_buckets.add(bucket)
        candidates.append(r)
        if len(candidates) >= 15:
            break

    if not candidates:
        print("No locally-cached files with a verified-fresh local copy found - "
              "open a few files through the mount first, then retry.")
        return 1

    ok = True
    for r in candidates:
        local_path = path_for(drive_id, r["id"])
        data = local_path.read_bytes()
        computed = quickxorhash_base64(data)
        expected = r["quickxor_hash"]
        match = computed == expected
        ok = ok and match
        print(f"{'OK  ' if match else 'FAIL'} size={r['size']:>10} path={r['path']}")
        if not match:
            print(f"       expected: {expected}")
            print(f"       computed: {computed}")

    print("\nPASS - implementation matches Graph's real hashes" if ok else "\nFAIL - implementation does NOT match")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
