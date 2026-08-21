import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from onedrive.db import Database

_OP_LABELS = {
    "create_file": "upload",
    "create_dir": "create folder",
    "write": "upload change",
    "rename": "rename/move",
    "delete": "delete",
}


def _format_ts(ts_iso: str) -> str:
    try:
        return datetime.datetime.fromisoformat(ts_iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts_iso


class _ProblemRow(QWidget):
    def __init__(self, problem: dict, on_dismiss, parent=None):
        super().__init__(parent)
        self.seq = problem["seq"]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 6, 4, 6)

        op_label = _OP_LABELS.get(problem["op_type"], problem["op_type"])
        text = QLabel(
            f"{problem['name']}\n{op_label} · {_format_ts(problem['created_at'])}\n{problem['last_error']}"
        )
        text.setWordWrap(True)
        layout.addWidget(text, stretch=1)

        dismiss_btn = QPushButton("Dismiss")
        dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss_btn.clicked.connect(lambda: on_dismiss(self))
        layout.addWidget(dismiss_btn)


class SyncProblemsDialog(QDialog):
    """Lists pending_mount_ops rows that have failed at least once and are
    still silently retrying - requested directly as the top-priority fix
    after being reported: two operations (a delete that 404'd because the
    item was already gone, and a create that 409'd because an earlier
    attempt already succeeded before a crash kept the op from being
    cleared) had been retrying on every single MountSyncWorker pass with
    zero visibility anywhere in the app. Both of those specific cases are
    now self-healed directly in mount_sync_worker.py (a 404-on-delete and
    a 409-on-create are treated as "already done," not failures) - this
    dialog is for whatever's left: a genuine, non-self-healing problem
    (e.g. a filename OneDrive itself will reject forever) that a user
    actually needs to know about and decide what to do with."""

    def __init__(self, db: Database, drive_id: str, mount_sync_worker_ref, parent=None):
        super().__init__(parent)
        self.db = db
        self.drive_id = drive_id
        self._mount_sync_worker_ref = mount_sync_worker_ref

        self.setWindowTitle("Sync Problems")
        self.resize(560, 420)

        layout = QVBoxLayout(self)

        info = QLabel(
            "These changes haven't synced to OneDrive yet and keep failing the same way. "
            "\"Retry Now\" tries them again immediately (useful after fixing the underlying "
            "cause); \"Dismiss\" gives up on that one specific change without retrying it again."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self._list = QListWidget()
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self._list, stretch=1)

        self._empty_label = QLabel("No sync problems right now.")
        self._empty_label.setWordWrap(True)
        layout.addWidget(self._empty_label)

        button_row = QHBoxLayout()
        retry_btn = QPushButton("Retry Now")
        retry_btn.clicked.connect(self._on_retry)
        button_row.addWidget(retry_btn)
        button_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        self._reload()

    def _reload(self) -> None:
        problems = self.db.list_mount_op_errors(self.drive_id)
        self._list.clear()
        if not problems:
            self._list.hide()
            self._empty_label.show()
            return
        self._empty_label.hide()
        self._list.show()
        for p in problems:
            row = _ProblemRow(p, self._on_dismiss)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, row)

    def _on_dismiss(self, row: "_ProblemRow") -> None:
        confirm = QMessageBox.question(
            self, "Dismiss",
            "Stop retrying this change? It will never be applied to OneDrive unless you make "
            "the same change again.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.db.dismiss_mount_op(row.seq)
        self._reload()

    def _on_retry(self) -> None:
        worker = self._mount_sync_worker_ref()
        if worker is not None:
            worker.wake()
        self._reload()
