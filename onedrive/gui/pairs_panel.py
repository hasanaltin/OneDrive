from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from onedrive.db import Database
from onedrive.gui.conflicts_dialog import ConflictsDialog
from onedrive.gui.pairs_dialog import AddPairDialog
from onedrive.sync.conflict_actions import resolve_pair_conflict

_GREEN = "#2E9E44"
_BLUE = "#0364B8"
_RED = "#C0392B"
_GRAY = "#8A8A8A"

_COL_NAME, _COL_STATUS, _COL_LOCAL, _COL_REMOTE, _COL_SYNCED, _COL_ACTIONS = range(6)


def _status_icon(status: str, enabled: bool) -> QPixmap:
    size = 34
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    if not enabled:
        color = _GRAY
    elif status.startswith("error"):
        color = _RED
    elif status == "syncing" or status.startswith((
        "Syncing", "Uploading", "Downloading", "Checking", "Creating", "Deleting", "Conflict",
    )):
        color = _BLUE
    else:
        color = _GREEN

    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, size, size)

    painter.setPen(QColor("white"))
    pen = painter.pen()
    pen.setWidth(2)
    painter.setPen(pen)
    if not enabled:
        # paused glyph: two small bars
        painter.drawLine(size // 2 - 5, size // 2 - 7, size // 2 - 5, size // 2 + 7)
        painter.drawLine(size // 2 + 5, size // 2 - 7, size // 2 + 5, size // 2 + 7)
    elif color == _RED:
        painter.drawLine(size // 2 - 6, size // 2 - 6, size // 2 + 6, size // 2 + 6)
        painter.drawLine(size // 2 + 6, size // 2 - 6, size // 2 - 6, size // 2 + 6)
    elif color == _BLUE:
        # sync glyph: simple circular arrow suggestion (arc)
        painter.drawArc(8, 8, size - 16, size - 16, 30 * 16, 280 * 16)
    else:
        # checkmark
        painter.drawLine(size // 2 - 7, size // 2, size // 2 - 2, size // 2 + 6)
        painter.drawLine(size // 2 - 2, size // 2 + 6, size // 2 + 8, size // 2 - 7)
    painter.end()
    return pixmap


class PairsPanel(QWidget):
    def __init__(self, db: Database, graph, get_drive_id, pair_worker_ref, parent=None):
        super().__init__(parent)
        self.db = db
        self.graph = graph
        self._get_drive_id = get_drive_id
        self._pair_worker_ref = pair_worker_ref  # callable returning current PairSyncWorker | None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # A stacked, all-gray title/subtitle/extra text block (one earlier
        # version of this panel) was reported directly as hard to read -
        # local path, remote path and last-synced all ran together in one
        # small gray line. A real table with its own columns, showing
        # everything side by side, is what was asked for instead.
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Name", "Status", "Local Folder", "Remote Folder", "Last Synced", ""]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setFrameShape(QFrame.Shape.NoFrame)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._table.setStyleSheet(
            "QTableWidget::item:selected { background: rgba(3, 100, 184, 35); color: palette(text); }"
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_LOCAL, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_REMOTE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_SYNCED, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_ACTIONS, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table, stretch=1)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(4, 8, 4, 4)
        add_btn = QPushButton("+  Add Folder Pair")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(
            "QPushButton { background: #0364B8; color: white; border: none; "
            "border-radius: 4px; padding: 8px 16px; font-weight: 600; }"
            "QPushButton:hover { background: #014A85; }"
        )
        add_btn.clicked.connect(self._on_add_clicked)
        button_row.addWidget(add_btn)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.refresh()

    def refresh(self) -> None:
        selected_id = self._selected_pair_id()
        pairs = self.db.list_pairs()
        self._table.setRowCount(len(pairs))
        for row, pair in enumerate(pairs):
            reviewable = self.db.count_conflicts(f"pair:{pair.id}")
            self._populate_row(row, pair, reviewable)
            if pair.id == selected_id:
                self._table.selectRow(row)

    def _populate_row(self, row: int, pair, reviewable: int) -> None:
        name = pair.local_path.rstrip("/").rsplit("/", 1)[-1] or pair.local_path
        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.ItemDataRole.UserRole, pair.id)
        font = name_item.font()
        font.setWeight(font.Weight.DemiBold)
        name_item.setFont(font)
        self._table.setItem(row, _COL_NAME, name_item)

        # Icon only (requested directly: "burada sadece senkron edildigini
        # gosteren icon olsun" - just an icon showing whether it's synced)
        # - no inline status text at all anymore, not even for an active
        # upload's filename/progress. The icon's color already carries the
        # state (green=synced, blue=syncing, gray=paused, red=error); the
        # full status string is still available as a tooltip rather than
        # dropped outright, same as the truncated path columns.
        status_widget = QWidget()
        status_layout = QHBoxLayout(status_widget)
        status_layout.setContentsMargins(6, 0, 6, 0)
        icon_label = QLabel()
        icon_label.setPixmap(
            _status_icon(pair.last_sync_status, pair.enabled).scaled(
                16, 16, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )
        status_text = "Paused" if not pair.enabled else pair.last_sync_status
        if reviewable:
            note = f"{reviewable} conflict{'s' if reviewable != 1 else ''} to review"
            status_text = f"{status_text} · {note}"
        status_widget.setToolTip(status_text)
        icon_label.setToolTip(status_text)
        status_layout.addWidget(icon_label)
        status_layout.addStretch(1)
        self._table.setCellWidget(row, _COL_STATUS, status_widget)

        local_item = QTableWidgetItem(pair.local_path)
        local_item.setToolTip(pair.local_path)
        self._table.setItem(row, _COL_LOCAL, local_item)

        remote_item = QTableWidgetItem(pair.remote_path)
        remote_item.setToolTip(pair.remote_path)
        self._table.setItem(row, _COL_REMOTE, remote_item)

        synced_text = pair.last_sync_at.split("T")[0] if pair.last_sync_at else "—"
        synced_item = QTableWidgetItem(synced_text)
        self._table.setItem(row, _COL_SYNCED, synced_item)

        menu_btn = QToolButton()
        menu_btn.setText("⋯")
        menu_btn.setAutoRaise(True)
        menu_btn.setStyleSheet("QToolButton { font-size: 16px; font-weight: bold; }")
        menu_btn.clicked.connect(lambda checked=False, pid=pair.id, btn=menu_btn: self._show_row_menu(pid, btn))
        self._table.setCellWidget(row, _COL_ACTIONS, menu_btn)

    def _selected_pair_id(self) -> int | None:
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, _COL_NAME)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _show_row_menu(self, pair_id: int, anchor: QToolButton) -> None:
        pair = self.db.get_pair(pair_id)
        if pair is None:
            return
        menu = QMenu(self)
        edit_action = menu.addAction("Edit Pair…")
        menu.addSeparator()
        toggle_action = menu.addAction("Disable" if pair.enabled else "Enable")
        conflicts_action = None
        # Only offer this when there's actually something to show - a stale
        # lifetime conflict_count with nothing left in the log to review
        # (evicted before conflicts were exempted from pruning, or already
        # reviewed) led directly to the user clicking in and finding nothing.
        reviewable = self.db.count_conflicts(f"pair:{pair_id}")
        if reviewable:
            conflicts_action = menu.addAction(
                f"View {reviewable} Conflict{'s' if reviewable != 1 else ''}…"
            )
        menu.addSeparator()
        remove_action = menu.addAction("Remove Pair…")

        chosen = menu.exec(anchor.mapToGlobal(anchor.rect().bottomRight()))
        if chosen == edit_action:
            self._edit_pair(pair_id)
        elif chosen == toggle_action:
            self._toggle_pair(pair_id)
        elif conflicts_action is not None and chosen == conflicts_action:
            self._view_conflicts(pair_id)
        elif chosen == remove_action:
            self._remove_pair(pair_id)

    def _view_conflicts(self, pair_id: int) -> None:
        pair = self.db.get_pair(pair_id)
        if pair is None:
            return
        name = pair.local_path.rstrip("/").rsplit("/", 1)[-1] or pair.local_path

        def on_changed():
            worker = self._pair_worker_ref()
            if worker is not None:
                worker.wake(pair_id)

        def resolve_fn(conflict_row, decision):
            current_pair = self.db.get_pair(pair_id)
            if current_pair is None:
                raise RuntimeError("this pair no longer exists")
            resolve_pair_conflict(self.db, self.graph, current_pair, conflict_row, decision)

        ConflictsDialog(
            self.db, f"pair:{pair_id}", name, resolve_fn, pair.conflict_count, on_changed, self
        ).exec()

    def _on_add_clicked(self) -> None:
        drive_id = self._get_drive_id()
        if not drive_id:
            QMessageBox.warning(self, "Add Folder Pair", "Sign in first.")
            return
        dialog = AddPairDialog(self.db, self.graph, drive_id, self)
        dialog.exec()
        if not dialog.success:
            return
        self.db.create_pair(dialog.local_path, drive_id, dialog.remote_item_id, dialog.remote_path)
        self.refresh()
        worker = self._pair_worker_ref()
        if worker is not None:
            worker.refresh_pairs()
            worker.wake()

    def _remove_pair(self, pair_id: int) -> None:
        confirm = QMessageBox.question(
            self, "Remove Folder Pair",
            "Stop syncing this pair? Local and remote files are left untouched, "
            "only the pairing itself is removed.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.db.delete_pair(pair_id)
        worker = self._pair_worker_ref()
        if worker is not None:
            worker.refresh_pairs()
        self.refresh()

    def _edit_pair(self, pair_id: int) -> None:
        """Requested directly - there was no way to edit an existing pair,
        which was a real gap: previously the only way to change a pair's
        local/remote mapping was removing it and adding a new one from
        scratch."""
        pair = self.db.get_pair(pair_id)
        if pair is None:
            return
        drive_id = self._get_drive_id()
        if not drive_id:
            QMessageBox.warning(self, "Edit Folder Pair", "Sign in first.")
            return
        dialog = AddPairDialog(self.db, self.graph, drive_id, self, existing_pair=pair)
        dialog.exec()
        if not dialog.success:
            return
        self.db.update_pair_mapping(pair_id, dialog.local_path, dialog.remote_item_id, dialog.remote_path)
        self.refresh()
        worker = self._pair_worker_ref()
        if worker is not None:
            worker.refresh_pairs()
            worker.wake(pair_id)

    def _toggle_pair(self, pair_id: int) -> None:
        pair = self.db.get_pair(pair_id)
        if pair is None:
            return
        self.db.set_pair_enabled(pair_id, not pair.enabled)
        worker = self._pair_worker_ref()
        if worker is not None:
            worker.refresh_pairs()
            if not pair.enabled:  # was disabled, now enabling
                worker.wake(pair_id)
        self.refresh()
