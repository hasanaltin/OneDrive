import datetime
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QSize, Qt, QPoint, QTimer
from PyQt6.QtGui import QGuiApplication, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from onedrive import sync_status
from onedrive.db import Database
from onedrive.gui.theme import (
    BRAND_BLUE,
    BRAND_BLUE_DARK,
    GREEN,
    badged_pixmap,
    folder_icon,
    icon_for_item,
    initials_avatar,
    photo_avatar,
)
from onedrive.gui.x11_hints import skip_taskbar

# A Tool window drops behind whatever the user clicks into (normal WM
# stacking) on its own, but that's not the same as actually closing -
# reported directly ("popup just goes back to active windows doesnt close
# itself when i click somewhere else"). _on_app_state_changed below is the
# real close-on-outside-click behavior; this timer is just a portable,
# window-manager-independent backstop for the case where it gets left open
# and forgotten anyway (e.g. on a desktop where the app-state signal proves
# unreliable). Long enough not to interrupt someone actively reading it
# (reset on any click, key press, search typing, or scrolling).
_IDLE_CLOSE_MS = 60_000

# Grace period after show() before outside-click auto-close is allowed to
# arm. Needed because a Tool window gets a brief, real activate-then-
# deactivate from the WM right on show() (see the window-flags comment in
# __init__) - without this, the popup would see that as "focus already
# lost" and close itself instantly, before the user ever gets to look at
# it. Confirmed via an isolated live test (grace period present: opens and
# stays open; without it: closes itself immediately, no click needed) -
# this is exactly the class of regression that bit the same window type
# once before, so it was verified this way rather than guessed.
_AUTOHIDE_GRACE_MS = 250

# A single applicationStateChanged flip to Inactive isn't trusted on its
# own - opening this popup's own account/right-click menu (QMenu.exec()) is
# a nested event loop in the same process and was verified, live, NOT to
# fire this signal at all, so in practice this debounce mostly just adds a
# small delay before a genuine outside click takes effect. Kept anyway as
# cheap insurance against any other same-app transient focus blip this
# wasn't explicitly tested against.
_AUTOHIDE_DEBOUNCE_MS = 250

_EVENT_VERBS = {
    "uploaded": "changed",
    "downloaded": "changed",
    "created": "created",
    "deleted": "deleted",
    "conflict": "created a conflicted copy of",
}

# "downloaded" means two different things depending on where it came from.
# For a Folder Pair, content_cache isn't involved at all - a pair-synced
# file is always fully local (see _is_cloud_only's docstring), so a
# "downloaded" event there only ever fires because reconcile.py detected a
# genuine remote change and pulled it down: "changed" is accurate. For the
# on-demand mount, ContentCache.ensure_cached() logs the exact same
# "downloaded" event type purely because open() triggered a first-time
# cache fill - it fires just from viewing a cloud-only file, with no
# content change involved at all, so labeling it "changed" is simply wrong
# (confirmed directly: opening a PDF/CSV that was never edited produced a
# "You changed X" entry backed by nothing but a plain 'downloaded' row).
def _verb_for(event_type: str, source: str) -> str:
    if event_type == "downloaded" and source == "mount":
        return "opened"
    return _EVENT_VERBS.get(event_type, event_type)

# Matches the "(42/745)" progress pairs_panel/pair_worker already writes
# into a pair's persisted status, e.g. "Uploading (42/745): some/path".
_PROGRESS_RE = re.compile(r"\((\d+)/(\d+)\)")
_ACTIVE_STATUS_PREFIXES = ("Syncing", "Uploading", "Downloading", "Checking", "Creating", "Deleting", "Conflict")

# Pulls the verb and specific file out of a per-file status like
# "Uploading (42/745): some/path" or "Deleting remotely: some/path" - this
# in-flight info only ever lives in a pair's status string, it's never
# written to activity_log until the operation actually finishes.
_STATUS_ITEM_RE = re.compile(r"^(?P<verb>[A-Za-z][A-Za-z ]*?)(?:\s*\(\d+/\d+\))?:\s*(?P<path>.+)$")

# Gap kept between the popup's bottom edge and the screen's bottom edge.
# Requested directly ("keep the popup touched to the task bar") - matches
# Windows OneDrive/Nextcloud's own popups, which sit flush against the
# taskbar's top edge rather than floating above it. Was 56 (a generous flat
# guess meant to clear the taskbar on desktops that don't report it as
# reserved space via screen.availableGeometry()) - 0 is the deliberate
# "touching" value now that being flush is the actual goal, not a gap to
# avoid.
_TASKBAR_CLEARANCE = 0

# Small gap kept between the popup's right edge and the screen's right
# edge when auto-placed - matches how native system-tray flyouts (e.g. the
# Plasma network/Bluetooth applets) sit, requested directly with a
# screenshot of one of those as the reference: open glued to the
# bottom-right corner near the system tray, just like that.
_EDGE_MARGIN = 8


def _relative_time(ts_iso: str) -> str:
    try:
        dt = datetime.datetime.fromisoformat(ts_iso)
    except ValueError:
        return ""
    now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
    seconds = (now - dt).total_seconds()
    if seconds < 60:
        return "now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours}h"
    return f"{int(hours // 24)}d"


