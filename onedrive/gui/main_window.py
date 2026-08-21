import logging
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QEvent, QObject, QPoint, QSize, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QSystemTrayIcon,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from onedrive import constants, network_status, power_status, proxy_config, update_check
from onedrive.auth import AuthManager
from onedrive.content_cache import ContentCache
from onedrive.db import Database
from onedrive.dolphin_integration import add_places_bookmark, install_overlay_emblem_icons
from onedrive.dolphin_overlay_server import OverlayServer
from onedrive.fuse.mount import is_mounted, recover_stale_mount, start_mount, stop_mount
from onedrive.fuse.operations import OneDriveOperations
from onedrive.graph_client import GraphClient
from onedrive.gui.login_dialog import DeviceCodeDialog
from onedrive.gui.activity_popup import ActivityPopup
from onedrive.gui.pairs_panel import PairsPanel
from onedrive.gui.manage_access_dialog import ManageAccessDialog
from onedrive.gui.share_dialog import ShareDialog
from onedrive.gui.sync_problems_dialog import SyncProblemsDialog
from onedrive.gui.conflicts_dialog import ConflictsDialog
from onedrive.gui.settings_panel import SettingsPanel
from onedrive.gui.x11_hints import skip_taskbar
from onedrive.gui.theme import (
    BRAND_BLUE,
    BRAND_BLUE_DARK,
    app_icon,
    gray_tray_icon,
    paused_tray_icon,
    initials_avatar,
    photo_avatar,
)
from onedrive.sync.delta_worker import DeltaSyncWorker
from onedrive.sync.mount_sync_worker import MountSyncWorker
from onedrive.sync.pair_worker import PairSyncWorker
from onedrive.sync.pin_worker import PinWorker
from onedrive.sync.conflict_actions import resolve_mount_conflict

logger = logging.getLogger(__name__)

_ITEM_ID_ROLE = Qt.ItemDataRole.UserRole
_LOADED_ROLE = Qt.ItemDataRole.UserRole + 1

# Kept as module-level aliases so the rest of this file's string-formatted
# stylesheets below don't need to change.
_BRAND_BLUE = BRAND_BLUE
_BRAND_BLUE_DARK = BRAND_BLUE_DARK
_initials_avatar = initials_avatar


class WorkerSignals(QObject):
    status_changed = pyqtSignal(str)
    pair_status_changed = pyqtSignal(int, str)
    auth_required = pyqtSignal()
    avatar_ready = pyqtSignal(bytes)
    display_name_ready = pyqtSignal(str)
    tenant_name_ready = pyqtSignal(str)
    conflict_detected = pyqtSignal(str, str)
    manage_access_requested = pyqtSignal(str, str)
    share_requested = pyqtSignal(str, str)
    update_check_result = pyqtSignal(str, str)  # status ("uptodate"/"available"/"error"), message
    update_apply_result = pyqtSignal(bool, str)  # success, message


