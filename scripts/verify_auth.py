#!/usr/bin/env python3
"""CLI-only smoke test for authentication - no GUI, no FUSE.
Run with: python scripts/verify_auth.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onedrive.auth import AuthManager
from onedrive.graph_client import GraphClient
from onedrive.logging_setup import setup_logging


def main() -> int:
    setup_logging()
    auth = AuthManager()

    if not auth.is_signed_in:
        print("Not signed in. Starting device code flow...")
        flow = auth.start_device_flow()
        print(f"\nGo to: {flow['verification_uri']}")
        print(f"Enter code: {flow['user_code']}\n")
        print("Waiting for you to complete sign-in...")
        auth.complete_device_flow(flow)
    else:
        print(f"Already signed in as {auth.account_username}")

    graph = GraphClient(auth)
    drive = graph.get_drive()
    print("\nSign-in verified. Drive info:")
    print(f"  driveId: {drive.get('id')}")
    print(f"  owner:   {(drive.get('owner') or {}).get('user', {}).get('displayName')}")
    quota = drive.get("quota", {})
    total = quota.get("total", 0) / (1024**3)
    remaining = quota.get("remaining", 0) / (1024**3)
    print(f"  quota:   {remaining:.1f} GB free of {total:.1f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