def _format_eta(seconds: float) -> str:
    seconds = max(seconds, 0)
    if seconds < 60:
        return f"~{max(int(seconds), 1)}s left"
    minutes = seconds / 60
    if minutes < 60:
        return f"~{max(int(minutes), 1)}m left"
    hours = minutes / 60
    return f"~{hours:.1f}h left"


class _ClickableFrame(QFrame):
    """A QFrame that runs a callback on left-click, anywhere within it -
    used for the sync status row ("All synced!" / "Syncing... N items
    left"). A first attempt only made the status text QLabel itself
    clickable, which turned out to be too narrow a target: reported
    directly, with the user clicking the row and nothing happening - traced
    to a click landing on the "✓" checkmark or on blank space within the
    row rather than precisely on the text's own tight bounding box, neither
    of which had any click handling of their own (confirmed directly:
    simulated clicks on those exact spots left the popup open). Wrapping
    the whole row instead of just the label removes the need to hit an
    exact few-pixel target. A child that actively handles its own click
    (like the "Sync now" QPushButton) still consumes the event before it
    ever reaches here, so this doesn't interfere with that."""

    def __init__(self, on_click: Callable[[], None], parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
            return
        super().mousePressEvent(event)


class _ActivityRow(QFrame):
    def __init__(
        self,
        icon: QIcon,
        title: str,
        time_text: str,
        parent=None,
        in_progress: bool = False,
        badge: str = "check",
        on_click: Callable[[], None] | None = None,
    ):
        super().__init__(parent)
        self._on_click = on_click
        if on_click is not None:
            self.setObjectName("activityRow")
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setStyleSheet("QFrame#activityRow:hover { background: palette(midlight); }")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)

        icon_label = QLabel()
        icon_label.setPixmap(badged_pixmap(icon, 36, badge="syncing" if in_progress else badge))
        icon_label.setFixedSize(36, 36)
        layout.addWidget(icon_label)

        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet(f"font-size: 12px;{' font-weight: 600;' if in_progress else ''}")
        title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(title_label, stretch=1)

        time_label = QLabel(time_text)
        time_label.setStyleSheet(f"color: {BRAND_BLUE if in_progress else 'palette(mid)'}; font-size: 11px;")
        time_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        layout.addWidget(time_label)

        # A checked green box reads as "done" - misleading for a file still
        # actively transferring, so in-progress rows get no checkbox at all.
        if not in_progress:
            check = QCheckBox()
            check.setChecked(True)
            check.setEnabled(False)
            check.setStyleSheet(f"QCheckBox::indicator {{ width: 15px; height: 15px; }}"
                                 f"QCheckBox::indicator:checked {{ background: {GREEN}; border-radius: 3px; }}")
            layout.addWidget(check, alignment=Qt.AlignmentFlag.AlignTop)

    def mousePressEvent(self, event) -> None:
        if self._on_click is not None and event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
            return
        super().mousePressEvent(event)


