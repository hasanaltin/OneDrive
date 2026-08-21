from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from onedrive.auth import AuthManager


class DeviceFlowThread(QThread):
    flow_ready = pyqtSignal(dict)
    finished_ok = pyqtSignal(dict)
    finished_error = pyqtSignal(str)

    def __init__(self, auth: AuthManager):
        super().__init__()
        self.auth = auth

    def run(self) -> None:
        try:
            flow = self.auth.start_device_flow()
        except Exception as e:
            self.finished_error.emit(str(e))
            return
        self.flow_ready.emit(flow)
        try:
            result = self.auth.complete_device_flow(flow)
        except Exception as e:
            self.finished_error.emit(str(e))
            return
        self.finished_ok.emit(result)


class DeviceCodeDialog(QDialog):
    def __init__(self, auth: AuthManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sign in to Microsoft OneDrive")
        self.setMinimumWidth(420)
        self._success = False

        layout = QVBoxLayout(self)

        self._status_label = QLabel("Starting sign-in...")
        layout.addWidget(self._status_label)

        code_row = QHBoxLayout()
        self._code_field = QLineEdit()
        self._code_field.setReadOnly(True)
        self._code_field.setStyleSheet("font-size: 18pt; font-weight: bold;")
        code_row.addWidget(self._code_field)
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(self._copy_code)
        code_row.addWidget(copy_btn)
        layout.addLayout(code_row)

        self._url_label = QLabel("")
        self._url_label.setOpenExternalLinks(True)
        layout.addWidget(self._url_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        layout.addWidget(self._progress)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(cancel_btn)

        self._thread = DeviceFlowThread(auth)
        self._thread.flow_ready.connect(self._on_flow_ready)
        self._thread.finished_ok.connect(self._on_success)
        self._thread.finished_error.connect(self._on_error)
        self._thread.start()

    def _copy_code(self) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(self._code_field.text())

    def _on_flow_ready(self, flow: dict) -> None:
        self._code_field.setText(flow.get("user_code", ""))
        url = flow.get("verification_uri", "")
        self._url_label.setText(f'Open <a href="{url}">{url}</a> and enter the code above')
        self._status_label.setText("Waiting for you to sign in...")

    def _on_success(self, result: dict) -> None:
        self._success = True
        self._status_label.setText("Signed in successfully.")
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self.accept()

    def _on_error(self, message: str) -> None:
        self._status_label.setText(f"Sign-in failed: {message}")
        self._progress.setRange(0, 1)
        self._progress.setValue(0)

    @property
    def success(self) -> bool:
        return self._success
