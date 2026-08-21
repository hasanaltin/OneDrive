from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from onedrive.db import Database

_DEBOUNCE_MS = 350


class _RecipientRow(QWidget):
    def __init__(self, name: str, email: str, on_remove, parent=None):
        super().__init__(parent)
        self.email = email

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        text = name if name == email else f"{name}  ({email})"
        label = QLabel(text)
        layout.addWidget(label, stretch=1)

        remove_btn = QPushButton("Remove")
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.clicked.connect(lambda: on_remove(self))
        layout.addWidget(remove_btn)


class ShareDialog(QDialog):
    """Search-and-invite recipient picker for a single file or folder -
    triggered from Dolphin's right-click "OneDrive" submenu via the same
    fire-and-forget socket request + cross-thread signal pattern as
    ManageAccessDialog (see OPENSHARE in dolphin_overlay_server.py).

    Originally built as a KDE Purpose plugin with its own QML config
    dialog (0.4.95) - moved to a native PyQt6 dialog so it could live in
    the same "OneDrive" context-menu submenu as pin/unpin, Copy Link, and
    Manage Access (Purpose plugins can only ever appear in Dolphin's own
    generic "Share" submenu, not an arbitrary other plugin's submenu).
    Also simpler: search_people()/invite() are called directly in-process
    instead of round-tripping through the overlay socket + a separate C++/
    QML plugin just to reach the same GraphClient this process already
    holds."""

    def __init__(self, db: Database, graph_client, drive_id: str, item_id: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.graph = graph_client
        self.drive_id = drive_id
        self.item_id = item_id
        self._recipients: dict[str, str] = {}  # email -> name

        item = self.db.get_item_by_id(drive_id, item_id)
        item_name = item.name if item else item_id
        self.setWindowTitle(f"Share - {item_name}")
        self.resize(480, 480)

        layout = QVBoxLayout(self)

        info = QLabel(f'Share "{item_name}" with specific people - they\'ll get an email with access.')
        info.setWordWrap(True)
        layout.addWidget(info)

        self._search_field = QLineEdit()
        self._search_field.setPlaceholderText("Search by name or email…")
        self._search_field.textChanged.connect(self._on_search_text_changed)
        layout.addWidget(self._search_field)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._run_search)

        self._results_list = QListWidget()
        self._results_list.setFrameShape(QFrame.Shape.NoFrame)
        self._results_list.setMaximumHeight(140)
        self._results_list.itemClicked.connect(self._on_result_clicked)
        self._results_list.hide()
        layout.addWidget(self._results_list)

        layout.addWidget(QLabel("Recipients:"))
        self._recipients_list = QListWidget()
        self._recipients_list.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self._recipients_list, stretch=1)

        self._empty_recipients_label = QLabel("No recipients added yet.")
        self._empty_recipients_label.setWordWrap(True)
        layout.addWidget(self._empty_recipients_label)

        role_row = QHBoxLayout()
        role_row.addWidget(QLabel("Permission:"))
        self._role_combo = QComboBox()
        self._role_combo.addItem("Can view", "read")
        self._role_combo.addItem("Can edit", "write")
        role_row.addWidget(self._role_combo, stretch=1)
        layout.addLayout(role_row)

        self._message_field = QTextEdit()
        self._message_field.setPlaceholderText("Add a message (optional)")
        self._message_field.setMaximumHeight(70)
        layout.addWidget(self._message_field)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(cancel_btn)
        self._send_btn = QPushButton("Send")
        self._send_btn.setDefault(True)
        self._send_btn.clicked.connect(self._on_send)
        button_row.addWidget(self._send_btn)
        layout.addLayout(button_row)

        self._refresh_recipients_ui()

    # --- search -------------------------------------------------------

    def _on_search_text_changed(self, text: str) -> None:
        self._debounce.stop()
        if not text.strip():
            self._results_list.clear()
            self._results_list.hide()
            return
        self._debounce.start()

    def _run_search(self) -> None:
        query = self._search_field.text().strip()
        if not query:
            return
        try:
            results = self.graph.search_people(query)
        except Exception:
            results = []
        self._results_list.clear()
        for person in results:
            item = QListWidgetItem(f"{person['name']}  —  {person['email']}" if person["name"] != person["email"]
                                    else person["email"])
            item.setData(Qt.ItemDataRole.UserRole, person)
            self._results_list.addItem(item)
        self._results_list.setVisible(self._results_list.count() > 0)

    def _on_result_clicked(self, item: QListWidgetItem) -> None:
        person = item.data(Qt.ItemDataRole.UserRole)
        self._recipients[person["email"]] = person["name"]
        self._refresh_recipients_ui()
        self._search_field.clear()
        self._results_list.clear()
        self._results_list.hide()

    # --- recipients -----------------------------------------------------

    def _refresh_recipients_ui(self) -> None:
        self._recipients_list.clear()
        if not self._recipients:
            self._recipients_list.hide()
            self._empty_recipients_label.show()
            return
        self._empty_recipients_label.hide()
        self._recipients_list.show()
        for email, name in self._recipients.items():
            row = _RecipientRow(name, email, self._on_remove_recipient)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self._recipients_list.addItem(item)
            self._recipients_list.setItemWidget(item, row)

    def _on_remove_recipient(self, row: "_RecipientRow") -> None:
        self._recipients.pop(row.email, None)
        self._refresh_recipients_ui()

    # --- send -------------------------------------------------------

    def _on_send(self) -> None:
        if not self._recipients:
            QMessageBox.information(self, "Share", "Add at least one person to share with.")
            return
        role = self._role_combo.currentData()
        message = self._message_field.toPlainText()
        try:
            self.graph.invite(self.drive_id, self.item_id, list(self._recipients.keys()), role, message)
        except Exception as e:
            QMessageBox.warning(self, "Share", f"Couldn't share: {e}")
            return
        self.accept()
