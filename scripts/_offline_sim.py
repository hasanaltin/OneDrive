"""Shared helpers for the verify_mount_*.py scripts.

Simulates "no network" by swapping GraphClient's requests.Session for one
whose every call raises ConnectionError - the same failure mode a real dead
connection produces, matching how the app itself was reproduced/fixed
earlier (see CHANGELOG 0.3.5/0.3.4). Also wraps OneDriveOperations' async
pyfuse3 handlers in trio.run() so verification scripts can drive them
directly, in-process, with no real kernel FUSE mount required - the plan
this implements calls this out explicitly: pyfuse3 Operations methods are
plain coroutines, so there's nothing kernel-specific about invoking them.
"""
import os

import requests
import trio

import pyfuse3


class _OfflineSession:
    def get(self, *a, **k):
        raise requests.exceptions.ConnectionError("offline (simulated by _offline_sim)")

    def request(self, *a, **k):
        raise requests.exceptions.ConnectionError("offline (simulated by _offline_sim)")

    def put(self, *a, **k):
        raise requests.exceptions.ConnectionError("offline (simulated by _offline_sim)")

    def delete(self, *a, **k):
        raise requests.exceptions.ConnectionError("offline (simulated by _offline_sim)")


def go_offline(graph_client) -> requests.Session:
    """Swaps in a session that always fails, returning the real one so the
    caller can restore it later via go_online()."""
    real_session = graph_client.session
    graph_client.session = _OfflineSession()
    return real_session


def go_online(graph_client, real_session: requests.Session) -> None:
    graph_client.session = real_session


class MountTestHarness:
    """Thin sync wrapper around OneDriveOperations for driving its async
    FUSE handlers one call at a time from a plain verification script."""

    def __init__(self, ops):
        self.ops = ops

    def mkdir(self, parent_inode: int, name: str):
        return trio.run(self.ops.mkdir, parent_inode, name, 0o755, None)

    def create(self, parent_inode: int, name: str, flags=os.O_CREAT | os.O_WRONLY):
        return trio.run(self.ops.create, parent_inode, name, 0o644, flags, None)

    def write(self, fh: int, offset: int, data: bytes) -> int:
        return trio.run(self.ops.write, fh, offset, data)

    def flush(self, fh: int) -> None:
        trio.run(self.ops.flush, fh)

    def release(self, fh: int) -> None:
        trio.run(self.ops.release, fh)

    def unlink(self, parent_inode: int, name: str) -> None:
        trio.run(self.ops.unlink, parent_inode, name, None)

    def rmdir(self, parent_inode: int, name: str) -> None:
        trio.run(self.ops.rmdir, parent_inode, name, None)

    def rename(self, parent_inode_old: int, name_old: str, parent_inode_new: int, name_new: str) -> None:
        trio.run(self.ops.rename, parent_inode_old, name_old, parent_inode_new, name_new, 0, None)

    def getattr(self, inode: int) -> pyfuse3.EntryAttributes:
        return trio.run(self.ops.getattr, inode, None)

    def write_new_file(self, parent_inode: int, name: str, data: bytes) -> str:
        """create() + write() + flush() + release() in one call, returning
        the new item's id - the common case for setting up test fixtures."""
        fh_info, attrs = self.create(parent_inode, name)
        fh = fh_info.fh
        if data:
            self.write(fh, 0, data)
        self.flush(fh)
        self.release(fh)
        return self.ops._inode_to_id[attrs.st_ino]