class ActivityPopup(QWidget):
    """A small popup shown near the tray icon on click, listing recent sync
    activity - styled after the Nextcloud desktop client's own tray popup
    (blue header with avatar, search box, sync status row, grouped activity
    list, plain-text footer links)."""

    def __init__(
        self,
        db: Database,
        mountpoint_getter: Callable[[], object],
        on_open_settings: Callable[[], None],
        on_sync_now: Callable[[], None] | None = None,
        username_getter: Callable[[], str] | None = None,
        display_name_getter: Callable[[], str | None] | None = None,
        tenant_name_getter: Callable[[], str | None] | None = None,
        web_url_getter: Callable[[], str | None] | None = None,
        avatar_bytes_getter: Callable[[], bytes | None] | None = None,
        on_exit: Callable[[], None] | None = None,
        sync_paused_getter: Callable[[], bool] | None = None,
        on_toggle_sync: Callable[[], None] | None = None,
        show_status_row_getter: Callable[[], bool] | None = None,
        show_tenant_name_getter: Callable[[], bool] | None = None,
        sync_active_getter: Callable[[], bool] | None = None,
        drive_id_getter: Callable[[], str | None] | None = None,
        parent=None,
    ):
        # Qt.WindowType.Tool - not Popup. Popup is override-redirect at the
        # X11 level, which keeps it permanently pinned above every other
        # window regardless of focus (reported directly, confirmed via
        # screen recording). Tool IS a normal WM-managed window: it
        # participates in ordinary stacking, so it naturally drops behind
        # whatever the user clicks into - no click-detection needed at all
        # for that part, it's just how regular windows behave. It also
        # still skips the taskbar/pager, same as Popup did.
        #
        # Tool was tried here once before and reverted fast: it closed
        # itself immediately on open, no click needed. That regression
        # wasn't caused by the window type itself - it was caused by
        # KEEPING the Popup-era "close on any focus loss" logic
        # (focusOutEvent, the _NET_ACTIVE_WINDOW watchdog) while switching
        # to Tool. Tool windows get a brief, real activate-then-deactivate
        # from the WM right on show() (typical "utility window shouldn't
        # steal focus" policy) - fine on its own, but fatal combined with
        # code that closes on the first focus-loss signal it sees. This
        # time that whole close-on-focus-loss subsystem is removed
        # entirely, not just swapped to a different window type - see
        # keyPressEvent/the idle-close timer for how dismissal actually
        # works now instead.
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        # Tool | FramelessWindowHint alone was never actually enough to
        # skip the taskbar on this desktop - that exact discrepancy is why
        # MainWindow needed x11_hints.skip_taskbar() too (see its own
        # __init__ comment for the full story: `xprop` showed a _NORMAL
        # fallback type even with these flags, and Plasma's Task Manager
        # respects that fallback). This popup was never given the same
        # treatment and reportedly has the identical problem - fixed the
        # same way, called before first show() (direct property form) and
        # again in show_near() (ClientMessage form, for re-shows).
        skip_taskbar(int(self.winId()))
        self.db = db
        self._mountpoint_getter = mountpoint_getter
        self._on_open_settings = on_open_settings
        self._on_sync_now = on_sync_now
        self._username_getter = username_getter
        self._display_name_getter = display_name_getter
        self._tenant_name_getter = tenant_name_getter
        self._show_status_row_getter = show_status_row_getter
        self._drive_id_getter = drive_id_getter
        self._show_tenant_name_getter = show_tenant_name_getter
        self._sync_active_getter = sync_active_getter
        self._web_url_getter = web_url_getter
        self._avatar_bytes_getter = avatar_bytes_getter
        self._on_exit = on_exit
        self._sync_paused_getter = sync_paused_getter
        self._on_toggle_sync = on_toggle_sync
        self._all_events: list[dict] = []
        self._show_pos: QPoint = QPoint(0, 0)
        # ETA rate tracking - see note_progress()/_estimate_eta()
        self._sync_streak_start: float | None = None
        self._sync_streak_start_remaining: int = 0
        # Separate tracker for PinWorker's own backlog (see
        # _sync_progress_summary) - kept apart from the pair-sync one
        # above since they're two unrelated activity streams that can be
        # active independently of each other.
        self._pin_streak_start: float | None = None
        self._pin_streak_start_remaining: int = 0
        self.setFixedWidth(460)
        self.setAutoFillBackground(True)
        self.setStyleSheet(
            "ActivityPopup { background: palette(base); border: 1px solid palette(mid); }"
            "QListWidget { border: none; background: palette(base); }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_status_row())

        self._list = QListWidget()
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setMinimumHeight(380)
        self._list.setMaximumHeight(460)
        self._list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self._list)

        # Open folder / View online / Settings used to live in a footer row
        # here - removed (requested directly): Open folder duplicates the
        # header's folder-icon button, and View online/Settings now live in
        # the account menu instead, alongside the new pause/resume toggle.
        # Sync happens on a background thread with no signal back to this
        # widget, so without polling the list only ever reflected whatever
        # was true at the moment the popup was opened - a change made while
        # it sat open (even one synced within a couple seconds) wouldn't
        # show up until it was closed and reopened, which read as "detection
        # is slow" when the sync itself wasn't.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(3000)
        self._refresh_timer.timeout.connect(self.refresh)

        self._idle_close_timer = QTimer(self)
        self._idle_close_timer.setSingleShot(True)
        self._idle_close_timer.timeout.connect(self.close)
        self._list.verticalScrollBar().valueChanged.connect(self._note_activity)

        # Close-on-outside-click. armed starts False and only flips True
        # after _AUTOHIDE_GRACE_MS from show() (see that constant's
        # comment for why) - the debounce timer, not this signal directly,
        # is what actually triggers the close, so a same-app blip that
        # bounces back to Active within _AUTOHIDE_DEBOUNCE_MS never closes
        # anything.
        self._autohide_armed = False
        self._autohide_grace_timer = QTimer(self)
        self._autohide_grace_timer.setSingleShot(True)
        self._autohide_grace_timer.timeout.connect(self._arm_autohide)
        self._autohide_debounce_timer = QTimer(self)
        self._autohide_debounce_timer.setSingleShot(True)
        self._autohide_debounce_timer.timeout.connect(self._check_still_inactive_and_close)
        QApplication.instance().applicationStateChanged.connect(self._on_app_state_changed)

        self.refresh()

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("popupHeader")
        # Went 60 -> 68 -> 64 chasing a reported "too much space" that
        # turned out not to be about the gap between the two identity
        # lines at all (that part measured ~0px, see the button's own
        # comment below) - it was the HEADER itself being taller than its
        # content needs. The avatar (36px) plus the header's own 8px top/
        # bottom margins is 52px - that's the actual minimum, and is what
        # this is set to now, rather than a value picked to leave slack
        # for a two-line stack that turned out to only need ~34px anyway.
        header.setFixedHeight(52)
        header.setStyleSheet(
            "QFrame#popupHeader { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 {BRAND_BLUE}, stop:1 {BRAND_BLUE_DARK}); }}"
        )
        # No drag-to-move - removed after being explicitly rejected ("it's
        # movable, I don't want that"), following a screenshot of a native
        # system-tray flyout (Plasma's own Bluetooth applet) as the
        # reference: those dock to a fixed corner and aren't draggable at
        # all. show_near() now always anchors here; there's no saved
        # position to override it anymore.
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 8, 14, 8)
        header_layout.setSpacing(10)

        self._avatar_label = QLabel()
        self._avatar_label.setFixedSize(36, 36)
        self._avatar_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header_layout.addWidget(self._avatar_label)

        # Was a QLabel pair with WA_TransparentForMouseEvents set on both -
        # i.e. genuinely inert, not just visually static. Clicks passed
        # straight through to the header's own drag-handling eventFilter
        # underneath, so nothing happened at all - reported directly
        # ("Drop down menu doesn't work"). A QPushButton gets real click
        # handling for free instead of needing a custom event filter here.
        self._account_menu_btn = QPushButton("")
        self._account_menu_btn.setFlat(True)
        self._account_menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # Padding and setFixedHeight alone weren't enough either (both
        # reported directly, twice) - measured the actual rendered
        # geometry directly (a live isolated test, not another guess) and
        # found a 7px gap surviving even with every margin/padding/height
        # on the button and label themselves zeroed. The real cause: this
        # button used to sit inside its own nested QHBoxLayout (just for
        # an addStretch(1) to keep it left-aligned instead of stretching
        # full-width), and nesting a QHBoxLayout inside identity_col's
        # QVBoxLayout via addLayout() reserved that extra space by itself,
        # independent of any of that layout's own margins or spacing
        # settings. Adding the button straight to identity_col (no nested
        # layout at all) measured a 0px gap - it stretches to the column's
        # full width now instead of hugging its own text width, but
        # text-align: left below keeps the label itself sitting exactly
        # where it always did.
        self._account_menu_btn.setFixedHeight(18)  # matches this font's own natural line height exactly
        # No hover background - reported directly as not looking good, and
        # now that this button stretches to the column's full width (see
        # the comment above), that highlight would span the whole row
        # rather than just hugging the name text, which is what made it
        # noticeable enough to report.
        self._account_menu_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: white; "
            "font-weight: 600; font-size: 13px; text-align: left; padding: 0px 6px; border-radius: 4px; }"
        )
        self._account_menu_btn.clicked.connect(self._show_account_menu)

        # Tenant (the domain half of the signed-in email) shown under the
        # display name, requested directly - elided with "..." rather than
        # wrapping or overflowing if it doesn't fit the popup's fixed
        # width; the full value is still available as a tooltip.
        self._tenant_label = QLabel("")
        self._tenant_label.setFixedHeight(14)
        self._tenant_label.setStyleSheet(
            "color: rgba(255, 255, 255, 190); font-size: 10px; padding: 0 6px;"
        )

        identity_col = QVBoxLayout()
        identity_col.setContentsMargins(0, 0, 0, 0)
        identity_col.setSpacing(0)
        identity_col.addWidget(self._account_menu_btn)
        identity_col.addWidget(self._tenant_label)
        header_layout.addLayout(identity_col, stretch=1)

        folder_btn = QPushButton()
        folder_btn.setIcon(folder_icon(20, "white"))
        folder_btn.setIconSize(QSize(20, 20))
        folder_btn.setFlat(True)
        folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        folder_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; padding: 4px; border-radius: 4px; }"
            "QPushButton:hover { background: rgba(255, 255, 255, 40); }"
        )
        folder_btn.clicked.connect(self._open_folder)
        header_layout.addWidget(folder_btn)

        # No close ("✕") button here anymore - removed (requested directly)
        # now that outside-click actually closes the popup (see
        # _on_app_state_changed); Escape and clicking the status row still
        # work as manual dismiss paths too, so this wasn't the only way to
        # close it, just a redundant one.
        return header

    def _build_status_row(self) -> QFrame:
        row = _ClickableFrame(self.close)
        self._status_row = row
        row.setStyleSheet("QFrame { border-bottom: 1px solid palette(midlight); }")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 6, 14, 10)

        check_label = QLabel("✓")
        check_label.setStyleSheet(f"color: {GREEN}; font-weight: 700; font-size: 14px;")
        row_layout.addWidget(check_label)

        self._status_title = QLabel("All synced!")
        self._status_title.setStyleSheet("font-weight: 600;")
        row_layout.addWidget(self._status_title)
        row_layout.addStretch(1)

        self._sync_btn = QPushButton("Sync now")
        self._sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sync_btn.setStyleSheet(
            "QPushButton { border: 1px solid palette(mid); border-radius: 4px; "
            "padding: 4px 12px; background: palette(base); font-size: 11px; }"
            "QPushButton:hover { background: palette(midlight); }"
        )
        self._sync_btn.clicked.connect(self._on_sync_now_clicked)
        row_layout.addWidget(self._sync_btn)

        return row

    def refresh(self) -> None:
        username = self._username_getter() if self._username_getter else ""
        avatar_bytes = self._avatar_bytes_getter() if self._avatar_bytes_getter else None
        photo = photo_avatar(avatar_bytes, 36) if avatar_bytes else None
        display_name = self._display_name_getter() if self._display_name_getter else None
        self._avatar_label.setPixmap(photo or initials_avatar(display_name or username or "?", size=36))
        shown_name = display_name or (username or "").split("@")[0] or "Not signed in"
        self._account_menu_btn.setText(f"{shown_name} ▾")

        # Real tenant/company name (e.g. "Contoso Ltd") from Graph's
        # /organization when available - requested directly, in place of
        # the email domain that was there before (the domain isn't the
        # same as the company name). Fetched asynchronously after sign-in,
        # so this still falls back to the domain when it isn't available
        # yet. Whether this line shows at all is a Settings toggle
        # (requested directly, as a customizable pair alongside the "Sync
        # now" button below) - no tooltip on it anymore either, since a
        # stray tooltip bubble left open in a screenshot was mistaken for
        # part of the popup itself and reported as an unwanted blank popup.
        show_tenant = self._show_tenant_name_getter() if self._show_tenant_name_getter else True
        if show_tenant:
            tenant_name = self._tenant_name_getter() if self._tenant_name_getter else None
            tenant = tenant_name or (username.split("@", 1)[1] if "@" in username else "")
            fm = self._tenant_label.fontMetrics()
            self._tenant_label.setText(fm.elidedText(tenant, Qt.TextElideMode.ElideRight, 280))
        self._tenant_label.setVisible(show_tenant)

        # Hides the WHOLE status row (checkmark, "All synced!"/progress
        # text, and the Sync now button together), not just the button on
        # its own - reported directly, with a screenshot circling the
        # entire row: turning this off left the row's status text and
        # checkmark still sitting there, which wasn't the "gone" the
        # setting's name implied.
        show_status_row = self._show_status_row_getter() if self._show_status_row_getter else True
        self._status_row.setVisible(show_status_row)

        self._all_events = self.db.list_recent_activity(limit=30)
        progress = self._sync_progress_summary()
        sync_active = self._sync_active_getter() if self._sync_active_getter else True
        if progress:
            self._status_title.setText(progress)
        elif not sync_active:
            self._status_title.setText("Sync paused")
        else:
            self._status_title.setText("All synced!" if self._all_events else "OneDrive is up to date")
        self._render(self._all_events)

    def _current_sync_state(self) -> tuple[int, bool]:
        """(remaining_total, any_active), parsed from the "(done/total)"
        progress pair_worker already writes into each pair's status -
        shared by _sync_progress_summary (what to display) and note_progress
        (the ETA rate tracker below), so both always agree on the same
        numbers.

        Short-circuits to (0, False) whenever background sync isn't
        actually running right now (manually paused, or auto-paused for
        metered/battery reasons) - reported directly as a real bug
        ("sync durdurulmus olsa bile upload ediyor", it keeps showing
        upload activity even though sync is paused): pair.last_sync_status
        is only ever updated by PairSyncWorker itself, so the last string
        it wrote before being stopped (e.g. "Uploading ...") just sits in
        the database unchanged - forever, since nothing runs to overwrite
        it - and this method was reading that stale text with no check on
        whether the worker announcing it even still exists."""
        if self._sync_active_getter is not None and not self._sync_active_getter():
            return 0, False
        remaining_total = 0
        any_active = False
        for pair in self.db.list_pairs():
            status = pair.last_sync_status
            match = _PROGRESS_RE.search(status)
            if match:
                done, total = int(match.group(1)), int(match.group(2))
                remaining_total += max(total - done, 0)
                any_active = True
            elif status == "syncing" or status.startswith(_ACTIVE_STATUS_PREFIXES):
                any_active = True
        # The on-demand mount's own offline-write queue (pending_mount_ops,
        # drained by MountSyncWorker) is a completely separate mechanism
        # from Folder Pairs' pair.last_sync_status - previously invisible
        # here entirely, the same gap PinWorker had before it got its own
        # branch below. Folded into the same total rather than a separate
        # line: from the user's perspective it's just more files syncing,
        # not a conceptually different kind of activity.
        drive_id = self._drive_id_getter() if self._drive_id_getter else None
        if drive_id:
            pending_mount = self.db.count_pending_mount_ops(drive_id)
            if pending_mount:
                remaining_total += pending_mount
                any_active = True
        return remaining_total, any_active

    def note_progress(self) -> None:
        """Updates the ETA rate tracker - cheap (one DB query, no widget
        work), meant to be called on every pair_status_changed signal
        regardless of whether the popup is currently visible, so an ETA is
        already available the moment it's opened mid-sync rather than
        needing a few seconds to "warm up" after opening. Tracks a "streak"
        (start time + remaining-item count at that time) rather than a
        moving average - simpler, and self-corrects immediately if new
        changes arrive mid-sync and grow the total (restarts the baseline
        instead of diluting the rate with a now-stale earlier estimate)."""
        remaining, any_active = self._current_sync_state()
        if not any_active:
            self._sync_streak_start = None
            return
        if self._sync_streak_start is None or remaining > self._sync_streak_start_remaining:
            self._sync_streak_start = time.monotonic()
            self._sync_streak_start_remaining = remaining

    def _estimate_eta(self, remaining: int) -> str | None:
        if self._sync_streak_start is None:
            return None
        elapsed = time.monotonic() - self._sync_streak_start
        completed = self._sync_streak_start_remaining - remaining
        if elapsed < 3 or completed <= 0:
            return None  # not enough data yet for a stable estimate
        seconds_left = remaining / (completed / elapsed)
        return _format_eta(seconds_left)

    def _sync_progress_summary(self) -> str | None:
        """"All synced!" used to show even while hundreds of files were
        actively uploading, because it only checked "has any activity ever
        happened" rather than the pairs' actual current status. Returns a
        "N items left (~ETA)" summary instead - None means nothing is
        actively syncing right now, so the caller falls back to the old
        static text."""
        remaining_total, any_active = self._current_sync_state()
        if any_active:
            if not remaining_total:
                return "Syncing..."
            eta = self._estimate_eta(remaining_total)
            base = f"Syncing... {remaining_total} item{'s' if remaining_total != 1 else ''} left"
            return f"{base} ({eta})" if eta else base
        # Folder Pairs show nothing active - PinWorker (the on-demand
        # mount's eager pre-download for pinned folders) is a completely
        # separate mechanism that never touches pair.last_sync_status at
        # all, so it was invisible here entirely. Reported directly: files
        # were visibly downloading with no indication of that anywhere in
        # the popup. Has its own ETA now too (first shipped without one,
        # then reported directly again - it showed the remaining file
        # count but no duration estimate): PinWorker has no per-file
        # signal the way pair syncing
        # does, so this streak tracker is instead sampled here, at
        # whatever rate _sync_progress_summary() itself gets called
        # (refresh()'s ~3s timer) - coarser samples, same underlying
        # rate-estimate math as pair syncing's own tracker.
        pending_pins = self.db.count_pending_pinned_downloads()
        if pending_pins:
            if self._pin_streak_start is None or pending_pins > self._pin_streak_start_remaining:
                self._pin_streak_start = time.monotonic()
                self._pin_streak_start_remaining = pending_pins
            eta = self._estimate_pin_eta(pending_pins)
            base = f"Downloading pinned files... {pending_pins} remaining"
            return f"{base} ({eta})" if eta else base
        self._pin_streak_start = None
        return None

    def _estimate_pin_eta(self, remaining: int) -> str | None:
        if self._pin_streak_start is None:
            return None
        elapsed = time.monotonic() - self._pin_streak_start
        completed = self._pin_streak_start_remaining - remaining
        if elapsed < 3 or completed <= 0:
            return None  # not enough data yet for a stable estimate
        seconds_left = remaining / (completed / elapsed)
        return _format_eta(seconds_left)

    def _current_syncing_rows(self) -> list[tuple[str, str, str]]:
        """(verb, rel_path, pair_local_path) for each pair actively working
        on a specific file right now, straight off the same status text the
        Folder Pairs panel shows - this list wants to answer "which files",
        not just "how many are left". pair_local_path is carried along so a
        click can resolve the same file's absolute path, same as a finished
        activity row."""
        # Same stale-status guard as _current_sync_state - see its
        # docstring for why this can't just trust pair.last_sync_status on
        # its own.
        if self._sync_active_getter is not None and not self._sync_active_getter():
            return []
        rows = []
        for pair in self.db.list_pairs():
            match = _STATUS_ITEM_RE.match(pair.last_sync_status)
            if match:
                rows.append((match.group("verb").strip(), match.group("path").strip(), pair.local_path))
        return rows

    def _resolve_open_target(self, source: str, rel_or_full_path: str | None, is_deleted: bool) -> Path | None:
        """Turns an activity_log row's (source, path) into a real, absolute
        filesystem path to hand to xdg-open - "mount" paths are drive-
        relative (resolved against the current mountpoint), "pair:<id>"
        paths are relative to that pair's own local_path. A deleted item (or
        one whose exact path no longer exists for any other reason) falls
        back to its containing folder, which does still exist."""
        if not rel_or_full_path:
            return None
        if source == "mount":
            mountpoint = self._mountpoint_getter()
            if not mountpoint:
                return None
            target = Path(mountpoint) / rel_or_full_path.lstrip("/")
        elif source.startswith("pair:"):
            try:
                pair_id = int(source.split(":", 1)[1])
            except ValueError:
                return None
            pair = self.db.get_pair(pair_id)
            if pair is None:
                return None
            target = Path(pair.local_path) / rel_or_full_path
        else:
            return None
        if is_deleted or not target.exists():
            target = target.parent
        return target if target.exists() else None

    def _is_cloud_only(self, source: str, path: str | None) -> bool:
        """True if this row's file exists only on OneDrive right now and
        hasn't actually been downloaded to this device yet - the same
        green-check/blue-cloud distinction Nextcloud's own client draws
        (its Nautilus integration is the reference the badge colors here
        were checked against). Delegates to sync_status.status_for_path -
        the same function the Dolphin overlay-icon socket server uses - so
        a file never shows one color here and a different one in Dolphin.
        Only meaningful for "mount" source rows: Folder Pairs sync
        downloads real files to a real local folder, so a pair-synced item
        is always fully local by definition."""
        if source != "mount" or not path:
            return False
        mountpoint = self._mountpoint_getter()
        if not mountpoint:
            return False
        abs_path = Path(mountpoint) / path.lstrip("/")
        return sync_status.status_for_path(self.db, Path(mountpoint), abs_path) == sync_status.CLOUD

    def _open_path(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            subprocess.Popen(["xdg-open", str(path)])
        except OSError:
            pass
        self.close()

    def _render(self, events: list[dict]) -> None:
        scroll_pos = self._list.verticalScrollBar().value()
        self._list.clear()

        syncing_rows = self._current_syncing_rows()
        for verb, rel_path, pair_local_path in syncing_rows:
            name = rel_path.rsplit("/", 1)[-1]
            target = Path(pair_local_path) / rel_path
            resolved = target if target.exists() else (target.parent if target.parent.exists() else None)
            on_click = (lambda t=resolved: self._open_path(t)) if resolved is not None else None
            # The in-flight status text only says e.g. "Creating remote
            # folder" / "Uploading" - "folder" appearing in the verb is the
            # only signal available here for which icon to use.
            icon = icon_for_item(name, is_folder="folder" in verb.lower())
            row = _ActivityRow(icon, f"{verb} {name}", "now", in_progress=True, on_click=on_click)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(item)
            self._list.setItemWidget(item, row)

        if not events:
            if not syncing_rows:
                placeholder = QListWidgetItem("No recent activity")
                placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
                self._list.addItem(placeholder)
            self._list.verticalScrollBar().setValue(scroll_pos)
            return
        for e in events:
            verb = _verb_for(e["event_type"], e["source"])
            title = f"You {verb} {e['name']}"
            is_deleted = e["event_type"] == "deleted"
            target = self._resolve_open_target(e["source"], e["path"], is_deleted=is_deleted)
            on_click = (lambda t=target: self._open_path(t)) if target is not None else None
            icon = icon_for_item(e["name"], bool(e["is_folder"]))
            cloud_only = not is_deleted and self._is_cloud_only(e["source"], e["path"])
            # cloud_only reflects current on-disk state (still cloud-only
            # right now) and takes priority when true; otherwise the badge
            # is derived from `verb` (the same text already shown), not the
            # raw event_type - requested directly, with a screenshot: an
            # edit to an existing, already-synced file showed the
            # "uploaded" up-arrow (reads as "just arrived new"), while the
            # text right next to it correctly said "You changed X". Using
            # `verb` instead of event_type keeps the icon and the text
            # telling the same story: "uploaded"/"downloaded" event_type
            # both mean "created" or "changed" ("You changed X"'s badge is
            # a dot, not an arrow) depending on which one actually
            # happened, exactly as _verb_for already computed for the text;
            # the on-demand mount's "opened" case (event_type="downloaded"
            # purely from a first-time cache fill on open(), not a real
            # edit - see _verb_for's own docstring) now correctly falls
            # through to the plain "check" badge instead of a stray
            # download arrow too.
            if is_deleted:
                badge = "deleted"
            elif cloud_only:
                badge = "cloud"
            elif verb == "created":
                badge = "created"
            elif verb == "changed":
                badge = "changed"
            else:
                badge = "check"
            row = _ActivityRow(icon, title, _relative_time(e["ts"]), on_click=on_click, badge=badge)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(item)
            self._list.setItemWidget(item, row)
        # a periodic auto-refresh re-renders the whole list on a timer -
        # without this a scrolled-down reader gets yanked back to the top
        # every few seconds
        self._list.verticalScrollBar().setValue(scroll_pos)

    def _on_sync_now_clicked(self) -> None:
        if self._on_sync_now:
            self._on_sync_now()

    def _open_folder(self) -> None:
        try:
            subprocess.Popen(["xdg-open", str(self._mountpoint_getter())])
        except OSError:
            pass
        self.close()

    def _view_online(self) -> None:
        url = (self._web_url_getter() if self._web_url_getter else None) or "https://onedrive.live.com"
        try:
            subprocess.Popen(["xdg-open", url])
        except OSError:
            pass
        self.close()

    def _open_settings_clicked(self) -> None:
        self.close()
        self._on_open_settings()

    def _anchor_pos(self, pos: QPoint, size: QSize) -> tuple[int, int]:
        """Bottom-right-corner anchor computation for `size`, on whichever
        screen `pos` is on - factored out so show_near() can call this
        twice (once with a stale size before refresh(), once with the real
        size after) without duplicating the geometry math."""
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is not None:
            full = screen.geometry()
            avail = screen.availableGeometry()
            bottom_edge = avail.bottom() if avail.bottom() < full.bottom() else full.bottom()
            x = avail.right() - size.width() - _EDGE_MARGIN
            x = max(avail.left(), x)
            y = max(avail.top(), bottom_edge - size.height() - _TASKBAR_CLEARANCE)
        else:
            x = max(0, pos.x() - size.width() // 2)
            y = max(0, pos.y() - size.height() - _TASKBAR_CLEARANCE)
        return x, y

    def show_near(self, pos: QPoint) -> None:
        """Position the popup glued to the bottom-right corner of whichever
        screen `pos` is on, matching native system-tray flyouts (Plasma's
        own network/Bluetooth applets, etc.) - a fixed dock point, every
        time, not centered on the cursor and not draggable. Was cursor-
        centered before, which read as "moving around" since the exact
        click point within the tray icon isn't identical every time; a
        drag-to-move escape hatch was tried after that but was itself
        rejected just as directly ("it's movable, I don't want that") -
        native flyouts aren't draggable either, so this now always computes
        the same corner, full stop.

        Shows the window BEFORE calling refresh() - reported directly (the
        popup either didn't open at all or appeared very late): refresh()
        runs several database queries, which
        can take a noticeable moment while a background sync pass is
        actively hammering the same database, and doing that before show()
        meant the window itself didn't even appear on screen until every
        one of those queries finished. Now the window becomes visible
        immediately (whatever content it already has, stale from last time
        or empty on first run), and refresh() runs right after via the
        event loop's very next iteration instead of blocking the same call
        that makes the window visible - a brief flash of stale content
        beats the window not showing up at all.
        """
        size = self.size()
        x, y = self._anchor_pos(pos, size)
        self._show_pos = pos

        self.move(x, y)
        self.show()
        # Re-asserted on every show, not just the initial one in __init__ -
        # same reasoning as MainWindow's own _show_window(): the
        # ClientMessage form is what actually takes effect for a window
        # being re-shown after a hide, the direct property-set form only
        # reliably applies on first map.
        skip_taskbar(int(self.winId()))
        self.activateWindow()
        self.raise_()
        # No explicit grabMouse() here - a Tool window has no automatic
        # pointer grab to begin with (unlike the old Popup type), and an
        # earlier version's explicit grab was a real, separately-reported
        # bug anyway: it intercepted every mouse event system-wide,
        # including ones over a completely different process's window, so
        # a right-click meant for the tray icon's own context menu (owned
        # by the desktop shell, a separate process) got consumed by this
        # popup instead of ever reaching it.
        self._refresh_timer.start()
        self._idle_close_timer.start(_IDLE_CLOSE_MS)
        self._autohide_armed = False
        self._autohide_grace_timer.start(_AUTOHIDE_GRACE_MS)
        QTimer.singleShot(0, self._refresh_after_show)

    def _refresh_after_show(self) -> None:
        self.refresh()
        self.adjustSize()
        # Content just changed size (first real data instead of whatever
        # was left over from last time) - re-anchor so it still sits
        # flush in its corner instead of drifting from wherever the
        # stale-size version was placed.
        x, y = self._anchor_pos(self._show_pos, self.size())
        self.move(x, y)

    def _note_activity(self, *_args) -> None:
        """Resets the idle-close countdown - connected to anything that
        means "the user is still actively using this popup" (search typing,
        scrolling, a click inside it, a key press). Accepts and ignores
        *args so it can be used directly as a slot for signals that pass
        arguments (textChanged(str), valueChanged(int)) without a lambda
        at each call site."""
        self._idle_close_timer.start(_IDLE_CLOSE_MS)

    def hideEvent(self, event) -> None:
        self._refresh_timer.stop()
        self._idle_close_timer.stop()
        self._autohide_grace_timer.stop()
        self._autohide_debounce_timer.stop()
        self._autohide_armed = False
        super().hideEvent(event)

    def _arm_autohide(self) -> None:
        self._autohide_armed = True

    def _on_app_state_changed(self, state: Qt.ApplicationState) -> None:
        """Real close-on-outside-click. A Tool window only drops behind
        whatever's clicked (normal WM stacking) on its own - this is what
        actually closes it, using the same applicationStateChanged
        mechanism already proven reliable for a regular WM-managed window
        (see MainWindow's own history with it), not the old Popup-era
        focusOutEvent/grab approach that caused a real regression here
        once before."""
        if not self.isVisible() or not self._autohide_armed:
            return
        if state == Qt.ApplicationState.ApplicationActive:
            self._autohide_debounce_timer.stop()
        elif state == Qt.ApplicationState.ApplicationInactive:
            self._autohide_debounce_timer.start(_AUTOHIDE_DEBOUNCE_MS)

    def _check_still_inactive_and_close(self) -> None:
        still_inactive = QApplication.instance().applicationState() != Qt.ApplicationState.ApplicationActive
        if self.isVisible() and still_inactive:
            self.close()

    def keyPressEvent(self, event) -> None:
        """Escape closes the popup - a genuinely reliable dismiss path
        alongside the close button, since keyboard input to a widget that
        currently has it is basic Qt behavior, independent of window type
        or window-manager stacking."""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        self._note_activity()
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        # No outside-bounds check here anymore - a Tool window has no
        # automatic pointer grab (unlike the old Popup type), so this only
        # ever fires for a click genuinely within this window in the first
        # place; a click elsewhere is delivered to whatever window is
        # actually under it, the same as any other regular window.
        self._note_activity()
        super().mousePressEvent(event)

    def _show_account_menu(self) -> None:
        # Settings and View online used to be separate footer links -
        # consolidated here (requested directly) since the account menu is
        # now a real, working thing rather than the decorative chevron it
        # used to be. Pause/resume now has real plumbing behind it
        # (MainWindow._toggle_sync_paused, reusing the same stop/start
        # worker methods sign-out already relies on) - no longer scoped out
        # the way it was before. About lives only in the tray's right-click
        # menu now (requested directly) - not duplicated here.
        menu = QMenu(self)
        toggle_action = None
        if self._on_toggle_sync is not None:
            paused = self._sync_paused_getter() if self._sync_paused_getter else False
            toggle_action = menu.addAction("Resume sync" if paused else "Pause sync")
            menu.addSeparator()
        # Order swapped from Settings/View online to View online/Settings
        # (requested directly).
        view_online_action = menu.addAction("View online") if self._web_url_getter else None
        settings_action = menu.addAction("Settings")
        exit_action = None
        if self._on_exit:
            menu.addSeparator()
            exit_action = menu.addAction("Exit")
        chosen = menu.exec(self._account_menu_btn.mapToGlobal(self._account_menu_btn.rect().bottomLeft()))
        if toggle_action is not None and chosen == toggle_action:
            self._on_toggle_sync()
        elif chosen == settings_action:
            self._open_settings_clicked()
        elif view_online_action is not None and chosen == view_online_action:
            self._view_online()
        elif exit_action is not None and chosen == exit_action:
            self._on_exit()
