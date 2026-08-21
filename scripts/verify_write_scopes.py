#!/usr/bin/env python3
"""Proves the signed-in token actually carries write permission, by creating
then deleting a throwaway remote folder - independent of anything else.
Run with: python scripts/verify_write_scopes.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onedrive.auth import AuthManager
from onedrive.graph_client import GraphClient
from onedrive.logging_setup import setup_logging


def main() -> int:
    setup_logging()
    auth = AuthManager()
    if not auth.is_signed_in:
        print("Not signed in - run scripts/verify_auth.py first.")
        return 1

    graph = GraphClient(auth)
    drive = graph.get_drive()
    drive_id = drive["id"]
    root = graph._get(f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root").json()

    name = f"onedrive-scope-check-{int(time.time())}"
    print(f"Creating throwaway folder '{name}' at drive root...")
    created = graph.create_folder(drive_id, root["id"], name)
    print(f"  created: id={created['id']}")

    print("Deleting it again...")
    graph.delete_item(drive_id, created["id"])
    print("  deleted.")

    print("\nPASS - token has write access.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
