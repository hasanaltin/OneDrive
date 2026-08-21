from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from onedrive import constants
from onedrive.db import Database
from onedrive.graph_client import GraphClient

_ITEM_ID_ROLE = Qt.ItemDataRole.UserRole
_LOADED_ROLE = Qt.ItemDataRole.UserRole + 1


class AddPairDialog(QDialog):
    """Follows the same .exec()/.success idiom as login_dialog.py's
    DeviceCodeDialog. On success, .local_path/.remote_item_id/.remote_path
    hold the chosen pair - the caller does db.create_pair(...) (or, when
    `existing_pair` was passed, db.update_pair_mapping(...)) itself.

    `existing_pair` (a db.Pair) switches this into edit mode - requested
    directly, there was no way to change an already-created pair's local/
    remote mapping at all before this: pre-fills both fields with the
    pair's current values and excludes that same pair from the "overlaps
    an existing pair" check in _on_accept (otherwise its own unchanged
    local folder would always fail that check against itself)."""

    def __init__(self, db: Database, graph: GraphClient, drive_id: str, parent=None, existing_pair=None):
        super().__init__(parent)
        self.db = db
        self.graph = graph
        self.drive_id = drive_id
        self._success = False
        self._editing_pair_id = existing_pair.id if existing_pair is not None else None
        self.local_path: str | None = None
        self.remote_item_id: str | None = existing_pair.remote_item_id if existing_pair is not None else None
        self.remote_path: str | None = existing_pair.remote_path if existing_pair is not None else None

        self.setWindowTitle("Edit Folder Pair" if existing_pair is not None else "Add Folder Pair")
        self.setMinimumSize(480, 420)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Local folder:"))
        local_row = QHBoxLayout()
        initial_local = existing_pair.local_path if existing_pair is not None else str(Path.home())
        self._local_field = QLineEdit(initial_local)
        local_row.addWidget(self._local_field, stretch=1)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._on_browse_local)
        local_row.addWidget(browse_btn)
        layout.addLayout(local_row)

        layout.addWidget(QLabel("Remote folder (pick one, or create a new one):"))
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._tree, stretch=1)

        new_folder_row = QHBoxLayout()
        self._new_folder_name = QLineEdit()
        self._new_folder_name.setPlaceholderText("New folder name")
        new_folder_row.addWidget(self._new_folder_name, stretch=1)
        new_folder_btn = QPushButton("Create here")
        new_folder_btn.clicked.connect(self._on_create_folder)
        new_folder_row.addWidget(new_folder_btn)
        layout.addLayout(new_folder_row)

        self._selected_label = QLabel(
            f"Selected: {self.remote_path}" if self.remote_path else "No remote folder selected"
        )
        layout.addWidget(self._selected_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate_top_level()

    # --- local folder -----------------------------------------------------

    def _on_browse_local(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose local folder", self._local_field.text())
        if chosen:
            self._local_field.setText(chosen)

    # --- remote tree --------------------------------------------------------

    def _populate_top_level(self) -> None:
        self._tree.clear()
        root_item = self.db.get_item_by_path(self.drive_id, "")
        if root_item is None:
            return
        top = QTreeWidgetItem(self._tree, ["/ (OneDrive root)"])
        top.setData(0, _ITEM_ID_ROLE, root_item.id)
        top.setData(0, Qt.ItemDataRole.UserRole + 2, "")  # remote path
        top.setData(0, _LOADED_ROLE, False)
        QTreeWidgetItem(top, ["Loading..."])
        top.setExpanded(True)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, _LOADED_ROLE):
            return
        item.takeChildren()
        parent_id = item.data(0, _ITEM_ID_ROLE)
        for child in self.db.list_children(self.drive_id, parent_id):
            if not child.is_folder:
                continue
            child_item = QTreeWidgetItem(item, [child.name])
            child_item.setData(0, _ITEM_ID_ROLE, child.id)
            child_item.setData(0, Qt.ItemDataRole.UserRole + 2, child.path)
            child_item.setData(0, _LOADED_ROLE, False)
            QTreeWidgetItem(child_item, ["Loading..."])
        item.setData(0, _LOADED_ROLE, True)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        remote_id = item.data(0, _ITEM_ID_ROLE)
        remote_path = item.data(0, Qt.ItemDataRole.UserRole + 2)
        if remote_id is None:
            return
        self.remote_item_id = remote_id
        self.remote_path = remote_path or "/"
        self._selected_label.setText(f"Selected: {self.remote_path}")

    def _on_create_folder(self) -> None:
        name = self._new_folder_name.text().strip()
        if not name:
            QMessageBox.warning(self, "New folder", "Enter a folder name first.")
            return
        if not self.remote_item_id:
            QMessageBox.warning(self, "New folder", "Select a parent folder in the tree first.")
            return
        try:
            result = self.graph.create_folder(self.drive_id, self.remote_item_id, name)
        except Exception as e:
            QMessageBox.critical(self, "New folder", f"Couldn't create folder: {e}")
            return
        self.db.upsert_item(self.drive_id, result)
        new_path = (self.remote_path.rstrip("/") + "/" + name) if self.remote_path != "/" else "/" + name
        selected_items = self._tree.selectedItems()
        parent_widget_item = selected_items[0] if selected_items else self._tree.topLevelItem(0)
        new_item = QTreeWidgetItem(parent_widget_item, [name])
        new_item.setData(0, _ITEM_ID_ROLE, result["id"])
        new_item.setData(0, Qt.ItemDataRole.UserRole + 2, new_path)
        new_item.setData(0, _LOADED_ROLE, True)
        parent_widget_item.setExpanded(True)
        self.remote_item_id = result["id"]
        self.remote_path = new_path
        self._selected_label.setText(f"Selected: {self.remote_path}")
        self._new_folder_name.clear()

    # --- validation / accept ------------------------------------------------

    def _on_accept(self) -> None:
        local_path = self._local_field.text().strip()
        if not local_path:
            QMessageBox.warning(self, "Add Folder Pair", "Choose a local folder.")
            return
        local_p = Path(local_path).expanduser().resolve()

        if not self.remote_item_id:
            QMessageBox.warning(self, "Add Folder Pair", "Choose (or create) a remote folder.")
            return

        mount_p = Path(constants.DEFAULT_MOUNTPOINT).resolve()
        if local_p == mount_p or mount_p in local_p.parents or local_p in mount_p.parents:
            QMessageBox.warning(
                self, "Add Folder Pair",
                "That path overlaps the on-demand mount folder - pick a different local folder.",
            )
            return

        for pair in self.db.list_pairs():
            if self._editing_pair_id is not None and pair.id == self._editing_pair_id:
                continue
            existing = Path(pair.local_path).resolve()
            if existing == local_p or existing in local_p.parents or local_p in existing.parents:
                QMessageBox.warning(
                    self, "Add Folder Pair",
                    f"That folder overlaps an existing pair ({pair.local_path}).",
                )
                return

        local_p.mkdir(parents=True, exist_ok=True)
        self.local_path = str(local_p)
        self._success = True
        self.accept()

    @property
    def success(self) -> bool:
        return self._success
