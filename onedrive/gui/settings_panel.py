import logging
from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from onedrive import proxy_config
from onedrive.db import DEFAULT_EXCLUDE_PATTERNS, Database
from onedrive.gui.ignored_files_dialog import IgnoredFilesDialog

logger = logging.getLogger(__name__)


class SettingsPanel(QWidget):
    """Bandwidth limits and metered-connection behavior - requested
    directly ("in the settings we should have upload and download settings
    plus also metered connection settings"). Persisted straight to
    sync_state on every change, same as every other setting in this app
    (pin/unpin, folder pairs) - no separate Save button to remember to
    click."""

    def __init__(
        self,
        db: Database,
        on_metered_setting_changed: Callable[[], None] | None = None,
        on_excludes_changed: Callable[[], None] | None = None,
        on_popup_display_changed: Callable[[], None] | None = None,
        on_proxy_changed: Callable[[], None] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.db = db
        self._on_metered_setting_changed = on_metered_setting_changed
        self._on_excludes_changed = on_excludes_changed
        self._on_popup_display_changed = on_popup_display_changed
        self._on_proxy_changed = on_proxy_changed

        # Split into separate "General" / "Network" pages instead of one
        # long stacked column - reported directly that the settings surface
        # had gotten too long ("cok uzadi"), with the native Windows
        # OneDrive client's own tabbed Settings dialog (Settings / Account /
        # Network / About, screenshots attached) as the reference for doing
        # it "section by section." This class no longer lays itself out
        # directly (self has no layout of its own) - MainWindow places
        # general_page/network_page into its own Settings tab strip
        # alongside the Account and About tabs it builds itself, since
        # account state and static about-text aren't this class's concern.
        self.general_page = QWidget()
        general_layout = QVBoxLayout(self.general_page)
        general_layout.setContentsMargins(16, 16, 16, 16)
        general_layout.setSpacing(16)

        self.network_page = QWidget()
        network_page_layout = QVBoxLayout(self.network_page)
        network_page_layout.setContentsMargins(16, 16, 16, 16)
        network_page_layout.setSpacing(16)

        # Rebuilt as radio-button pairs (No limit / Limit to X KB/s) per
        # direction, matching the reference screenshots' structure -
        # reported directly that this didn't match yet (a download
        # bandwidth limit had been built but the matching upload one
        # hadn't). Deliberately no third
        # "Limit automatically" radio the reference also has - that means
        # actually detecting available bandwidth and adapting to it, which
        # this app has no mechanism for; asked directly and confirmed a
        # fake option for something not truly supported wasn't wanted.
        # Download/Upload placed side by side (two columns), not stacked -
        # requested directly ("upload ve download bandwith kisimlarini alt
        # alta degil yanyana kullanalim" - let's use the upload and
        # download bandwidth sections side by side, not one below the
        # other).
        bandwidth_box = QGroupBox("Bandwidth")
        bandwidth_columns = QHBoxLayout(bandwidth_box)

        download_col = QVBoxLayout()
        download_col.addWidget(QLabel("Download Bandwidth"))
        self._download_none_radio = QRadioButton("No limit")
        self._download_limit_radio = QRadioButton("Limit to:")
        self._download_group = QButtonGroup(self)
        self._download_group.addButton(self._download_none_radio)
        self._download_group.addButton(self._download_limit_radio)
        download_col.addWidget(self._download_none_radio)
        download_row = QHBoxLayout()
        download_row.addWidget(self._download_limit_radio)
        self._download_spin = self._make_limit_value_spin()
        download_row.addWidget(self._download_spin)
        download_row.addWidget(QLabel("KB/s"))
        download_col.addLayout(download_row)
        download_col.addStretch(1)
        bandwidth_columns.addLayout(download_col)

        upload_col = QVBoxLayout()
        upload_col.addWidget(QLabel("Upload Bandwidth"))
        self._upload_none_radio = QRadioButton("No limit")
        self._upload_limit_radio = QRadioButton("Limit to:")
        self._upload_group = QButtonGroup(self)
        self._upload_group.addButton(self._upload_none_radio)
        self._upload_group.addButton(self._upload_limit_radio)
        upload_col.addWidget(self._upload_none_radio)
        upload_row = QHBoxLayout()
        upload_row.addWidget(self._upload_limit_radio)
        self._upload_spin = self._make_limit_value_spin()
        upload_row.addWidget(self._upload_spin)
        upload_row.addWidget(QLabel("KB/s"))
        upload_col.addLayout(upload_row)
        upload_col.addStretch(1)
        bandwidth_columns.addLayout(upload_col)

        network_page_layout.addWidget(bandwidth_box)

        network_box = QGroupBox("Network")
        network_layout = QVBoxLayout(network_box)
        self._metered_check = QCheckBox("Automatically pause sync on metered connections")
        self._metered_check.setToolTip(
            "Detected via NetworkManager (e.g. a phone Wi-Fi hotspot marked metered). "
            "Resumes automatically once the connection is no longer metered."
        )
        network_layout.addWidget(self._metered_check)
        network_page_layout.addWidget(network_box)

        # Requested directly, with screenshots of another client's own
        # "Connection settings" panel as the reference. Three modes,
        # matching that reference exactly: "system" (default - do nothing,
        # let requests/MSAL fall back to the standard HTTP_PROXY/
        # HTTPS_PROXY environment variables), "none" (explicitly ignore
        # even those), "manual" (this app's own host/port/credentials).
        # See proxy_config.py for how this is actually applied to
        # GraphClient's session and MSAL's client - both need it
        # separately, since MSAL's own token/device-code requests don't go
        # through GraphClient's session at all.
        proxy_box = QGroupBox("Proxy")
        proxy_layout = QVBoxLayout(proxy_box)
        self._proxy_none_radio = QRadioButton("No proxy")
        self._proxy_system_radio = QRadioButton("Use system proxy")
        self._proxy_manual_radio = QRadioButton("Manually specify proxy")
        proxy_layout.addWidget(self._proxy_none_radio)
        proxy_layout.addWidget(self._proxy_system_radio)
        proxy_layout.addWidget(self._proxy_manual_radio)

        manual_row = QHBoxLayout()
        manual_row.addWidget(QLabel("Host:"))
        self._proxy_host_field = QLineEdit()
        self._proxy_host_field.setPlaceholderText("Hostname of proxy server")
        manual_row.addWidget(self._proxy_host_field, stretch=1)
        manual_row.addWidget(QLabel("Port:"))
        self._proxy_port_field = QSpinBox()
        self._proxy_port_field.setRange(1, 65535)
        self._proxy_port_field.setValue(8080)
        manual_row.addWidget(self._proxy_port_field)
        proxy_layout.addLayout(manual_row)

        self._proxy_auth_check = QCheckBox("Proxy server requires authentication")
        proxy_layout.addWidget(self._proxy_auth_check)

        auth_row = QHBoxLayout()
        self._proxy_username_field = QLineEdit()
        self._proxy_username_field.setPlaceholderText("Username for proxy server")
        self._proxy_password_field = QLineEdit()
        self._proxy_password_field.setPlaceholderText("Password for proxy server")
        self._proxy_password_field.setEchoMode(QLineEdit.EchoMode.Password)
        auth_row.addWidget(self._proxy_username_field)
        auth_row.addWidget(self._proxy_password_field)
        proxy_layout.addLayout(auth_row)

        network_page_layout.addWidget(proxy_box)
        network_page_layout.addStretch(1)

        # Requested directly, with a screenshot of another client's own
        # equivalent setting as the reference ("battery saver mode").
        # Originally just power-profiles-daemon's ActiveProfile ==
        # "power-saver" - broadened to also cover a low raw battery
        # percentage after that alone turned out not to be enough
        # (reported directly, confirmed live: this machine never actually
        # switches to a power-saver profile just from running low, so
        # sync kept going at 10%, then 7%, remaining). See power_status.py
        # for both checks - standard freedesktop.org (UPower/power-
        # profiles-daemon), not KDE-specific.
        power_box = QGroupBox("Power")
        power_layout = QVBoxLayout(power_box)
        self._battery_saver_check = QCheckBox("Automatically pause sync when battery is low or in battery saver mode")
        self._battery_saver_check.setToolTip(
            "Detected via UPower (battery percentage) and power-profiles-daemon (battery saver "
            "mode). Resumes automatically once neither condition applies."
        )
        power_layout.addWidget(self._battery_saver_check)
        general_layout.addWidget(power_box)

        # Requested directly ("Conflict varsa kullaniciya bilgi verebilir
        # miyiz? ... General altina bir ayar ekleyebiliriz belki" - can we
        # inform the user if there's a conflict, maybe as a setting under
        # General) - a sync conflict was previously only discoverable by
        # opening the Backup tab or the tray popup's activity list; this
        # setting controls the new desktop notification fired the moment
        # one is actually detected (see MainWindow._on_conflict_detected).
        notifications_box = QGroupBox("Notifications")
        notifications_layout = QVBoxLayout(notifications_box)
        self._notify_on_conflict_check = QCheckBox("Notify me when a sync conflict occurs")
        self._notify_on_conflict_check.setToolTip(
            "Shows a desktop notification when a file was changed on both this device and "
            "OneDrive at the same time and both versions had to be kept."
        )
        notifications_layout.addWidget(self._notify_on_conflict_check)
        general_layout.addWidget(notifications_box)

        # Both requested directly, as a customizable pair ("gorunsun mu
        # gorunmesin mi" - should it show or not) - the popup itself always
        # reads these live in ActivityPopup.refresh(), so toggling either
        # here updates it immediately if it's currently open.
        popup_box = QGroupBox("Tray Popup")
        popup_layout = QVBoxLayout(popup_box)
        # Renamed from "Show 'Sync now' button" - reported directly, with
        # a screenshot circling the whole row, that turning it off left
        # the status text/checkmark still showing (only the button itself
        # was hidden) - now it hides that entire row, so the name needed
        # to say that instead of promising just the button.
        self._show_status_row_check = QCheckBox("Show sync status bar")
        self._show_status_row_check.setToolTip(
            "The row showing \"All synced!\" / sync progress and the Sync now button."
        )
        self._show_status_row_check.setChecked(True)
        popup_layout.addWidget(self._show_status_row_check)
        self._show_tenant_name_check = QCheckBox("Show tenant/company name under account name")
        self._show_tenant_name_check.setChecked(True)
        popup_layout.addWidget(self._show_tenant_name_check)
        general_layout.addWidget(popup_box)

        # One global list instead of a per-pair one (requested directly:
        # add global excludes to Settings so each pair doesn't need its
        # own separate configuration), matching Nextcloud's own single
        # "Ignored Files Editor" rather than this app's previous per-pair
        # "Edit Excludes..." (removed from pairs_panel.py's row menu).
        # Applies to every Folder Pair - pair_worker.py now reads this
        # instead of a per-pair column.
        excludes_box = QGroupBox("Ignored Files")
        excludes_layout = QVBoxLayout(excludes_box)
        excludes_desc = QLabel(
            "Files and folders matching these patterns are never synced, for any Folder Pair."
        )
        excludes_desc.setWordWrap(True)
        # Was styled with "color: palette(mid)" (a muted gray meant for
        # subtle helper text) - reported directly as unreadable against
        # this group box's background on the user's theme. Plain default
        # text color, just smaller, reads reliably regardless of theme.
        excludes_desc.setStyleSheet("font-size: 11px;")
        excludes_layout.addWidget(excludes_desc)
        edit_excludes_btn = QPushButton("Edit Ignored Files…")
        edit_excludes_btn.clicked.connect(self._edit_global_excludes)
        excludes_layout.addWidget(edit_excludes_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        general_layout.addWidget(excludes_box)

        general_layout.addStretch(1)

        self._load()

        # Connected after _load() so setting initial values from saved
        # state doesn't immediately re-save them right back / fire the
        # metered-changed callback spuriously on construction.
        self._download_none_radio.toggled.connect(self._save_download_limit_mode)
        self._download_limit_radio.toggled.connect(self._save_download_limit_mode)
        self._download_spin.valueChanged.connect(self._save_download_limit_value)
        self._upload_none_radio.toggled.connect(self._save_upload_limit_mode)
        self._upload_limit_radio.toggled.connect(self._save_upload_limit_mode)
        self._upload_spin.valueChanged.connect(self._save_upload_limit_value)
        self._metered_check.toggled.connect(self._save_metered_setting)
        self._battery_saver_check.toggled.connect(self._save_battery_saver_setting)
        self._notify_on_conflict_check.toggled.connect(self._save_notify_on_conflict_setting)
        self._show_status_row_check.toggled.connect(self._save_show_status_row_setting)
        self._show_tenant_name_check.toggled.connect(self._save_show_tenant_name_setting)
        self._proxy_none_radio.toggled.connect(self._save_proxy_mode)
        self._proxy_system_radio.toggled.connect(self._save_proxy_mode)
        self._proxy_manual_radio.toggled.connect(self._save_proxy_mode)
        self._proxy_host_field.editingFinished.connect(self._save_proxy_manual_fields)
        self._proxy_port_field.editingFinished.connect(self._save_proxy_manual_fields)
        self._proxy_auth_check.toggled.connect(self._save_proxy_manual_fields)
        self._proxy_username_field.editingFinished.connect(self._save_proxy_manual_fields)
        self._proxy_password_field.editingFinished.connect(self._save_proxy_manual_fields)

    @staticmethod
    def _make_limit_value_spin() -> QSpinBox:
        # No "0 = Unlimited" special-case text anymore - unlimited is its
        # own radio now, so this spin only ever represents an actual limit.
        # Minimum of 1 keeps that meaning unambiguous.
        spin = QSpinBox()
        spin.setRange(1, 1_000_000)
        spin.setSingleStep(64)
        spin.setSuffix(" KB/s")
        return spin

    def _load(self) -> None:
        upload = self.db.get_sync_state("upload_limit_kbps")
        download = self.db.get_sync_state("download_limit_kbps")
        metered = self.db.get_sync_state("pause_on_metered")
        upload_value = int(float(upload)) if upload else 0
        download_value = int(float(download)) if download else 0
        self._upload_spin.setValue(upload_value if upload_value > 0 else 1024)
        self._download_spin.setValue(download_value if download_value > 0 else 1024)
        self._upload_limit_radio.setChecked(upload_value > 0)
        self._upload_none_radio.setChecked(upload_value <= 0)
        self._download_limit_radio.setChecked(download_value > 0)
        self._download_none_radio.setChecked(download_value <= 0)
        self._update_bandwidth_field_states()
        self._metered_check.setChecked(metered == "1")
        self._battery_saver_check.setChecked(self.db.get_sync_state("pause_on_battery_saver") == "1")
        # Default-on, same rationale as the popup-display checkboxes below -
        # a brand-new install shouldn't silently withhold a notification
        # nobody asked to turn off yet.
        self._notify_on_conflict_check.setChecked(self.db.get_sync_state("notify_on_conflict") != "0")
        # Default-on (unset means "show") for both - a brand-new install
        # shouldn't silently hide something nobody asked to hide yet.
        self._show_status_row_check.setChecked(self.db.get_sync_state("popup_show_status_row") != "0")
        self._show_tenant_name_check.setChecked(self.db.get_sync_state("popup_show_tenant_name") != "0")

        mode = self.db.get_sync_state("proxy_mode") or "system"
        {"none": self._proxy_none_radio, "system": self._proxy_system_radio, "manual": self._proxy_manual_radio}.get(
            mode, self._proxy_system_radio
        ).setChecked(True)
        self._proxy_host_field.setText(self.db.get_sync_state("proxy_host") or "")
        port = self.db.get_sync_state("proxy_port")
        self._proxy_port_field.setValue(int(port) if port else 8080)
        self._proxy_auth_check.setChecked(self.db.get_sync_state("proxy_auth_enabled") == "1")
        self._proxy_username_field.setText(self.db.get_sync_state("proxy_username") or "")
        self._proxy_password_field.setText(proxy_config.get_proxy_password())
        self._update_proxy_field_states()

    def _update_bandwidth_field_states(self) -> None:
        self._download_spin.setEnabled(self._download_limit_radio.isChecked())
        self._upload_spin.setEnabled(self._upload_limit_radio.isChecked())

    def _save_download_limit_mode(self, checked: bool) -> None:
        if not checked:
            # Each radio's toggled fires twice on a selection change (the
            # old one turning off, the new one turning on) - only the
            # "turning on" half needs to actually save anything.
            return
        value = self._download_spin.value() if self._download_limit_radio.isChecked() else 0
        self.db.set_sync_state("download_limit_kbps", str(value))
        self._update_bandwidth_field_states()

    def _save_download_limit_value(self, value: int) -> None:
        if self._download_limit_radio.isChecked():
            self.db.set_sync_state("download_limit_kbps", str(value))

    def _save_upload_limit_mode(self, checked: bool) -> None:
        if not checked:
            return
        value = self._upload_spin.value() if self._upload_limit_radio.isChecked() else 0
        self.db.set_sync_state("upload_limit_kbps", str(value))
        self._update_bandwidth_field_states()

    def _save_upload_limit_value(self, value: int) -> None:
        if self._upload_limit_radio.isChecked():
            self.db.set_sync_state("upload_limit_kbps", str(value))

    def _save_metered_setting(self, checked: bool) -> None:
        self.db.set_sync_state("pause_on_metered", "1" if checked else "0")
        if self._on_metered_setting_changed:
            self._on_metered_setting_changed()

    def _save_battery_saver_setting(self, checked: bool) -> None:
        self.db.set_sync_state("pause_on_battery_saver", "1" if checked else "0")
        if self._on_metered_setting_changed:
            # Reuses the same callback as the metered checkbox - both just
            # mean "re-evaluate whether sync should be running right now",
            # which is exactly what MainWindow._apply_sync_state() already
            # does regardless of which condition triggered it.
            self._on_metered_setting_changed()

    def _save_notify_on_conflict_setting(self, checked: bool) -> None:
        self.db.set_sync_state("notify_on_conflict", "1" if checked else "0")

    def _save_show_status_row_setting(self, checked: bool) -> None:
        self.db.set_sync_state("popup_show_status_row", "1" if checked else "0")
        if self._on_popup_display_changed:
            self._on_popup_display_changed()

    def _save_show_tenant_name_setting(self, checked: bool) -> None:
        self.db.set_sync_state("popup_show_tenant_name", "1" if checked else "0")
        if self._on_popup_display_changed:
            self._on_popup_display_changed()

    def _edit_global_excludes(self) -> None:
        current_text = self.db.get_sync_state("global_exclude_patterns") or DEFAULT_EXCLUDE_PATTERNS
        current_patterns = [p.strip() for p in current_text.splitlines() if p.strip()]
        dialog = IgnoredFilesDialog(current_patterns, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.db.set_sync_state("global_exclude_patterns", "\n".join(dialog.patterns))
        if self._on_excludes_changed:
            self._on_excludes_changed()

    def _update_proxy_field_states(self) -> None:
        manual = self._proxy_manual_radio.isChecked()
        self._proxy_host_field.setEnabled(manual)
        self._proxy_port_field.setEnabled(manual)
        self._proxy_auth_check.setEnabled(manual)
        auth_fields_enabled = manual and self._proxy_auth_check.isChecked()
        self._proxy_username_field.setEnabled(auth_fields_enabled)
        self._proxy_password_field.setEnabled(auth_fields_enabled)

    def _save_proxy_mode(self, checked: bool) -> None:
        if not checked:
            # Each radio's toggled fires twice on a selection change (the
            # old one turning off, the new one turning on) - only the
            # "turning on" half needs to actually save anything.
            return
        mode = "none" if self._proxy_none_radio.isChecked() else (
            "manual" if self._proxy_manual_radio.isChecked() else "system"
        )
        self.db.set_sync_state("proxy_mode", mode)
        self._update_proxy_field_states()
        if self._on_proxy_changed:
            self._on_proxy_changed()

    def _save_proxy_manual_fields(self) -> None:
        self.db.set_sync_state("proxy_host", self._proxy_host_field.text().strip())
        self.db.set_sync_state("proxy_port", str(self._proxy_port_field.value()))
        self.db.set_sync_state("proxy_auth_enabled", "1" if self._proxy_auth_check.isChecked() else "0")
        self.db.set_sync_state("proxy_username", self._proxy_username_field.text().strip())
        proxy_config.set_proxy_password(self._proxy_password_field.text())
        self._update_proxy_field_states()
        if self._on_proxy_changed:
            self._on_proxy_changed()
