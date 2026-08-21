import errno
import logging
import os
import subprocess
import threading
from pathlib import Path

from onedrive import constants  # also resolves FUSE_LIBRARY_PATH as an import-time side effect
import pyfuse3
import trio

logger = logging.getLogger(__name__)

# Set by stop_mount() right before it asks the kernel to tear the mount
# down, so _run()'s own exit path (below) can tell "this is the expected
# result of an unmount we ourselves just requested" apart from "the FUSE
# session just silently ended for some other reason." Both look identical
# to pyfuse3 itself - trio.run(pyfuse3.main) simply returns, no exception
# either way - and telling them apart is the whole point of this
# bookkeeping: an unexplained silent disconnect (mountpoint left showing
# "Transport endpoint is not connected", or just quietly unmounted, with
# absolutely nothing else logged anywhere) has happened repeatedly with no
# other diagnostic evidence to explain why.
_unmount_requested: dict[str, bool] = {}


def start_mount(operations, mountpoint: Path) -> threading.Thread:
    mountpoint.mkdir(parents=True, exist_ok=True)

    fuse_options = set(pyfuse3.default_options)
    fuse_options.add(f"fsname={constants.APP_NAME}")
    key = str(mountpoint)
    _unmount_requested[key] = False

    def _run():
        try:
            pyfuse3.init(operations, str(mountpoint), fuse_options)
            logger.info("FUSE session for %s started", mountpoint)
            try:
                trio.run(pyfuse3.main)
            except BaseException:
                pyfuse3.close(unmount=False)
                raise
            if _unmount_requested.get(key):
                logger.info("FUSE session for %s ended (unmount was requested)", mountpoint)
            else:
                logger.warning(
                    "FUSE session for %s ended on its own - pyfuse3.main() returned with no "
                    "exception and no unmount was requested through this app. The kernel side "
                    "almost certainly closed the connection out from under us; nothing in this "
                    "process's own code asked for that.",
                    mountpoint,
                )
            pyfuse3.close()
        except Exception:
            logger.exception("FUSE mount at %s exited with an error", mountpoint)
        finally:
            _unmount_requested.pop(key, None)

    t = threading.Thread(target=_run, name="FUSEMountThread", daemon=True)
    t.start()
    return t


def stop_mount(mountpoint: Path) -> None:
    _unmount_requested[str(mountpoint)] = True
    result = subprocess.run(
        ["fusermount3", "-u", str(mountpoint)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    logger.warning(
        "fusermount3 -u %s exited %s: %s - retrying with lazy unmount",
        mountpoint, result.returncode, result.stderr.strip(),
    )
    # Graceful unmount fails with EBUSY if something (e.g. Dolphin/gwenview)
    # still has a file open under the mount. Lazy unmount (-z) detaches it
    # immediately regardless - any lingering handles just get ENOTCONN on
    # their next call, which beats leaving a stale/dead mount behind (the
    # kernel keeps a mount registered with no process serving it, so every
    # access hangs with "Transport endpoint is not connected").
    result2 = subprocess.run(
        ["fusermount3", "-uz", str(mountpoint)],
        capture_output=True,
        text=True,
    )
    if result2.returncode != 0:
        logger.error(
            "lazy unmount of %s also failed: %s", mountpoint, result2.stderr.strip()
        )


def is_mounted(mountpoint: Path) -> bool:
    try:
        with open("/proc/mounts") as f:
            target = str(mountpoint)
            return any(line.split()[1] == target for line in f if len(line.split()) > 1)
    except OSError:
        return False


def recover_stale_mount(mountpoint: Path) -> bool:
    """Root cause finally found for a mount that kept showing "Transport
    endpoint is not connected" with absolutely nothing useful ever logged
    anywhere, no matter how much diagnostic tooling got thrown at it: it
    wasn't a pyfuse3/kernel crash at all. Killing this process with a bare
    SIGTERM (systemctl stop/restart does exactly this, with no chance for
    this app's own graceful-shutdown code to run) ends the FUSE session
    without ever calling fusermount3 - the kernel has no way to know the
    server is gone, so it just leaves the mount table entry in place and
    every access to it starts failing with ENOTCONN. is_mounted() alone
    can't tell this apart from a genuinely healthy mount (both simply show
    up in /proc/mounts), which is exactly why _maybe_auto_mount() kept
    concluding "already mounted, nothing to do" at the next startup and
    leaving it broken indefinitely - only a manual `fusermount3 -uz` ever
    actually fixed it. Call this before trusting is_mounted() at startup:
    if the mountpoint is listed but unresponsive, force-unmounts it (safe
    even if nothing is actually wrong - fusermount3 on a mountpoint that
    isn't mounted at all is a no-op) so a fresh mount can take its place.
    Returns True if a stale mount was found and cleaned up."""
    if not is_mounted(mountpoint):
        return False
    try:
        os.stat(mountpoint)
        return False  # responsive - genuinely fine, not stale
    except OSError as e:
        if e.errno != errno.ENOTCONN:
            return False
    logger.warning(
        "mountpoint %s is a stale FUSE mount (ENOTCONN, nothing actually serving it) - "
        "cleaning it up so a fresh mount can take its place",
        mountpoint,
    )
    subprocess.run(["fusermount3", "-uz", str(mountpoint)], capture_output=True)
    return True
