import datetime
import threading

from PyQt6.QtCore import Qt, pyqtSignal
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


def _format_ts(ts_iso: str) -> str:
    try:
        return datetime.datetime.fromisoformat(ts_iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts_iso


class _ConflictRow(QWidget):
    def __init__(self, conflict: dict, on_decision, parent=None):
        super().__init__(parent)
        self.row_id = conflict["id"]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 6, 4, 6)

        text = QLabel(
            f"{conflict['name']}\n→ kept as {conflict['path']}   ·   {_format_ts(conflict['ts'])}"
        )
        text.setWordWrap(True)
        layout.addWidget(text, stretch=1)

        self._buttons = []
        for label, decision in (("Keep Local", "keep_local"), ("Keep Server", "keep_server"), ("Dismiss", "dismiss")):
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, d=decision: on_decision(self, d))
            layout.addWidget(btn)
            self._buttons.append(btn)

    def set_busy(self, busy: bool) -> None:
        for btn in self._buttons:
            btn.setEnabled(not busy)


class ConflictsDialog(QDialog):
    """Lists the individual files that currently have an unreviewed
    two-sided conflict for one source - either one Folder Pair
    ("pair:<id>") or the on-demand mount as a whole ("mount"; mount
    conflicts aren't per-pair, so there's exactly one of these across the
    whole drive). The row's own badge/menu, driven by db.count_conflicts(),
    only ever offers to open this when that's non-zero. conflict_count here
    is a separate PERMANENT lifetime counter, used only for the empty-state
    note if this dialog is ever reached with nothing left to show. Both
    sides of every conflict are always already preserved (the auto-
    resolution that created the record - resolve_pair_conflict/
    resolve_mount_conflict in sync/conflict_actions.py - never overwrites
    anything), so this is purely a review/cleanup step: pick which version
    to keep going forward, or dismiss to leave both files exactly as they
    are. `resolve_fn(conflict_row, decision)` does the actual work (off the
    GUI thread, see _on_decision) - supplied by the caller so this dialog
    stays agnostic to which kind of conflict it's showing."""

    _resolved = pyqtSignal(int, str)  # row_id, error message ("" on success)

    def __init__(self, db: Database, source: str, title: str, resolve_fn, conflict_count: int = 0,
                 on_changed=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.source = source
        self.resolve_fn = resolve_fn
        self._on_changed = on_changed  # optional callable, invoked after any resolution commits

        self.setWindowTitle(f"Conflicts – {title}")
        self.resize(640, 440)

        self._layout = QVBoxLayout(self)
        self._resolved.connect(self._on_resolved)

        self._info = QLabel(
            "Each of these files was edited on both sides before either edit had been synced, "
            "so nothing was overwritten: the local edit was kept, saved alongside the remote "
            "version under a new “(conflicted copy …)” name. Choose which version to keep, or "
            "Dismiss to leave both files as they are."
        )
        self._info.setWordWrap(True)
        self._layout.addWidget(self._info)

        self._list = QListWidget()
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._layout.addWidget(self._list, stretch=1)

        self._empty_label = QLabel()
        self._empty_label.setWordWrap(True)
        self._layout.addWidget(self._empty_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.accept)
        button_row.addWidget(self._close_btn)
        self._layout.addLayout(button_row)

        self._conflict_count = conflict_count
        self._reload()

    def _reload(self) -> None:
        conflicts = self.db.list_conflicts(self.source)
        self._list.clear()

        if not conflicts:
            self._list.hide()
            self._info.hide()
            if self._conflict_count:
                self._empty_label.setText(
                    "No conflicts currently need review. Either none have happened recently, or "
                    "older ones were evicted from the log by a large amount of unrelated sync "
                    "activity before anyone reviewed them - conflicts from now on stay listed here "
                    "until reviewed."
                )
            else:
                self._empty_label.setText("No conflicts recorded for this pair.")
            self._empty_label.show()
            return

        self._empty_label.hide()
        self._info.show()
        self._list.show()
        for c in conflicts:
            row = _ConflictRow(c, self._on_decision)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, row)

    def _on_decision(self, row: "_ConflictRow", decision: str) -> None:
        if decision in ("keep_local", "keep_server"):
            verb = "Keep the local edit" if decision == "keep_local" else "Keep the server version"
            other = "the current server version" if decision == "keep_local" else "the local edit"
            confirm = QMessageBox.question(
                self, "Resolve Conflict",
                f"{verb}? The conflicted copy holding {other} will be permanently deleted "
                "(OneDrive moves the remote copy to its recycle bin; the local copy is removed "
                "directly and is not recoverable through this app).",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        row.set_busy(True)
        self._close_btn.setEnabled(False)
        conflict_row = {"id": row.row_id}
        for c in self.db.list_conflicts(self.source):
            if c["id"] == row.row_id:
                conflict_row = c
                break

        def run():
            error = ""
            try:
                self.resolve_fn(conflict_row, decision)
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
            self._resolved.emit(row.row_id, error)

        threading.Thread(target=run, daemon=True).start()

    def _on_resolved(self, row_id: int, error: str) -> None:
        self._close_btn.setEnabled(True)
        if error:
            QMessageBox.warning(self, "Resolve Conflict", f"Couldn't resolve this conflict:\n\n{error}")
        self._reload()
        if self._on_changed is not None:
            self._on_changed()
