import fcntl
import logging
import subprocess

from onedrive import constants

logger = logging.getLogger(__name__)

_LOCK_PATH = constants.RUNTIME_DIR / f"{constants.APP_NAME}.lock"

# Kept open (module-level, never closed explicitly) for the whole process
# lifetime - the OS releases the flock automatically the instant this
# process exits, however it exits (clean quit, crash, kill -9), so there's
# no cleanup code needed anywhere else and no stale-lock-file problem the
# way a plain "does this PID file exist" check would have.
_lock_file = None


def acquire() -> bool:
    """True if this process is the only instance. False means another one
    is already running - reproduced directly: the app has no autostart-vs-
    manual-launch coordination at all, so clicking its new Applications
    menu entry while the autostart copy was already running silently
    started a second full process - a second set of background sync
    workers hammering the same account, and a second overlay-icon socket
    server that stole the first one's socket file out from under it,
    leaving the original instance's Dolphin integration silently dead
    until restarted."""
    global _lock_file
    constants.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        _lock_file.close()
        _lock_file = None
        return False


def open_existing_instance_folder() -> None:
    """Called once we've determined this launch is a duplicate - opens
    whatever mountpoint the already-running instance is using. This is
    what a user actually wants from clicking the app's icon a second time:
    their files, not a second copy of the app or its settings window."""
    from onedrive.db import Database

    db = Database()
    mountpoint = db.get_sync_state("last_mountpoint") or str(constants.DEFAULT_MOUNTPOINT)
    logger.info("another instance is already running - opening %s instead of starting a second copy", mountpoint)
    try:
        subprocess.Popen(["xdg-open", mountpoint])
    except OSError:
        logger.exception("couldn't open %s", mountpoint)