class MainWindow(QMainWindow):
    def __init__(self):
        # Qt.WindowType.Tool - no separate taskbar entry. Requested directly
        # after a first attempt at solving this a different way (auto-hide
        # the window on click-away while keeping a normal taskbar entry -
        # correctly working, but not actually what was wanted): "I dont
        # want to see OneDrive icon on taskbar when I click on somewhere
        # else. I just want to see OneDrive icon in system tray." The
        # simpler, correct fix - just never have a taskbar entry at all,
        # the same way the tray popup already doesn't - not "hide it
        # correctly when unfocused." No focus-loss-triggered close/hide
        # logic here (that class of bug already bit the popup once when
        # Tool was tried there): this window still opens/closes exactly
        # like before (double-click tray icon to open, X button hides it
        # to the tray rather than quitting, per closeEvent below) - the
        # only change is it no longer claims its own taskbar slot while
        # doing so.
        #
        # Tool alone turned out not to be enough on its own - confirmed
        # directly, live: `xprop` showed Qt sets _NET_WM_WINDOW_TYPE to
        # BOTH _UTILITY and a _NORMAL fallback for a Tool window that still
        # has native decorations, and Plasma's Task Manager widget here
        # respects that _NORMAL fallback and shows it anyway. Also tested
        # Qt.WindowType.Dialog directly, live - same result, still shown.
        # FramelessWindowHint was ALSO not enough by itself, in the end -
        # reported directly, still showing after that fix too. Even with
        # Tool | FramelessWindowHint (the exact combination the tray popup
        # itself uses without issue), `xprop` on the live main window kept
        # showing the same _UTILITY + _NORMAL fallback pair, and Plasma's
        # Task Manager evidently keys off the _NORMAL fallback regardless
        # of window flags. The actual fix is x11_hints.skip_taskbar(),
        # called below after this widget exists - it sets
        # _NET_WM_STATE_SKIP_TASKBAR directly (the real, purpose-built EWMH
        # mechanism for this, independent of window TYPE), confirmed
        # working via a live side-by-side test window against the real
        # taskbar. Tool | FramelessWindowHint is kept regardless (still
        # correct for skipping the pager/alt-tab and matching the popup's
        # own look), it's just not sufficient alone here.
        #
        # Frameless drops the native title bar, so the header below picks
        # up a close button and drag-to-move (see eventFilter) to replace
        # what that used to provide.
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(constants.DISPLAY_NAME)
        self.resize(700, 620)
        skip_taskbar(int(self.winId()))

        self.db = Database()
        initial_proxies, initial_trust_env = proxy_config.get_proxy_settings(self.db)
        self.auth = AuthManager(proxies=initial_proxies)
        self.graph = GraphClient(
            self.auth,
            upload_limit_kbps_getter=lambda: self._read_limit_kbps("upload_limit_kbps"),
            download_limit_kbps_getter=lambda: self._read_limit_kbps("download_limit_kbps"),
            proxies=initial_proxies,
            trust_env=initial_trust_env,
        )
        self.content_cache = ContentCache(self.db, self.graph)
        self.drive_id: str | None = self.db.get_sync_state("drive_id")

        self.delta_worker: DeltaSyncWorker | None = None
        self.pin_worker: PinWorker | None = None
        self.pair_worker: PairSyncWorker | None = None
        self.mount_sync_worker: MountSyncWorker | None = None
        self.fuse_operations: OneDriveOperations | None = None
        self._mount_thread = None
        saved_mountpoint = self.db.get_sync_state("last_mountpoint")
        self._mountpoint = Path(saved_mountpoint) if saved_mountpoint else constants.DEFAULT_MOUNTPOINT

        self._signals = WorkerSignals()
        self._signals.status_changed.connect(self._on_status_changed)
        self._signals.pair_status_changed.connect(self._on_pair_status_changed)
        self._signals.auth_required.connect(self._prompt_reconsent)
        self._signals.avatar_ready.connect(self._on_avatar_ready)
        self._signals.display_name_ready.connect(self._on_display_name_ready)
        self._signals.tenant_name_ready.connect(self._on_tenant_name_ready)
        self._signals.conflict_detected.connect(self._on_conflict_detected)
        self._signals.manage_access_requested.connect(self._open_manage_access_dialog)
        self._signals.share_requested.connect(self._open_share_dialog)
        self._signals.update_check_result.connect(self._on_update_check_result)
        self._signals.update_apply_result.connect(self._on_update_apply_result)
        self._quitting = False

        # Independent of sign-in/mount state (like auto-mount itself) - the
        # Dolphin overlay-icon plugin can start querying paths the moment
        # Dolphin opens a window, and correctly just gets "NONE" back for
        # everything until there's a real drive_id/mount to answer against.
        self.overlay_server = OverlayServer(
            self.db, mountpoint_getter=lambda: self._mountpoint, pin_worker_getter=lambda: self.pin_worker,
            graph_client=self.graph, on_manage_access=self._signals.manage_access_requested.emit,
            on_share=self._signals.share_requested.emit,
        )
        self.overlay_server.start()
        install_overlay_emblem_icons()

        # Loaded synchronously here (a small local file read) so the very
        # first paint already has a real photo if one was already fetched in
        # a previous run, rather than flashing the initials fallback for a
        # moment every single startup while _refresh_account_info_async's
        # network call is still in flight.
        self._avatar_photo_bytes: bytes | None = None
        if constants.PROFILE_PHOTO_FILE.exists():
            try:
                self._avatar_photo_bytes = constants.PROFILE_PHOTO_FILE.read_bytes()
            except OSError:
                logger.debug("failed to read cached profile photo", exc_info=True)
        # Same idea for the real Graph display name ("Hasan Altin") - cached
        # as plain text in sync_state rather than needing its own file.
        self._display_name: str | None = self.db.get_sync_state("display_name")
        # And the tenant/company name (e.g. "Contoso Ltd") shown under it
        # in the tray popup - requested directly.
        self._tenant_name: str | None = self.db.get_sync_state("tenant_name")
        self._sync_paused: bool = self.db.get_sync_state("sync_paused") == "1"
        # Live network state, not persisted - starts optimistic (not
        # metered, online) rather than blocking startup on a synchronous
        # gdbus call; a quick singleShot check plus the repeating timer
        # below (both set up after _build_tray_icon()) correct these
        # within seconds if either assumption was wrong.
        self._metered_now: bool = False
        self._online: bool = True
        self._battery_low_now: bool = False
        self._reconsent_prompted = False
        self._auto_mount_attempted = False

        # Status/pairs updates can fire many times per second during a bulk
        # sync (once per file). Rebuilding the UI - and especially
        # recomputing cache size, a full disk walk - on every single one of
        # those froze the app outright ("Not Responding"). Everything here
        # is coalesced: rapid bursts collapse into one UI update via a short
        # timer, and cache size is computed in a background thread on its
        # own slow timer instead of synchronously on the GUI thread.
        self._pending_status_message: str | None = None
        self._status_update_timer = QTimer(self)
        self._status_update_timer.setSingleShot(True)
        self._status_update_timer.timeout.connect(self._apply_pending_status_update)

        self._pair_refresh_timer = QTimer(self)
        self._pair_refresh_timer.setSingleShot(True)

        self._popup_refresh_timer = QTimer(self)
        self._popup_refresh_timer.setSingleShot(True)
        self._popup_refresh_timer.timeout.connect(self._refresh_popup_if_visible)

        self._cache_size_bytes = 0
        self._cache_size_lock = threading.Lock()
        self._cache_size_timer = QTimer(self)
        self._cache_size_timer.timeout.connect(self._refresh_cache_size_async)
        self._cache_size_timer.start(5000)
        self._refresh_cache_size_async()

        self._build_ui()
        self._build_tray_icon()
        self._refresh_account_ui()

        # Periodic network-status check: metered connection (requested
        # directly - "metered connection settings", implemented as
        # auto-pause/resume through _apply_sync_state, same reconciliation
        # point as manual pause) and online/offline (requested directly -
        # "when it is online lets have a blue icon, when it is offline or
        # paused show gray icon"). One timer for both since they're both
        # NetworkManager D-Bus queries. 30s interval, plus one quick check
        # shortly after startup instead of waiting a full interval for the
        # first classification - queried fresh each time (deliberately
        # uncached; the active connection can change while this app keeps
        # running, e.g. switching to a phone hotspot).
        self._network_check_timer = QTimer(self)
        self._network_check_timer.setInterval(30_000)
        self._network_check_timer.timeout.connect(self._check_network_status)
        self._network_check_timer.start()
        QTimer.singleShot(2000, self._check_network_status)

        if self.auth.is_signed_in and self.drive_id:
            # A user who paused sync before quitting shouldn't have it
            # silently resume just from reopening the app - the mount
            # itself (below) is unaffected either way, since browsing is
            # pure local-cache reads independent of whether the background
            # sync workers are running.
            self._apply_sync_state()
            self._populate_folder_tree()
            # Previously this only ever ran from _on_status_changed's "Idle"
            # case, i.e. only after DeltaSyncWorker completed a real network
            # sync - so a returning user with a fully populated local cache
            # from earlier sessions couldn't get their on-demand mount back
            # at all while offline (or even just while the network was still
            # coming up at boot), despite having every byte of metadata
            # needed to serve it already sitting in the DB. _mount() itself
            # is pure local reads - nothing here depends on network.
            self._maybe_auto_mount()
            self._refresh_account_info_async()

    # --- UI construction ----------------------------------------------

    def _build_ui(self) -> None:
        outer = QWidget()
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # --- branded header bar: avatar, account identity, sign in/out ---
        header = QFrame()
        header.setObjectName("headerBar")
        header.setStyleSheet(
            f"""
            QFrame#headerBar {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {_BRAND_BLUE}, stop:1 {_BRAND_BLUE_DARK});
            }}
            QFrame#headerBar QLabel {{ color: white; background: transparent; }}
            QFrame#headerBar QPushButton {{
                background: rgba(255,255,255,30);
                color: white;
                border: 1px solid rgba(255,255,255,90);
                border-radius: 4px;
                padding: 6px 14px;
            }}
            QFrame#headerBar QPushButton:hover {{ background: rgba(255,255,255,55); }}
            """
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 14, 18, 14)
        header_layout.setSpacing(12)

        self._avatar_label = QLabel()
        self._avatar_label.setFixedSize(44, 44)
        header_layout.addWidget(self._avatar_label)

        identity_col = QVBoxLayout()
        # Was 2 - reported directly as too much space between the name and
        # the tenant line under it (same complaint, and same fix, as the
        # tray popup's own header).
        identity_col.setSpacing(0)
        self._account_label = QLabel("Not signed in")
        title_font = self._account_label.font()
        title_font.setPointSize(title_font.pointSize() + 1)
        title_font.setWeight(QFont.Weight.DemiBold)
        self._account_label.setFont(title_font)
        identity_col.addWidget(self._account_label)
        self._account_sublabel = QLabel(constants.DISPLAY_NAME)
        sub_font = self._account_sublabel.font()
        sub_font.setPointSize(max(8, sub_font.pointSize() - 1))
        self._account_sublabel.setFont(sub_font)
        self._account_sublabel.setStyleSheet("color: rgba(255,255,255,190);")
        identity_col.addWidget(self._account_sublabel)
        self._quota_label = QLabel("")
        self._quota_label.setFont(sub_font)
        self._quota_label.setStyleSheet("color: rgba(255,255,255,190);")
        identity_col.addWidget(self._quota_label)
        header_layout.addLayout(identity_col, stretch=1)

        # Sign in/out moved to the Settings tab (requested directly) - see
        # the Settings tab's Account box below. self._signin_btn still
        # lives on self (built there instead), _refresh_account_ui updates
        # it regardless of which tab is currently visible.

        # Frameless (see __init__'s window-flags comment) means no native
        # title bar - this header is the only place left to close or drag
        # the window from, so it needs to actually do both now. Same
        # pattern the tray popup's own header already uses.
        close_btn = QPushButton("✕")
        close_btn.setFlat(True)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFixedWidth(28)
        # Explicit stylesheet, not just inherited from
        # QFrame#headerBar QPushButton above - that one's `padding: 6px
        # 14px` (28px of horizontal padding alone) exactly ate the entire
        # fixed 28px width of this button, leaving no room to actually
        # draw the "✕" glyph - it was there, just squeezed to zero visible
        # space (reported directly, with a screenshot: the button's outline
        # showed, the X didn't). Same tight padding the tray popup's own
        # close button already uses without this problem.
        close_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: white; "
            "font-size: 13px; padding: 4px; border-radius: 4px; }"
            "QPushButton:hover { background: rgba(255, 255, 255, 40); }"
        )
        close_btn.clicked.connect(self.close)
        header_layout.addWidget(close_btn)

        header.setCursor(Qt.CursorShape.SizeAllCursor)
        header.installEventFilter(self)
        self._header = header
        self._drag_offset: QPoint | None = None

        outer_layout.addWidget(header)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            """
            QTabWidget::pane { border: none; }
            QTabBar::tab {
                padding: 9px 20px;
                margin-right: 2px;
                background: palette(window);
                border: none;
                border-bottom: 2px solid transparent;
            }
            QTabBar::tab:selected {
                border-bottom: 2px solid #0364B8;
                font-weight: 600;
            }
            QTabBar::tab:hover:!selected { background: palette(midlight); }
            """
        )
        content_margin = QWidget()
        content_layout = QVBoxLayout(content_margin)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.addWidget(tabs)
        outer_layout.addWidget(content_margin, stretch=1)

        # --- "Choose folders" dialog: the on-demand mount's pin tree - moved
        # out of a permanent top-level "Browse" tab into its own dialog,
        # opened from the new Account tab below, mirroring the native
        # Windows OneDrive client's own Account tab ("1 location is
        # syncing" / "Choose folders" / "Stop sync", screenshots attached).
        # Requested directly: "Browse ile Account kismi birlesebilir gibi"
        # (Browse and Account could merge). Built once, not lazily per
        # open - _populate_folder_tree() is already invoked from
        # background-thread signal handlers regardless of whether this
        # dialog is currently visible, same as when this was a tab.
        self._choose_folders_dialog = QDialog(self)
        self._choose_folders_dialog.setWindowTitle("Choose folders")
        self._choose_folders_dialog.resize(480, 520)
        choose_folders_layout = QVBoxLayout(self._choose_folders_dialog)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Folder", "Always keep on this device"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._populating = False
        choose_folders_layout.addWidget(self._tree, stretch=1)

        choose_folders_buttons = QHBoxLayout()
        refresh_btn = QPushButton("Refresh now")
        refresh_btn.clicked.connect(self._on_refresh_clicked)
        choose_folders_buttons.addWidget(refresh_btn)
        choose_folders_buttons.addStretch(1)
        close_folders_btn = QPushButton("Close")
        close_folders_btn.clicked.connect(self._choose_folders_dialog.close)
        choose_folders_buttons.addWidget(close_folders_btn)
        choose_folders_layout.addLayout(choose_folders_buttons)

        # --- One flat top-level tab strip: General / Account / Backup /
        # Network / About - reported directly not to nest Settings' own
        # sub-tabs inside a separate outer "Settings" tab ("Bu tab menuleri
        # bir birinden ayirmayalim" - don't separate these tab menus from
        # each other), with the exact tab order/names spelled out too. The
        # Folder Pairs tab itself is labeled plain "Backup" (further
        # simplified from "Folder Pairs (Use Backups)" per a follow-up
        # request) since it's this app's actual equivalent of the
        # reference client's separate "Backup" tab (Desktop/Documents/
        # Pictures are just three ordinary Folder Pairs here, not a
        # distinct feature) rather than adding an empty tab with no real
        # function behind it.
        self._settings_panel = SettingsPanel(
            self.db,
            on_metered_setting_changed=self._apply_sync_state,
            on_excludes_changed=self._on_global_excludes_changed,
            on_popup_display_changed=self._on_popup_display_changed,
            on_proxy_changed=self._on_proxy_settings_changed,
        )
        # General tab's addTab() call is deliberately deferred until after
        # the Shared tab below (requested directly: "general tabini shared
        # tabindan sonra goster") - the page itself is still built here so
        # every other tab's construction order/comments above stay
        # unchanged.

        # Sign in/out moved here from the header bar (requested directly) -
        # the header keeps just the identity display (avatar/name/quota).
        # Redesigned again to match the reference screenshot's own Account
        # tab structure: a storage/quota box with a "Manage storage" link,
        # and a sync-locations box (this app only ever has the one
        # on-demand mount "location") with the Mount/Unmount toggle and a
        # "Choose folders" link opening the dialog built above - this is
        # also where the old standalone "Browse" tab's mount path field and
        # Browse... button ended up living now.
        account_tab = QWidget()
        account_tab_layout = QVBoxLayout(account_tab)
        account_tab_layout.setContentsMargins(16, 16, 16, 16)
        account_tab_layout.setSpacing(16)

        storage_box = QGroupBox("OneDrive")
        storage_layout = QVBoxLayout(storage_box)
        self._account_status_label = QLabel("Not signed in")
        status_font = self._account_status_label.font()
        status_font.setWeight(QFont.Weight.DemiBold)
        self._account_status_label.setFont(status_font)
        storage_layout.addWidget(self._account_status_label)
        self._account_quota_label = QLabel("")
        storage_layout.addWidget(self._account_quota_label)
        storage_links_row = QHBoxLayout()
        manage_storage_link = QLabel('<a href="#">Manage storage</a>')
        manage_storage_link.linkActivated.connect(self._on_manage_storage_clicked)
        storage_links_row.addWidget(manage_storage_link)
        storage_links_row.addStretch(1)
        self._signin_btn = QPushButton("Sign in")
        self._signin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._signin_btn.clicked.connect(self._on_signin_clicked)
        storage_links_row.addWidget(self._signin_btn)
        storage_layout.addLayout(storage_links_row)
        account_tab_layout.addWidget(storage_box)

        location_box = QGroupBox("Sync Locations")
        location_layout = QVBoxLayout(location_box)
        location_layout.addWidget(QLabel("OneDrive (on-demand mount)"))
        mount_row = QHBoxLayout()
        self._mount_field = QLineEdit(str(self._mountpoint))
        mount_row.addWidget(self._mount_field, stretch=1)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._on_browse_clicked)
        mount_row.addWidget(browse_btn)
        self._mount_btn = QPushButton("Mount")
        self._mount_btn.clicked.connect(self._on_mount_clicked)
        self._mount_btn.setEnabled(False)
        mount_row.addWidget(self._mount_btn)
        location_layout.addLayout(mount_row)
        self._account_cache_size_label = QLabel("")
        location_layout.addWidget(self._account_cache_size_label)
        # Piggybacks on the same 5s timer that already drives the status
        # bar's own cache-size figure (_refresh_cache_size_async computes
        # it on a background thread) - this just reads whatever
        # self._cache_size_bytes the last completed pass left behind,
        # same staleness (up to one tick) the status bar already accepts.
        self._cache_size_timer.timeout.connect(self._update_account_cache_size_label)
        self._update_account_cache_size_label()
        choose_folders_row = QHBoxLayout()
        choose_folders_link = QLabel('<a href="#">Choose folders</a>')
        choose_folders_link.linkActivated.connect(self._open_choose_folders_dialog)
        choose_folders_row.addWidget(choose_folders_link)
        choose_folders_row.addStretch(1)
        location_layout.addLayout(choose_folders_row)

        # Surfaces pending_mount_ops rows that have failed at least once
        # and are still silently retrying forever - requested directly as
        # the top-priority fix after two such operations were found stuck
        # with zero visibility anywhere in the app ("1 numara cok onemli").
        # Hidden entirely when there's nothing to show, same as the
        # per-pair "N conflicts to review" note in the Backup tab.
        self._sync_problems_link = QLabel("")
        self._sync_problems_link.setStyleSheet("color: #C0392B;")
        self._sync_problems_link.linkActivated.connect(self._open_sync_problems_dialog)
        self._sync_problems_link.hide()
        location_layout.addWidget(self._sync_problems_link)
        self._cache_size_timer.timeout.connect(self._update_sync_problems_indicator)
        self._update_sync_problems_indicator()

        # Mount-sourced conflicts (see mount_sync_worker's keep-both
        # resolution) previously had no way to act on them at all - they
        # only ever showed up as a "conflict" line in the Recent Activity
        # popup, unlike Folder Pairs' own per-pair "View N Conflicts…"
        # menu entry. Same visibility rule as the sync-problems link above:
        # hidden entirely when count_conflicts("mount") is 0.
        self._mount_conflicts_link = QLabel("")
        self._mount_conflicts_link.setStyleSheet("color: #C0392B;")
        self._mount_conflicts_link.linkActivated.connect(self._open_mount_conflicts_dialog)
        self._mount_conflicts_link.hide()
        location_layout.addWidget(self._mount_conflicts_link)
        self._cache_size_timer.timeout.connect(self._update_mount_conflicts_indicator)
        self._update_mount_conflicts_indicator()

        account_tab_layout.addWidget(location_box)

        account_tab_layout.addStretch(1)
        tabs.addTab(account_tab, "Account")

        # --- "Backup" tab: two-way sync of arbitrary local<->remote
        # folders (still PairsPanel/Folder Pairs under the hood) - tab
        # label simplified from "Folder Pairs (Use Backups)" to just
        # "Backup" (requested directly: "Folder Pairs kismini sadece
        # Backup olarak kullanalim" - let's use the Folder Pairs part only
        # as Backup) rather than naming both the general mechanism and its
        # backup use case in the same label. ---
        self._pairs_panel = PairsPanel(
            self.db, self.graph, lambda: self.drive_id, lambda: self.pair_worker
        )
        self._pair_refresh_timer.timeout.connect(self._pairs_panel.refresh)
        tabs.addTab(self._pairs_panel, "Backup")

        tabs.addTab(self._settings_panel.general_page, "General")

        tabs.addTab(self._settings_panel.network_page, "Network")

        # Same version/publisher/license text as the tray menu's "About"
        # dialog (_show_about()) - shown inline here too now that Settings
        # has its own tab strip to put it in, instead of only reachable as
        # a popup.
        about_tab = QWidget()
        about_tab_layout = QVBoxLayout(about_tab)
        about_tab_layout.setContentsMargins(16, 16, 16, 16)
        about_label = QLabel(
            f"<b>{constants.DISPLAY_NAME}</b><br>"
            f"Version {constants.VERSION}<br><br>"
            "Publisher: Hasan Altin<br>"
            '<a href="https://hasanaltin.com">hasanaltin.com</a><br><br>'
            '<a href="https://github.com/hasanaltin/OneDrive">github.com/hasanaltin/OneDrive</a><br><br>'
            "License: MIT"
        )
        about_label.setTextFormat(Qt.TextFormat.RichText)
        about_label.setOpenExternalLinks(True)
        about_tab_layout.addWidget(about_label)

        update_row = QHBoxLayout()
        self._update_check_btn = QPushButton("Check for Updates")
        self._update_check_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_check_btn.clicked.connect(self._on_check_for_updates_clicked)
        update_row.addWidget(self._update_check_btn)
        self._update_apply_btn = QPushButton("Update Now")
        self._update_apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_apply_btn.clicked.connect(self._on_apply_update_clicked)
        self._update_apply_btn.hide()
        update_row.addWidget(self._update_apply_btn)
        update_row.addStretch(1)
        about_tab_layout.addLayout(update_row)
        self._update_status_label = QLabel("")
        self._update_status_label.setWordWrap(True)
        about_tab_layout.addWidget(self._update_status_label)

        about_tab_layout.addStretch(1)
        tabs.addTab(about_tab, "About")

        self.setCentralWidget(outer)
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Not signed in")

    def _build_tray_icon(self) -> None:
        self.setWindowIcon(app_icon(64))

        self._tray = QSystemTrayIcon(self)
        self._update_tray_icon()  # reflects self._sync_paused from the moment it's created

        # Right-click gives quick app-level actions (Open, About, Exit) -
        # distinct from the left-click popup's own account menu, matching
        # the standard tray-icon convention (requested directly - "if you
        # show this About page only when user make a right click icon
        # user can see like this Open OneDrive, About and Exit options").
        menu = QMenu()
        open_action = QAction("Open OneDrive", self)
        open_action.triggered.connect(self._show_window)
        menu.addAction(open_action)
        menu.addSeparator()
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        menu.addAction(about_action)
        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit_app)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

        self._activity_popup = ActivityPopup(
            self.db,
            lambda: self._mountpoint,
            self._show_window,
            on_sync_now=self._on_refresh_clicked,
            username_getter=lambda: self.auth.account_username or "",
            display_name_getter=lambda: self._display_name,
            tenant_name_getter=lambda: self._tenant_name,
            web_url_getter=self._get_drive_web_url,
            avatar_bytes_getter=lambda: self._avatar_photo_bytes,
            on_exit=self._quit_app,
            sync_paused_getter=lambda: self._sync_paused,
            on_toggle_sync=self._toggle_sync_paused,
            show_status_row_getter=lambda: self.db.get_sync_state("popup_show_status_row") != "0",
            show_tenant_name_getter=lambda: self.db.get_sync_state("popup_show_tenant_name") != "0",
            sync_active_getter=lambda: self.pair_worker is not None,
            drive_id_getter=lambda: self.drive_id,
            parent=self,
        )

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            from PyQt6.QtGui import QCursor

            # Both QSystemTrayIcon.geometry() and QCursor.pos() have proven
            # unreliable for vertical placement on this desktop (the popup
            # has landed mid-screen and even at the very top of the screen
            # on different attempts) - show_near() no longer trusts either
            # for y, only for x, so which one is passed here barely matters
            # anymore; cursor position is simplest.
            self._activity_popup.show_near(QCursor.pos())
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self) -> None:
        self._center_on_screen()
        self.show()
        # Re-asserted on every show, not just the initial one in __init__ -
        # the ClientMessage form of skip_taskbar() is what actually takes
        # effect for a window being re-shown after a hide (the direct
        # property-set form only reliably applies on first map).
        skip_taskbar(int(self.winId()))
        self.raise_()
        self.activateWindow()

    def _center_on_screen(self) -> None:
        """Requested directly - without this the window just opens
        wherever the WM's default placement (or a previous manual drag via
        the header, see eventFilter) happened to leave it, which isn't
        necessarily centered at all."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        self.move(avail.center().x() - self.width() // 2, avail.center().y() - self.height() // 2)

    # --- account / sign-in ----------------------------------------------

    def _refresh_account_ui(self) -> None:
        if self.auth.is_signed_in:
            username = self.auth.account_username or "signed in"
            self._account_label.setText(self._display_name or username.split("@")[0])
            # Reverted back to plain email - a 0.4.58 change swapped this
            # for the tenant name (matching the tray popup) but that wasn't
            # actually wanted here ("ayarlardaki mail kalabilirdi" - the
            # email could have stayed) and, unlike the popup's version,
            # this label had no eliding, so a long legal tenant name just
            # overflowed the header instead of looking clean.
            self._account_sublabel.setText(username)
            photo = photo_avatar(self._avatar_photo_bytes, 44) if self._avatar_photo_bytes else None
            self._avatar_label.setPixmap(photo or _initials_avatar(self._display_name or username))
            self._signin_btn.setText("Sign out")
            self._account_status_label.setText(f"Signed in as {self._display_name or username}")
            self._mount_btn.setEnabled(True)
            self._quota_label.setText(self._format_quota())
            self._account_quota_label.setText(self._format_quota())
        else:
            self._account_label.setText("Not signed in")
            self._account_sublabel.setText(constants.DISPLAY_NAME)
            self._avatar_label.setPixmap(_initials_avatar("?"))
            self._signin_btn.setText("Sign in")
            self._account_status_label.setText("Not signed in")
            self._mount_btn.setEnabled(False)
            self._quota_label.setText("")
            self._account_quota_label.setText("")

    def _format_quota(self) -> str:
        total = self.db.get_sync_state("drive_quota_total")
        remaining = self.db.get_sync_state("drive_quota_remaining")
        if not total or not remaining:
            return ""
        total_bytes, remaining_bytes = int(total), int(remaining)
        if total_bytes <= 0:
            return ""
        used_gb = (total_bytes - remaining_bytes) / (1024 ** 3)
        total_gb = total_bytes / (1024 ** 3)
        return f"{used_gb:.1f} GB of {total_gb:.1f} GB used"

    def _on_signin_clicked(self) -> None:
        if self.auth.is_signed_in:
            self._sign_out()
            return
        dialog = DeviceCodeDialog(self.auth, self)
        dialog.exec()
        if dialog.success:
            self._after_sign_in()
        self._refresh_account_ui()

    def _after_sign_in(self) -> None:
        try:
            drive = self.graph.get_drive()
        except Exception as e:
            QMessageBox.warning(self, "Sign-in", f"Signed in, but couldn't fetch drive info yet: {e}")
            return
        self.drive_id = drive["id"]
        self.db.set_sync_state("drive_id", self.drive_id)
        if drive.get("webUrl"):
            self.db.set_sync_state("drive_web_url", drive["webUrl"])
        quota = drive.get("quota", {})
        self.db.set_sync_state("drive_quota_total", str(quota.get("total", 0)))
        self.db.set_sync_state("drive_quota_remaining", str(quota.get("remaining", 0)))
        self._sync_paused = False
        self.db.set_sync_state("sync_paused", "0")
        self._start_background_workers()
        self._populate_folder_tree()
        self._refresh_account_info_async()

    def _get_drive_web_url(self) -> str | None:
        """The signed-in drive's own webUrl - work/school accounts live
        under a tenant SharePoint domain, not the consumer onedrive.live.com,
        so "View online" has to use the account's real URL, not a hardcoded
        one. Cached in sync_state after the first lookup."""
        cached = self.db.get_sync_state("drive_web_url")
        if cached:
            return cached
        if not self.drive_id:
            return None
        try:
            drive = self.graph.get_drive()
        except Exception:
            return None
        url = drive.get("webUrl")
        if url:
            self.db.set_sync_state("drive_web_url", url)
        return url

    def _on_manage_storage_clicked(self) -> None:
        url = self._get_drive_web_url()
        if url:
            QDesktopServices.openUrl(QUrl(url))
        else:
            QMessageBox.information(
                self, "Manage storage", "Sign in first to open your OneDrive storage page."
            )

    def _open_choose_folders_dialog(self) -> None:
        self._choose_folders_dialog.show()
        self._choose_folders_dialog.raise_()
        self._choose_folders_dialog.activateWindow()

    def _sign_out(self) -> None:
        if is_mounted(self._mountpoint):
            self._unmount()
        self._stop_background_workers()
        self.auth.sign_out()
        self.drive_id = None
        self._tree.clear()
        self._status_bar.showMessage("Signed out")

    # --- background workers -----------------------------------------------

    def _start_background_workers(self) -> None:
        if self.delta_worker is not None:
            return
        self.delta_worker = DeltaSyncWorker(
            self.db,
            self.graph,
            self.drive_id,
            on_status=self._signals.status_changed.emit,
            on_auth_required=self._signals.auth_required.emit,
        )
        self.pin_worker = PinWorker(self.db, self.content_cache)
        self.pair_worker = PairSyncWorker(
            self.db,
            self.graph,
            self.drive_id,
            on_status=self._signals.pair_status_changed.emit,
            on_auth_required=self._signals.auth_required.emit,
            on_conflict=self._signals.conflict_detected.emit,
        )
        self.mount_sync_worker = MountSyncWorker(
            self.db,
            self.graph,
            self.drive_id,
            on_auth_required=self._signals.auth_required.emit,
            on_conflict=self._signals.conflict_detected.emit,
        )
        self.delta_worker.start()
        self.pin_worker.start()
        self.pair_worker.start()
        self.mount_sync_worker.start()

    def _prompt_reconsent(self) -> None:
        if self._reconsent_prompted:
            return
        self._reconsent_prompted = True
        self._show_window()
        QMessageBox.information(
            self,
            "Sign-in required",
            "OneDrive needs you to sign in again to grant the new permissions "
            "this app now needs (read and write access).",
        )
        dialog = DeviceCodeDialog(self.auth, self)
        dialog.exec()
        self._reconsent_prompted = False
        self._refresh_account_ui()

    def _stop_background_workers(self) -> None:
        # .stop() only sets a flag - a worker can still be mid-operation
        # (a network call, a db write) for a little while after this
        # returns. An earlier version of this method joined each thread
        # here to close that window before db.close() ran - reverted: the
        # systemd unit's own TimeoutStopUSec is 5s, well under a single
        # worker's join(timeout=10), so on quit systemd was SIGKILLing the
        # process mid-join before it could even finish, which just traded
        # one race for a worse one. See closeEvent for the actual fix -
        # not calling db.close() at all - which makes this race harmless
        # instead of trying to outrun it.
        if self.delta_worker:
            self.delta_worker.stop()
            self.delta_worker = None
        if self.pin_worker:
            self.pin_worker.stop()
            self.pin_worker = None
        if self.pair_worker:
            self.pair_worker.stop()
            self.pair_worker = None
        if self.mount_sync_worker:
            self.mount_sync_worker.stop()
            self.mount_sync_worker = None

    def _toggle_sync_paused(self) -> None:
        """Manual pause/resume (requested directly - "there is no pause
        sync and start sync button"). Just flips the persisted flag and
        defers to _apply_sync_state() for the actual start/stop decision -
        that function is also the metered-connection auto-pause's own
        trigger point, so this can't independently fight it over worker
        state (e.g. resuming manually while still on a metered connection
        with auto-pause enabled correctly stays paused)."""
        self._sync_paused = not self._sync_paused
        self.db.set_sync_state("sync_paused", "1" if self._sync_paused else "0")
        self._apply_sync_state()
        self._status_bar.showMessage("Sync paused" if self._sync_paused else "Sync resumed")
        if self._activity_popup is not None:
            self._activity_popup.refresh()

    def _read_limit_kbps(self, key: str) -> float | None:
        """Backs GraphClient's upload/download rate limiter getters - reads
        fresh from sync_state on every call (not cached) so a limit changed
        in Settings applies to the very next transfer. None/0/unparsable
        all mean unlimited."""
        raw = self.db.get_sync_state(key)
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def _check_network_status(self) -> None:
        metered = network_status.is_metered()
        online = network_status.is_online()
        # Covers two distinct conditions under one setting: the desktop's
        # power-profile is "power-saver", OR the raw battery percentage is
        # low - reported directly as a real gap ("batarya az olsa bile
        # senktron ediyor", it keeps syncing even when battery is low):
        # is_battery_saver() alone never fired here since this machine
        # never actually switches to a power-saver profile at low charge,
        # confirmed directly down to 7% remaining.
        battery_low = power_status.is_battery_saver() or power_status.is_battery_low()
        changed = (
            metered != self._metered_now
            or online != self._online
            or battery_low != self._battery_low_now
        )
        if changed:
            logger.info(
                "status changed: metered %s -> %s, online %s -> %s, battery low/saver %s -> %s",
                self._metered_now, metered, self._online, online,
                self._battery_low_now, battery_low,
            )
        self._metered_now = metered
        self._online = online
        self._battery_low_now = battery_low
        if changed:
            self._apply_sync_state()

    def _should_sync(self) -> bool:
        if self._sync_paused:
            return False
        if self._metered_now and self.db.get_sync_state("pause_on_metered") == "1":
            return False
        if self._battery_low_now and self.db.get_sync_state("pause_on_battery_saver") == "1":
            return False
        return True

    def _apply_sync_state(self) -> None:
        """Single source of truth for whether the background workers
        should be running right now - reconciles the user's manual pause
        with the metered-connection auto-pause setting. Every trigger
        (manual toggle, the periodic metered check, the Settings panel's
        checkbox) goes through this instead of calling
        _start/_stop_background_workers directly, so two independent
        triggers can't fight over the same worker state."""
        should_run = self._should_sync() and self.auth.is_signed_in and bool(self.drive_id)
        currently_running = self.delta_worker is not None
        if should_run and not currently_running:
            self._start_background_workers()
        elif not should_run and currently_running:
            self._stop_background_workers()
        self._update_tray_icon()

    def _update_tray_icon(self) -> None:
        # Blue when online and syncing, gray when offline, gray-with-pause-
        # bars when paused - requested directly ("when it is online lets
        # have a blue icon, when it is offline or paused show gray icon",
        # later refined: "when onedrive is paused use pause icon on
        # onedrive"). "Paused" here covers every reason sync might not be
        # running other than being offline: the user's own manual pause,
        # or the metered-connection/battery-saver auto-pause settings -
        # offline itself isn't a "pause" the app or user chose, so it
        # keeps the plain gray cloud with no pause bars.
        show_gray = not self._online or not self._should_sync()
        if not self._online:
            icon = gray_tray_icon(64)
        elif not self._should_sync():
            icon = paused_tray_icon(64)
        else:
            icon = app_icon(64)
        self._tray.setIcon(icon)
        tooltip = constants.DISPLAY_NAME
        if not self._online:
            tooltip += " - Offline"
        elif self._sync_paused:
            tooltip += " - Sync paused"
        elif self._battery_low_now and self.db.get_sync_state("pause_on_battery_saver") == "1":
            tooltip += " - Sync paused (battery)"
        elif show_gray:
            tooltip += " - Sync paused (metered connection)"
        self._tray.setToolTip(tooltip)

    def _on_refresh_clicked(self) -> None:
        if self.delta_worker:
            self.delta_worker.wake()
        if self.pair_worker:
            self.pair_worker.wake()
        if self.mount_sync_worker:
            self.mount_sync_worker.wake()

    def _on_global_excludes_changed(self) -> None:
        if self.pair_worker:
            self.pair_worker.wake()

    def _on_popup_display_changed(self) -> None:
        if self._activity_popup is not None:
            self._activity_popup.refresh()

    def _on_proxy_settings_changed(self) -> None:
        """GraphClient's session and MSAL's client both need their own
        copy of the current proxy config - they don't share a network
        layer, so this applies it to each separately rather than relying
        on one of them to somehow propagate to the other."""
        proxies, trust_env = proxy_config.get_proxy_settings(self.db)
        self.graph.apply_proxy(proxies, trust_env)
        self.auth.set_proxies(proxies)

    def _on_conflict_detected(self, name: str, path: str) -> None:
        # Fired the moment PairSyncWorker/MountSyncWorker actually resolves
        # a both-sides-changed conflict (kept both versions) - previously
        # the only way to learn this happened was to notice a lower
        # conflict count on the Backup tab or spot a "conflict" line while
        # scrolling the tray popup's activity list. Requested directly:
        # "Conflict varsa kullaniciya bilgi verebilir miyiz?" (can we
        # inform the user if there's a conflict), answered with a Settings
        # > General toggle rather than always-on (default on, same as the
        # other notification-shaped checkboxes in this app).
        if self.db.get_sync_state("notify_on_conflict") == "0":
            return
        if self._tray is None or not self._tray.supportsMessages():
            return
        self._tray.showMessage(
            "Sync conflict",
            f'"{name}" was changed on both this device and OneDrive - both versions were kept. '
            f"See {path}",
            QSystemTrayIcon.MessageIcon.Warning,
        )

    def _on_pair_status_changed(self, _pair_id: int, _message: str) -> None:
        # coalesce bursts (a bulk sync can fire this once per file) into at
        # most one table rebuild every 500ms
        if not self._pair_refresh_timer.isActive():
            self._pair_refresh_timer.start(500)
        if self._activity_popup is not None:
            # Cheap (one DB query, no widget work) - always runs, even while
            # the popup is hidden, so its ETA has already "warmed up" by the
            # time it's opened mid-sync instead of starting from zero.
            self._activity_popup.note_progress()
            # The full rebuild (avatar pixmap, activity list, DB query) is
            # only worth doing while actually visible - reported directly
            # that the popup felt slow to reflect real sync progress, since
            # it previously only refreshed on its own 3s timer while open.
            # Same 500ms coalescing as the table above, just gated on
            # visibility instead of always running.
            if self._activity_popup.isVisible() and not self._popup_refresh_timer.isActive():
                self._popup_refresh_timer.start(500)

    def _refresh_popup_if_visible(self) -> None:
        if self._activity_popup is not None and self._activity_popup.isVisible():
            self._activity_popup.refresh()

    def _refresh_cache_size_async(self) -> None:
        def compute():
            size = self.content_cache.cache_size_bytes()
            with self._cache_size_lock:
                self._cache_size_bytes = size

        threading.Thread(target=compute, daemon=True).start()

    def _update_account_cache_size_label(self) -> None:
        with self._cache_size_lock:
            cache_bytes = self._cache_size_bytes
        cache_mb = cache_bytes / (1024 * 1024)
        text = f"{cache_mb / 1024:.1f} GB used on this PC" if cache_mb >= 1024 else f"{cache_mb:.1f} MB used on this PC"
        self._account_cache_size_label.setText(text)

    def _update_sync_problems_indicator(self) -> None:
        if not self.drive_id:
            self._sync_problems_link.hide()
            return
        count = self.db.count_mount_op_errors(self.drive_id)
        if not count:
            self._sync_problems_link.hide()
            return
        noun = "problem" if count == 1 else "problems"
        self._sync_problems_link.setText(f'⚠ <a href="#">{count} sync {noun} - view</a>')
        self._sync_problems_link.show()

    def _open_sync_problems_dialog(self) -> None:
        dialog = SyncProblemsDialog(self.db, self.drive_id, lambda: self.mount_sync_worker, self)
        dialog.exec()
        self._update_sync_problems_indicator()

    def _update_mount_conflicts_indicator(self) -> None:
        if not self.drive_id:
            self._mount_conflicts_link.hide()
            return
        count = self.db.count_conflicts("mount")
        if not count:
            self._mount_conflicts_link.hide()
            return
        noun = "conflict" if count == 1 else "conflicts"
        self._mount_conflicts_link.setText(f'⚠ <a href="#">{count} sync {noun} to review - view</a>')
        self._mount_conflicts_link.show()

    def _open_mount_conflicts_dialog(self) -> None:
        drive_id = self.drive_id

        def resolve_fn(conflict_row, decision):
            resolve_mount_conflict(self.db, self.graph, drive_id, conflict_row, decision)

        def on_changed():
            if self.mount_sync_worker is not None:
                self.mount_sync_worker.wake()

        count = self.db.count_conflicts("mount")
        dialog = ConflictsDialog(self.db, "mount", "OneDrive", resolve_fn, count, on_changed, self)
        dialog.exec()
        self._update_mount_conflicts_indicator()

    def _open_manage_access_dialog(self, drive_id: str, item_id: str) -> None:
        # Mirrors _prompt_reconsent's own _show_window() call - the app may
        # be sitting minimized in the tray when a Dolphin context-menu
        # action triggers this, and a modal dialog appearing on a hidden
        # window would look like nothing happened.
        self._show_window()
        dialog = ManageAccessDialog(self.db, self.graph, drive_id, item_id, self)
        dialog.exec()

    def _open_share_dialog(self, drive_id: str, item_id: str) -> None:
        self._show_window()
        dialog = ShareDialog(self.db, self.graph, drive_id, item_id, self)
        dialog.exec()

    def _refresh_account_info_async(self) -> None:
        """Fetches the signed-in user's real Graph profile photo and display
        name in the background (requested directly - "make OneDrive UI look
        like NextCloud" for the photo, then "My name should be seen as it is
        seen in microsoft graph from Display Name" for the name). Both are
        routed through WorkerSignals rather than writing the cached
        attributes directly from this thread - QPixmap construction (in
        _refresh_account_ui/photo_avatar) isn't safe off the GUI thread, and
        a queued signal is what actually marshals the follow-up work back
        onto it, the same pattern every other background-thread-to-GUI
        update in this class already uses."""
        if not self.auth.is_signed_in:
            return

        def fetch():
            try:
                data = self.graph.get_profile_photo()
            except Exception:
                logger.debug("profile photo fetch failed, keeping existing avatar", exc_info=True)
            else:
                if data is not None:  # None = account has no photo set, not an error
                    try:
                        constants.PROFILE_PHOTO_FILE.write_bytes(data)
                    except OSError:
                        logger.debug("failed to cache profile photo to disk", exc_info=True)
                    self._signals.avatar_ready.emit(data)

            try:
                name = self.graph.get_display_name()
            except Exception:
                logger.debug("display name fetch failed, keeping existing name", exc_info=True)
            else:
                if name:
                    self.db.set_sync_state("display_name", name)
                    self._signals.display_name_ready.emit(name)

            try:
                tenant = self.graph.get_tenant_name()
            except Exception:
                logger.debug("tenant name fetch failed, keeping existing tenant label", exc_info=True)
                return
            if tenant:
                self.db.set_sync_state("tenant_name", tenant)
                self._signals.tenant_name_ready.emit(tenant)

        threading.Thread(target=fetch, daemon=True).start()

    def _on_avatar_ready(self, data: bytes) -> None:
        self._avatar_photo_bytes = data
        self._refresh_account_ui()
        if self._activity_popup is not None:
            self._activity_popup.refresh()

    def _on_tenant_name_ready(self, name: str) -> None:
        self._tenant_name = name
        if self._activity_popup is not None:
            self._activity_popup.refresh()

    def _on_display_name_ready(self, name: str) -> None:
        self._display_name = name
        self._refresh_account_ui()
        if self._activity_popup is not None:
            self._activity_popup.refresh()

    def _on_status_changed(self, message: str) -> None:
        # same coalescing as pair status: DeltaSyncWorker can emit this
        # rapidly during a big crawl, and rebuilding the folder tree /
        # status bar on every single one is unnecessary work
        self._pending_status_message = message
        if not self._status_update_timer.isActive():
            self._status_update_timer.start(300)

    def _apply_pending_status_update(self) -> None:
        message = self._pending_status_message
        count = self.db.item_count(self.drive_id) if self.drive_id else 0
        with self._cache_size_lock:
            cache_bytes = self._cache_size_bytes
        cache_mb = cache_bytes / (1024 * 1024)
        self._status_bar.showMessage(
            f"{message} | {count} items indexed | {cache_mb:.1f} MB cached"
        )
        if message == "Idle":
            self._populate_folder_tree()
            self._maybe_auto_mount()
            self._maybe_auto_mount()

    def _maybe_auto_mount(self) -> None:
        if self._auto_mount_attempted:
            return
        # A prior run of this app that got killed by a bare SIGTERM (e.g.
        # `systemctl restart`, which doesn't give this app's own graceful-
        # shutdown code a chance to run) leaves the kernel still showing
        # the mountpoint as mounted even though nothing is serving it
        # anymore - is_mounted() alone can't tell that apart from a
        # genuinely healthy mount, so without this check this method
        # would conclude "already mounted, nothing to do" and leave the
        # mount permanently broken (see recover_stale_mount's docstring).
        recover_stale_mount(self._mountpoint)
        if is_mounted(self._mountpoint):
            return
        self._auto_mount_attempted = True
        if self.db.get_sync_state("was_mounted") == "1":
            self._mount_field.setText(str(self._mountpoint))
            self._mount()

    # --- mount ---------------------------------------------------------

    def _on_browse_clicked(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose mount folder", str(self._mountpoint))
        if chosen:
            self._mount_field.setText(chosen)

    def _on_mount_clicked(self) -> None:
        recover_stale_mount(self._mountpoint)
        if is_mounted(self._mountpoint):
            # Requested directly, after the user was surprised to find the
            # mountpoint empty right after unmounting (it's a FUSE mount -
            # the folder is just a live view; downloaded/pinned content
            # stays cached on disk and comes right back on remount, but
            # nothing is visible at all while unmounted, unlike a real
            # synced folder). A confirmation here catches an accidental
            # click before it causes that same surprise again.
            confirm = QMessageBox.question(
                self, "Unmount OneDrive",
                "Files under this mount - including anything downloaded or pinned - will be "
                "inaccessible until you mount again. Unmount anyway?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            self._unmount()
        else:
            self._mount()

    def _mount(self) -> None:
        self._mountpoint = Path(self._mount_field.text()).expanduser()
        root_item = self.db.get_item_by_path(self.drive_id, "")
        if root_item is None:
            QMessageBox.warning(
                self, "Mount", "Still indexing your OneDrive - try again in a moment."
            )
            return
        try:
            self.fuse_operations = OneDriveOperations(
                self.db, self.content_cache, self.drive_id, root_item.id, self.graph,
                mount_sync_worker_ref=lambda: self.mount_sync_worker,
            )
            self._mount_thread = start_mount(self.fuse_operations, self._mountpoint)
        except Exception as e:
            QMessageBox.critical(self, "Mount failed", str(e))
            return
        self._mount_field.setEnabled(False)
        self._mount_btn.setText("Unmount")
        add_places_bookmark(self._mountpoint, title="OneDrive")
        self.db.set_sync_state("last_mountpoint", str(self._mountpoint))
        self.db.set_sync_state("was_mounted", "1")

    def _unmount(self) -> None:
        self.db.set_sync_state("was_mounted", "0")
        try:
            stop_mount(self._mountpoint)
        except Exception as e:
            QMessageBox.warning(self, "Unmount", str(e))
        self._mount_field.setEnabled(True)
        self._mount_btn.setText("Mount")

    # --- folder tree / pinning -------------------------------------------

    def _populate_folder_tree(self) -> None:
        if not self.drive_id:
            return
        self._populating = True
        expanded_ids = self._collect_expanded_ids()
        self._tree.clear()
        for folder in self.db.list_top_level_folders(self.drive_id):
            self._add_folder_item(self._tree.invisibleRootItem(), folder)
        self._populating = False
        self._restore_expanded(expanded_ids)

    def _collect_expanded_ids(self) -> set:
        expanded = set()

        def walk(item: QTreeWidgetItem):
            for i in range(item.childCount()):
                child = item.child(i)
                if child.isExpanded():
                    expanded.add(child.data(0, _ITEM_ID_ROLE))
                walk(child)

        walk(self._tree.invisibleRootItem())
        return expanded

    def _restore_expanded(self, expanded_ids: set) -> None:
        def walk(item: QTreeWidgetItem):
            for i in range(item.childCount()):
                child = item.child(i)
                if child.data(0, _ITEM_ID_ROLE) in expanded_ids:
                    child.setExpanded(True)
                walk(child)

        walk(self._tree.invisibleRootItem())

    def _add_folder_item(self, parent: QTreeWidgetItem, folder) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [folder.name, ""])
        item.setData(0, _ITEM_ID_ROLE, folder.id)
        item.setData(0, _LOADED_ROLE, False)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            1, Qt.CheckState.Checked if folder.is_pinned else Qt.CheckState.Unchecked
        )
        # placeholder child so the expand arrow shows before we lazily load
        QTreeWidgetItem(item, ["Loading..."])
        return item

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, _LOADED_ROLE):
            return
        item_id = item.data(0, _ITEM_ID_ROLE)
        self._populating = True
        item.takeChildren()
        for child in self.db.list_children(self.drive_id, item_id):
            if child.is_folder:
                self._add_folder_item(item, child)
        item.setData(0, _LOADED_ROLE, True)
        self._populating = False

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._populating or column != 1:
            return
        item_id = item.data(0, _ITEM_ID_ROLE)
        pinned = item.checkState(1) == Qt.CheckState.Checked
        self.db.set_pinned(self.drive_id, item_id, pinned)
        if pinned and self.pin_worker:
            self.pin_worker.wake()

    # --- lifecycle -----------------------------------------------------

    def closeEvent(self, event) -> None:
        """Closing the window does NOT stop syncing or unmount - it just
        hides to the tray, matching how OneDrive/Nextcloud etc. behave.
        Only the tray's 'Quit' action actually tears things down."""
        if self._quitting:
            self._stop_background_workers()
            self.overlay_server.stop()
            if is_mounted(self._mountpoint):
                stop_mount(self._mountpoint)
            # Deliberately not calling self.db.close() here anymore -
            # confirmed directly (twice) that a worker thread can still be
            # mid-operation after .stop() returns (it only sets a flag, see
            # _stop_background_workers), and closing the connection while
            # that's happening is exactly what crashed with
            # sqlite3.ProgrammingError on quit. Waiting it out with a
            # blocking join isn't reliable either - the systemd unit's own
            # 5s stop timeout is shorter than a single worker's realistic
            # worst case, so it just traded the crash for a SIGKILL mid-
            # join instead. The database is opened in WAL mode
            # specifically for durability against exactly this kind of
            # abrupt disconnect (already relied on for crash recovery
            # elsewhere in this app - "reconciliation is the recovery
            # mechanism") - not explicitly closing it here is safe, and the
            # OS reclaims the file handle when the process actually exits
            # right after this either way.
            event.accept()
            return

        event.ignore()
        self.hide()

    def eventFilter(self, obj, event) -> bool:
        """Drag-to-move on the header bar - needed now that the window is
        frameless (no native title bar to drag by). Same pattern the tray
        popup's own header already uses, minus the right-click reset menu
        (this window doesn't remember a dragged position the way the popup
        does)."""
        if obj is self._header:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.pos()
                return True
            elif event.type() == QEvent.Type.MouseMove and self._drag_offset is not None:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                return True
            elif event.type() == QEvent.Type.MouseButtonRelease and self._drag_offset is not None:
                self._drag_offset = None
                return True
        return super().eventFilter(obj, event)

    def _show_about(self) -> None:
        # Requested directly ("can you also put about option to give some
        # information version and publisher informations?"). Publisher/
        # license pulled from README.md's own Author/License sections
        # rather than invented here, so this can't drift from what the
        # repo itself says. Reachable from both the tray's right-click menu
        # and the popup's own account menu - one implementation, not
        # duplicated dialog-building code in two files.
        QMessageBox.about(
            self,
            f"About {constants.DISPLAY_NAME}",
            f"<b>{constants.DISPLAY_NAME}</b><br>"
            f"Version {constants.VERSION}<br><br>"
            "Publisher: Hasan Altin<br>"
            '<a href="https://hasanaltin.com">hasanaltin.com</a><br><br>'
            '<a href="https://github.com/hasanaltin/OneDrive">github.com/hasanaltin/OneDrive</a><br><br>'
            "License: MIT",
        )

    def _on_check_for_updates_clicked(self) -> None:
        self._update_check_btn.setEnabled(False)
        self._update_apply_btn.hide()
        self._update_status_label.setText("Checking for updates...")

        def run():
            try:
                new_version = update_check.check_for_update()
                if new_version is None:
                    self._signals.update_check_result.emit("uptodate", "")
                else:
                    self._signals.update_check_result.emit("available", new_version)
            except update_check.UpdateCheckError as exc:
                self._signals.update_check_result.emit("error", str(exc))

        threading.Thread(target=run, daemon=True).start()

    def _on_update_check_result(self, status: str, message: str) -> None:
        self._update_check_btn.setEnabled(True)
        if status == "uptodate":
            self._update_status_label.setText(f"You're up to date (version {constants.VERSION}).")
        elif status == "available":
            self._update_status_label.setText(f"Version {message} is available.")
            self._update_apply_btn.show()
        else:
            self._update_status_label.setText(f"Couldn't check for updates: {message}")

    def _on_apply_update_clicked(self) -> None:
        self._update_check_btn.setEnabled(False)
        self._update_apply_btn.setEnabled(False)
        self._update_status_label.setText("Downloading update...")

        def run():
            try:
                update_check.apply_update()
                self._signals.update_apply_result.emit(True, "")
            except update_check.UpdateCheckError as exc:
                self._signals.update_apply_result.emit(False, str(exc))

        threading.Thread(target=run, daemon=True).start()

    def _on_update_apply_result(self, success: bool, message: str) -> None:
        self._update_check_btn.setEnabled(True)
        self._update_apply_btn.setEnabled(True)
        if not success:
            self._update_status_label.setText(f"Update failed: {message}")
            return

        self._update_apply_btn.hide()
        self._update_status_label.setText("Update downloaded.")
        confirm = QMessageBox.question(
            self, "Restart Required",
            "The update has been downloaded. Restart OneDrive now to finish applying it?",
        )
        if confirm == QMessageBox.StandardButton.Yes:
            update_check.restart_app(self)

    def _quit_app(self) -> None:
        self._quitting = True
        self.close()
        QApplication.instance().quit()
