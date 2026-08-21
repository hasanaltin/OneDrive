import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable

from onedrive import constants, sync_status
from onedrive.db import Database

logger = logging.getLogger(__name__)

_RECV_BUFSIZE = 4096


class OverlayServer(threading.Thread):
    """Listens on a Unix domain socket for Dolphin-side plugins that have no
    direct access to this app's database/state: the overlay-icon plugin
    (packaging/dolphin-overlay/, "what's the sync status of this path?")
    and the pin-action context-menu plugin (packaging/dolphin-pin-action/,
    "is this pinned, and let me change that") - the same basic IPC bridge
    idea Nextcloud's own client uses for its overlay icons.

    Protocol: one line per request, one line per response, connection
    closed after each exchange - Dolphin can ask about hundreds of files at
    once for a big folder listing, so keeping this stateless avoids any
    connection-lifecycle bugs mattering more than they're worth for v1.

        request:  STATUS <absolute-path>\\n
        response: LOCAL\\n | CLOUD\\n | SYNCING\\n | NONE\\n

        request:  PINSTATE <absolute-path>\\n
        response: PINNED\\n | UNPINNED\\n | NONE\\n

        request:  SETPIN <0|1> <absolute-path>\\n
        response: OK\\n | ERROR <message>\\n

        request:  SHARELINK <absolute-path>\\n
        response: LINK <url>\\n | NONE\\n | ERROR <message>\\n

        request:  MANAGEACCESS <absolute-path>\\n
        response: OK\\n | NONE\\n

        request:  OPENSHARE <absolute-path>\\n
        response: OK\\n | NONE\\n

    NONE for PINSTATE means "not a folder this app tracks" (outside the
    mount, or a plain file - pinning only ever applied to folders in the
    existing "Choose folders" tree, and the context-menu plugin keeps that
    same scope rather than growing it here).

    SHARELINK applies to both files and folders, and - unlike PINSTATE/
    SETPIN - resolves paths under EITHER the on-demand mount OR any local
    Folder Pair root, since sharing a file the user is editing through a
    paired folder (their actual Desktop/Documents/Pictures) is at least as
    common a case as sharing something browsed through the mount. NONE
    means the path isn't tracked at all, or is tracked but has never been
    synced yet (no remote id to share).

    MANAGEACCESS and OPENSHARE are both fire-and-forget: each resolves the
    path, then hands (drive_id, remote_id) off to a callback into the GUI
    thread (on_manage_access / on_share - see main_window.py's
    WorkerSignals.manage_access_requested / share_requested) to actually
    open the relevant dialog, since that's real interactive UI this
    background thread can't create itself. The response only ever
    confirms whether the path resolved, not whether the dialog was shown
    or what the user did with it.
    """

    def __init__(
        self,
        db: Database,
        mountpoint_getter: Callable[[], object],
        pin_worker_getter: Callable[[], object] | None = None,
        graph_client=None,
        on_manage_access: Callable[[str, str], None] | None = None,
        on_share: Callable[[str, str], None] | None = None,
    ):
        super().__init__(daemon=True, name="OverlayServer")
        self.db = db
        self._mountpoint_getter = mountpoint_getter
        self._pin_worker_getter = pin_worker_getter or (lambda: None)
        self.graph = graph_client
        self.on_manage_access = on_manage_access or (lambda drive_id, item_id: None)
        self.on_share = on_share or (lambda drive_id, item_id: None)
        self._stop = threading.Event()

    def run(self) -> None:
        path = constants.OVERLAY_SOCKET_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            # A stale socket file left behind by a crash makes bind() fail
            # with EADDRINUSE even though nothing is listening on it anymore.
            path.unlink(missing_ok=True)
        except OSError:
            pass

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
        except OSError:
            logger.exception("overlay server: couldn't bind %s", path)
            return
        os.chmod(path, 0o600)
        sock.listen(16)
        sock.settimeout(1.0)  # lets the accept loop notice stop() promptly
        logger.info("overlay server listening on %s", path)

        try:
            while not self._stop.is_set():
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            sock.close()
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(2.0)
            data = conn.recv(_RECV_BUFSIZE)
            line = data.decode("utf-8", errors="replace").strip()
            response = self._handle_line(line)
            logger.info("overlay server: %r -> %r", line, response)
            conn.sendall((response + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            conn.close()

    def _handle_line(self, line: str) -> str:
        if line.startswith("STATUS "):
            return self._handle_status(line[len("STATUS "):].strip())
        if line.startswith("PINSTATE "):
            return self._handle_pinstate(line[len("PINSTATE "):].strip())
        if line.startswith("SETPIN "):
            return self._handle_setpin(line[len("SETPIN "):].strip())
        if line.startswith("SHARELINK "):
            return self._handle_sharelink(line[len("SHARELINK "):].strip())
        if line.startswith("MANAGEACCESS "):
            return self._handle_manageaccess(line[len("MANAGEACCESS "):].strip())
        if line.startswith("OPENSHARE "):
            return self._handle_openshare(line[len("OPENSHARE "):].strip())
        return "NONE"

    def _handle_status(self, raw_path: str) -> str:
        if not raw_path:
            return "NONE"
        mountpoint = self._mountpoint_getter()
        mountpoint_path = Path(mountpoint) if mountpoint else None
        try:
            status = sync_status.status_for_path(self.db, mountpoint_path, Path(raw_path))
        except Exception:
            logger.exception("overlay server: status lookup failed for %s", raw_path)
            return "NONE"
        return status.upper() if status else "NONE"

    def _resolve_mount_item(self, raw_path: str):
        """Resolves an absolute path under the on-demand mount to (drive_id,
        Item), or (None, None) if it isn't one - shared by PINSTATE/SETPIN,
        which both only ever apply to folders inside this specific mount
        (Folder Pairs are plain local folders already, with no pin concept
        of their own)."""
        mountpoint = self._mountpoint_getter()
        if not mountpoint or not raw_path:
            return None, None
        try:
            rel = Path(raw_path).relative_to(mountpoint)
        except ValueError:
            return None, None
        drive_id = self.db.get_sync_state("drive_id")
        if not drive_id:
            return None, None
        drive_rel_path = "" if str(rel) == "." else "/" + rel.as_posix()
        item = self.db.get_item_by_path(drive_id, drive_rel_path)
        return drive_id, item

    def _resolve_pair_item(self, raw_path: str):
        """Resolves an absolute path under a Folder Pair's local root to
        (drive_id, Item) via its remote_path prefix, or (None, None) if it
        isn't under any pair - the pair-side counterpart to
        _resolve_mount_item, used only by SHARELINK since PINSTATE/SETPIN
        have no meaning for a plain local folder."""
        if not raw_path:
            return None, None
        target = Path(raw_path)
        for pair in self.db.list_pairs():
            try:
                rel = target.relative_to(pair.local_path)
            except ValueError:
                continue
            if str(rel) == ".":
                drive_rel_path = pair.remote_path
            else:
                base = pair.remote_path.rstrip("/")
                drive_rel_path = base + "/" + rel.as_posix()
            item = self.db.get_item_by_path(pair.drive_id, drive_rel_path)
            return pair.drive_id, item
        return None, None

    def _resolve_any_item(self, raw_path: str):
        """SHARELINK's path resolver: tries the on-demand mount first, then
        falls back to Folder Pairs - a path can only ever be under one of
        the two, so the first hit wins."""
        drive_id, item = self._resolve_mount_item(raw_path)
        if item is not None:
            return drive_id, item
        return self._resolve_pair_item(raw_path)

    def _handle_sharelink(self, raw_path: str) -> str:
        if self.graph is None:
            return "ERROR sharing unavailable"
        try:
            drive_id, item = self._resolve_any_item(raw_path)
        except Exception:
            logger.exception("overlay server: sharelink resolve failed for %s", raw_path)
            return "NONE"
        if item is None or not item.remote_id:
            return "NONE"
        try:
            url = self.graph.create_share_link(drive_id, item.remote_id)
        except Exception as e:
            logger.exception("overlay server: create_share_link failed for %s", raw_path)
            return f"ERROR {e}"
        return f"LINK {url}"

    def _handle_manageaccess(self, raw_path: str) -> str:
        try:
            drive_id, item = self._resolve_any_item(raw_path)
        except Exception:
            logger.exception("overlay server: manageaccess resolve failed for %s", raw_path)
            return "NONE"
        if item is None or not item.remote_id:
            return "NONE"
        try:
            self.on_manage_access(drive_id, item.remote_id)
        except Exception:
            logger.exception("overlay server: on_manage_access callback failed for %s", raw_path)
            return "NONE"
        return "OK"

    def _handle_openshare(self, raw_path: str) -> str:
        try:
            drive_id, item = self._resolve_any_item(raw_path)
        except Exception:
            logger.exception("overlay server: openshare resolve failed for %s", raw_path)
            return "NONE"
        if item is None or not item.remote_id:
            return "NONE"
        try:
            self.on_share(drive_id, item.remote_id)
        except Exception:
            logger.exception("overlay server: on_share callback failed for %s", raw_path)
            return "NONE"
        return "OK"

    def _handle_pinstate(self, raw_path: str) -> str:
        _drive_id, item = self._resolve_mount_item(raw_path)
        if item is None or not item.is_folder:
            return "NONE"
        return "PINNED" if item.is_pinned else "UNPINNED"

    def _handle_setpin(self, rest: str) -> str:
        try:
            flag, raw_path = rest.split(" ", 1)
        except ValueError:
            return "ERROR malformed request"
        drive_id, item = self._resolve_mount_item(raw_path)
        if item is None or not item.is_folder:
            return "ERROR not a tracked OneDrive folder"
        pinned = flag == "1"
        try:
            self.db.set_pinned(drive_id, item.id, pinned)
        except Exception as e:
            logger.exception("overlay server: set_pinned failed for %s", raw_path)
            return f"ERROR {e}"
        if pinned:
            worker = self._pin_worker_getter()
            if worker is not None:
                worker.wake()
        return "OK"
