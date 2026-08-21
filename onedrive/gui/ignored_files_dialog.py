from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class IgnoredFilesDialog(QDialog):
    """Global exclude-pattern editor, styled after Nextcloud's own
    "Ignored Files Editor" (a screenshot of it was supplied directly as
    the reference, asking for add/remove buttons like it has) - a real
    table with Add/Remove/Remove all buttons instead of one big multi-line
    text box.

    Deliberately no "Allow Deletion" column, unlike the reference - that's
    a Nextcloud-specific concept (whether a pattern-matched item may
    itself be deleted to unblock removing its containing folder) with no
    equivalent in this app's exclude model, where a pattern only ever
    means "never sync this at all". Adding an inert checkbox that didn't
    actually do anything would be more confusing than not having it.
    """

    def __init__(self, patterns: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Ignored Files")
        self.setMinimumSize(460, 420)
        self._patterns = list(patterns)

        layout = QVBoxLayout(self)
        desc = QLabel(
            "Files and folders matching these patterns are never synced, for any Folder Pair."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(desc)

        body = QHBoxLayout()
        self._table = QTableWidget(0, 1)
        self._table.setHorizontalHeaderLabels(["Pattern"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        body.addWidget(self._table, stretch=1)

        btn_col = QVBoxLayout()
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._on_add)
        btn_col.addWidget(add_btn)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._on_remove)
        btn_col.addWidget(remove_btn)
        remove_all_btn = QPushButton("Remove all")
        remove_all_btn.clicked.connect(self._on_remove_all)
        btn_col.addWidget(remove_all_btn)
        btn_col.addStretch(1)
        body.addLayout(btn_col)
        layout.addLayout(body)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._reload_table()

    def _reload_table(self) -> None:
        self._table.setRowCount(len(self._patterns))
        for row, pattern in enumerate(self._patterns):
            self._table.setItem(row, 0, QTableWidgetItem(pattern))

    def _on_add(self) -> None:
        text, ok = QInputDialog.getText(self, "Add Ignore Pattern", "Add a new ignore pattern:")
        text = text.strip()
        if not ok or not text or text in self._patterns:
            return
        self._patterns.append(text)
        self._reload_table()

    def _on_remove(self) -> None:
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            del self._patterns[row]
        self._reload_table()

    def _on_remove_all(self) -> None:
        if not self._patterns:
            return
        confirm = QMessageBox.question(self, "Remove all", "Remove every ignore pattern?")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._patterns.clear()
        self._reload_table()

    @property
    def patterns(self) -> list[str]:
        return list(self._patterns)
