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


def _describe_permission(perm: dict) -> str:
    roles = perm.get("roles") or []
    role_text = "Can edit" if "write" in roles else "Can view" if "read" in roles else "/".join(roles) or "?"

    link = perm.get("link")
    if link:
        scope = link.get("scope")
        who = {
            "anonymous": "Anyone with the link",
            "organization": "People in your organization with the link",
            "users": "Specific people with the link",
        }.get(scope, "People with the link")
        return f"{who} - {role_text}"

    granted = (perm.get("grantedToV2") or perm.get("grantedTo") or {}).get("user")
    if granted:
        name = granted.get("displayName") or granted.get("email") or "Unknown person"
        email = granted.get("email")
        who = f"{name} ({email})" if email and email != name else name
        return f"{who} - {role_text}"

    return f"Unknown grant - {role_text}"


class _PermissionRow(QWidget):
    def __init__(self, perm: dict, on_remove, parent=None):
        super().__init__(parent)
        self.permission_id = perm["id"]
        inherited = perm.get("inheritedFrom") is not None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 6, 4, 6)

        text = _describe_permission(perm)
        if inherited:
            text += "\n(inherited from a parent folder - remove it there instead)"
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label, stretch=1)

        remove_btn = QPushButton("Remove")
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setEnabled(not inherited)
        remove_btn.clicked.connect(lambda: on_remove(self))
        layout.addWidget(remove_btn)


class ManageAccessDialog(QDialog):
    """Lists everyone (and every sharing link) with access to a single
    OneDrive item and lets the user revoke any of them - the missing
    counterpart to "Share via OneDrive"/"Copy OneDrive Link": those two
    grant access, this is where you take it back. Mirrors the "Manage
    access" panel in the OneDrive/SharePoint web UI (same three groupings:
    named people, sharing links, groups - though Graph's permissions list
    doesn't cleanly separate them into tabs the way the web UI does, so
    this just lists all of them together)."""

    def __init__(self, db: Database, graph_client, drive_id: str, item_id: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.graph = graph_client
        self.drive_id = drive_id
        self.item_id = item_id

        item = self.db.get_item_by_id(drive_id, item_id)
        item_name = item.name if item else item_id
        self.setWindowTitle(f"Manage Access - {item_name}")
        self.resize(520, 420)

        layout = QVBoxLayout(self)

        info = QLabel(f'Who has access to "{item_name}":')
        info.setWordWrap(True)
        layout.addWidget(info)

        self._list = QListWidget()
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self._list, stretch=1)

        self._empty_label = QLabel("Nobody else has access to this item yet.")
        self._empty_label.setWordWrap(True)
        layout.addWidget(self._empty_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        self._reload()

    def _reload(self) -> None:
        try:
            permissions = self.graph.list_permissions(self.drive_id, self.item_id)
        except Exception as e:
            QMessageBox.warning(self, "Manage Access", f"Couldn't load sharing info: {e}")
            permissions = []

        self._list.clear()
        if not permissions:
            self._list.hide()
            self._empty_label.show()
            return
        self._empty_label.hide()
        self._list.show()
        for perm in permissions:
            row = _PermissionRow(perm, self._on_remove)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, row)

    def _on_remove(self, row: "_PermissionRow") -> None:
        confirm = QMessageBox.question(
            self, "Remove access",
            "Remove this access? They will no longer be able to open this item using it.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.graph.delete_permission(self.drive_id, self.item_id, row.permission_id)
        except Exception as e:
            QMessageBox.warning(self, "Manage Access", f"Couldn't remove access: {e}")
            return
        self._reload()
