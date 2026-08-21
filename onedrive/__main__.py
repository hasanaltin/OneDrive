import faulthandler
import logging
import os
import signal
import sys

from onedrive import constants
from onedrive.logging_setup import setup_logging

logger = logging.getLogger(__name__)

# Kept open for the process's whole lifetime (faulthandler needs a real,
# still-open file object, not a path) - a separate file from the normal
# app log since a native crash (SIGSEGV etc., e.g. inside pyfuse3's C
# extension) can happen at a point where Python's own logging machinery
# isn't safely usable anymore. Investigating a mount that silently
# disappears with nothing at all in the ordinary logs - this is here so
# that IF it turns out to be a native-level crash rather than a Python
# exception (which the code around start_mount() already catches and logs
# on its own), there's at least a chance of a signal + traceback landing
# here instead of leaving zero evidence either way.
constants.ensure_dirs()
_faulthandler_file = open(constants.DATA_DIR / "faulthandler.log", "a")
faulthandler.enable(file=_faulthandler_file)


def _install_graceful_shutdown(window) -> None:
    """`systemctl stop`/`restart` sends a bare SIGTERM, which Python
    doesn't otherwise intercept - the process just dies immediately,
    skipping MainWindow._quit_app()'s cleanup (stop_mount() in particular)
    entirely. That leaves a stale FUSE mount table entry that outlives the
    process (see fuse/mount.py's recover_stale_mount, the self-healing
    side of this same problem): the kernel has no way to know the server
    is gone, so it keeps listing the mountpoint as mounted, and every
    access to it starts failing with "Transport endpoint is not
    connected" until something notices and force-unmounts it. This is
    what was actually behind a mount that kept going silently unusable
    with nothing useful ever logged anywhere - not a pyfuse3 or kernel
    bug, just a bare SIGTERM giving this app's own cleanup no chance to
    run. Handling SIGTERM here means a normal restart/stop doesn't create
    that stale state in the first place.

    Python only checks for pending signals between interpreter bytecode
    instructions, which Qt's own C++ event loop doesn't yield to on its
    own - a plain signal.signal() handler can sit un-delivered for as
    long as the event loop runs without something forcing the interpreter
    to periodically regain control. The idle QTimer below exists purely
    for that (a well-known Qt+Python interaction, not specific to this
    app) - its own timeout callback does nothing."""
    from PyQt6.QtCore import QTimer

    wakeup_timer = QTimer(window)
    wakeup_timer.timeout.connect(lambda: None)
    wakeup_timer.start(500)
    window._signal_wakeup_timer = wakeup_timer  # keep it alive - QTimer(window) parents it, but be explicit

    def _handle(signum, _frame):
        logger.info("received signal %s - shutting down gracefully", signum)
        window._quit_app()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main() -> int:
    setup_logging()

    from onedrive import single_instance

    if not single_instance.acquire():
        single_instance.open_existing_instance_folder()
        return 0

    # Native Wayland (KDE Plasma/KWin, confirmed via XDG_SESSION_TYPE) doesn't
    # let a plain client-side window position or move itself - KWin silently
    # overrides on-screen placement even though Qt's own geometry bookkeeping
    # says the move succeeded, which is why every positioning approach tried
    # for the tray popup (computed placement, then manual dragging) had no
    # visible effect. Forcing XWayland restores normal X11 positioning
    # semantics for this app specifically, without changing anything for the
    # rest of the (native Wayland) desktop. Only takes effect if the app
    # hasn't already been launched with QT_QPA_PLATFORM set explicitly.
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    from PyQt6.QtWidgets import QApplication

    from onedrive.gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(constants.DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    _install_graceful_shutdown(window)
    # Every real background sync client (this one's own tray/mount design is
    # modeled on them) starts silently on login - only a first-time user who
    # hasn't signed in yet needs the window to actually appear, so they have
    # something to sign in with. A returning, already-signed-in user gets the
    # tray icon only; the window opens on demand via the tray (click/double-
    # click, both already wired) exactly like every subsequent manual launch.
    if not (window.auth.is_signed_in and window.drive_id):
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
