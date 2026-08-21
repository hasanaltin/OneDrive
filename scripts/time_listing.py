#!/usr/bin/env python3
"""Times a directory listing through the mount. Run once shortly after mount
(while the initial crawl is still in progress) and again once the GUI status
shows 'Idle', to confirm listings are instant once the metadata cache is warm.

Usage: python scripts/time_listing.py /path/under/the/mount
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: time_listing.py <path-under-mount>")
        return 1
    target = sys.argv[1]

    import os

    t0 = time.perf_counter()
    try:
        entries = os.listdir(target)
    except OSError as e:
        elapsed = time.perf_counter() - t0
        print(f"listdir failed after {elapsed*1000:.1f} ms: {e}")
        return 1
    elapsed = time.perf_counter() - t0
    print(f"{target}: {len(entries)} entries in {elapsed*1000:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
