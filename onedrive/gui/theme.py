import math
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QTransform,
)

BRAND_BLUE = "#0364B8"
BRAND_BLUE_DARK = "#014A85"
GREEN = "#2E9E44"
RED = "#C0392B"
GRAY = "#8A8A8A"


def cloud_pixmap(size: int = 64, color: str = BRAND_BLUE) -> QPixmap:
    """This project's own original icon shape - entirely self-drawn with
    QPainter, no external image file and no dependency on any installed
    icon theme. Requested directly ("can you create our own icon and use
    it everywhere?") after settling the question of whether to use
    Microsoft's actual trademarked logo (no - this app has no license to
    redistribute it) or an icon theme's own interpretation of one (also
    no, in the end). A follow-up request ("change the color of the cloud
    instead of changing background... lets try without background")
    dropped the circular badge this originally had in favor of just the
    cloud silhouette on its own - color is parameterized so the same
    shape can be recolored for different states (see app_icon() for blue/
    online, gray_tray_icon() for gray/offline-or-paused)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)

    cloud = QPainterPath()
    # WindingFill, not the QPainterPath default (OddEven) - with 3 circles
    # overlapping each other and the body rect, OddEven treats any point
    # covered an even number of times as a hole, which produced a visible
    # pinwheel of gaps between the lobes at their mutual overlap.
    # WindingFill just treats "covered by anything" as filled, which is
    # what a simple shape union actually needs.
    cloud.setFillRule(Qt.FillRule.WindingFill)
    cloud.addRoundedRect(QRectF(size * 0.06, size * 0.56, size * 0.88, size * 0.30), size * 0.15, size * 0.15)
    cloud.addEllipse(QRectF(size * 0.04, size * 0.32, size * 0.40, size * 0.40))
    cloud.addEllipse(QRectF(size * 0.24, size * 0.10, size * 0.52, size * 0.52))
    cloud.addEllipse(QRectF(size * 0.56, size * 0.32, size * 0.40, size * 0.40))
    painter.drawPath(cloud.simplified())
    painter.end()
    return pixmap


def cloud_icon(size: int = 64, color: str = BRAND_BLUE) -> QIcon:
    return QIcon(cloud_pixmap(size, color))


def app_icon(size: int = 64) -> QIcon:
    """The app's normal (online, syncing) identity icon - this project's
    own self-drawn cloud shape in brand blue, no background badge. See
    gray_tray_icon() for the gray variant shown while offline or paused."""
    return cloud_icon(size, BRAND_BLUE)


def gray_tray_icon(size: int = 64) -> QIcon:
    """The tray icon while offline - the exact same cloud shape as
    app_icon(), just gray instead of blue (requested directly - "when it
    is online lets have a blue icon, when it is offline or paused show
    gray icon"), matching the common sync-client convention that a colored
    icon means "active and reachable" and gray means "not doing anything
    right now". See paused_tray_icon() for the distinct pause-badged
    variant now used specifically when sync itself is paused (manually or
    by an auto-pause setting), as opposed to genuinely offline."""
    return cloud_icon(size, GRAY)


def paused_tray_icon(size: int = 64) -> QIcon:
    """The tray icon while sync is paused (manual pause, or the metered-
    connection/battery-saver auto-pause settings) - requested directly
    ("when onedrive is paused use pause icon on onedrive"), distinct from
    gray_tray_icon()'s plain gray recolor for "offline," which isn't a
    choice the app or user made. Same gray cloud shape as gray_tray_icon(),
    with two bold white bars (the universal media "pause" glyph) drawn
    over it - white rather than the badge system's usual colored-symbol-
    with-white-halo, since the goal here is a bar shape read clearly at
    real system-tray render sizes (often scaled down to ~16-24px), not a
    small corner badge that would disappear at that scale."""
    pixmap = cloud_pixmap(size, GRAY)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    bar_w = size * 0.16
    bar_h = size * 0.40
    gap = size * 0.12
    cy = size * 0.54  # centered on the cloud body, not the full canvas
    left_x = size / 2 - gap / 2 - bar_w
    right_x = size / 2 + gap / 2

    halo_pen = QPen(QColor(GRAY))
    halo_pen.setWidth(max(2, int(size * 0.05)))
    painter.setPen(halo_pen)
    painter.setBrush(QColor("white"))
    radius = bar_w * 0.3
    painter.drawRoundedRect(QRectF(left_x, cy - bar_h / 2, bar_w, bar_h), radius, radius)
    painter.drawRoundedRect(QRectF(right_x, cy - bar_h / 2, bar_w, bar_h), radius, radius)

    painter.end()
    return QIcon(pixmap)


def folder_pixmap(size: int = 24, color: str = "white") -> QPixmap:
    """A plain folder glyph, self-drawn for the same reason as cloud_pixmap:
    QIcon.fromTheme("folder") renders in whatever color the system icon
    theme happens to use (often a dark/muted tone meant for a light
    background), which reads as nearly invisible against this app's dark
    blue header bar."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)

    path = QPainterPath()
    path.addRoundedRect(QRectF(size * 0.08, size * 0.16, size * 0.40, size * 0.14), size * 0.03, size * 0.03)
    path.addRoundedRect(QRectF(size * 0.08, size * 0.28, size * 0.84, size * 0.58), size * 0.05, size * 0.05)
    painter.drawPath(path.simplified())
    painter.end()
    return pixmap


def folder_icon(size: int = 24, color: str = "white") -> QIcon:
    return QIcon(folder_pixmap(size, color))


FOLDER_COLOR = "#EBA937"


def _page_pixmap(size: int, color: str, glyph_color: str = "white") -> tuple[QPixmap, QPainter]:
    """Base 'file' page shape (a rounded rect with a folded top-right
    corner), self-drawn for the same reason as folder_pixmap/cloud_pixmap:
    QIcon.fromTheme() silently returns a null icon for most of these names
    on this system (confirmed - every activity-list row rendered with the
    exact same fallback glyph regardless of file type), so every file-type
    icon here is built from this one shared shape instead, just tinted and
    marked differently per type. Returns the still-open QPainter too, so a
    caller can draw a small glyph on top before calling .end() itself."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)

    fold = size * 0.30
    body = QPainterPath()
    body.moveTo(size * 0.14, size * 0.04)
    body.lineTo(size * 0.86 - fold, size * 0.04)
    body.lineTo(size * 0.86, size * 0.04 + fold)
    body.lineTo(size * 0.86, size * 0.96)
    body.lineTo(size * 0.14, size * 0.96)
    body.closeSubpath()
    painter.setBrush(QColor(color))
    painter.drawPath(body)

    corner = QPainterPath()
    corner.moveTo(size * 0.86 - fold, size * 0.04)
    corner.lineTo(size * 0.86, size * 0.04 + fold)
    corner.lineTo(size * 0.86 - fold, size * 0.04 + fold)
    corner.closeSubpath()
    painter.setBrush(QColor(color).darker(140))
    painter.drawPath(corner)

    painter.setBrush(QColor(glyph_color))
    # A real pen, not the NoPen left over from drawing the page shape's
    # outline-free fill above - drawText() (used by pdf_pixmap and the
    # letter-mark icons) draws with the pen, not the brush, so it was
    # silently invisible this whole time under NoPen (same bug class as
    # badged_pixmap's symbols, confirmed the exact same way: rendered to a
    # PNG and looked at it directly). Callers that only fill brush shapes
    # (image_pixmap's mountains, audio_pixmap's notes, etc.) are unaffected
    # - same color as the fill, so an outline stroke in that color is
    # invisible against it either way.
    painter.setPen(QColor(glyph_color))
    return pixmap, painter


def file_pixmap(size: int = 36, color: str = "#7C93A8") -> QPixmap:
    """Generic/text file - a plain page with a few text-line strokes."""
    pixmap, painter = _page_pixmap(size, color)
    line_x0, line_x1 = size * 0.28, size * 0.66
    for frac in (0.50, 0.64, 0.78):
        painter.drawRoundedRect(
            QRectF(line_x0, size * frac, line_x1 - line_x0, size * 0.045), size * 0.02, size * 0.02
        )
    painter.end()
    return pixmap


def image_pixmap(size: int = 36, color: str = "#4CAF50") -> QPixmap:
    """Picture - a page with a small mountain-and-sun glyph, the near-
    universal "image" pictogram."""
    pixmap, painter = _page_pixmap(size, color)
    painter.setBrush(QColor("white"))
    painter.drawEllipse(QRectF(size * 0.32, size * 0.42, size * 0.13, size * 0.13))
    mountains = QPainterPath()
    mountains.moveTo(size * 0.22, size * 0.78)
    mountains.lineTo(size * 0.40, size * 0.54)
    mountains.lineTo(size * 0.52, size * 0.68)
    mountains.lineTo(size * 0.62, size * 0.56)
    mountains.lineTo(size * 0.78, size * 0.78)
    mountains.closeSubpath()
    painter.drawPath(mountains)
    painter.end()
    return pixmap


def pdf_pixmap(size: int = 36, color: str = "#D9534F") -> QPixmap:
    pixmap, painter = _page_pixmap(size, color)
    font = QFont()
    font.setPointSizeF(size * 0.19)
    font.setWeight(QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(QRectF(size * 0.14, size * 0.55, size * 0.72, size * 0.32), Qt.AlignmentFlag.AlignCenter, "PDF")
    painter.end()
    return pixmap


def _letter_mark_pixmap(size: int, color: str, letter: str) -> QPixmap:
    """A bold single-letter mark on the page shape - the same general
    visual language Microsoft's own Office file-type icons use (a
    brand-colored mark identifying the app, not the document's content)
    rather than a literal content pictogram. Requested directly, with a
    screenshot of the real OneDrive Windows client as the reference - not
    a copy of Microsoft's actual trademarked logo glyphs, just the same
    letter+color convention applied to this app's own hand-drawn page
    shape (see _page_pixmap's own docstring for why everything here is
    self-drawn instead of pulled from a system icon theme)."""
    pixmap, painter = _page_pixmap(size, color)
    font = QFont()
    font.setPointSizeF(size * 0.42)
    font.setWeight(QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(QRectF(size * 0.14, size * 0.40, size * 0.72, size * 0.52), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    return pixmap


def doc_pixmap(size: int = 36, color: str = "#2B6FCE") -> QPixmap:
    return _letter_mark_pixmap(size, color, "W")


def sheet_pixmap(size: int = 36, color: str = "#1D8348") -> QPixmap:
    return _letter_mark_pixmap(size, color, "X")


def slides_pixmap(size: int = 36, color: str = "#D06B1F") -> QPixmap:
    return _letter_mark_pixmap(size, color, "P")


def archive_pixmap(size: int = 36, color: str = "#8D6E63") -> QPixmap:
    """Archive - a page with a small zipper stripe of ticks."""
    pixmap, painter = _page_pixmap(size, color)
    for i in range(4):
        y = size * (0.46 + i * 0.10)
        painter.drawRect(QRectF(size * 0.47, y, size * 0.10, size * 0.05))
    painter.end()
    return pixmap


def audio_pixmap(size: int = 36, color: str = "#7E57C2") -> QPixmap:
    pixmap, painter = _page_pixmap(size, color)
    painter.drawEllipse(QRectF(size * 0.30, size * 0.68, size * 0.12, size * 0.10))
    painter.drawEllipse(QRectF(size * 0.52, size * 0.62, size * 0.12, size * 0.10))
    painter.drawRect(QRectF(size * 0.41, size * 0.42, size * 0.03, size * 0.30))
    painter.drawRect(QRectF(size * 0.63, size * 0.36, size * 0.03, size * 0.30))
    painter.drawRoundedRect(QRectF(size * 0.41, size * 0.40, size * 0.25, size * 0.05), size * 0.02, size * 0.02)
    painter.end()
    return pixmap


def video_pixmap(size: int = 36, color: str = "#E0457B") -> QPixmap:
    """Video - a page with a small play triangle."""
    pixmap, painter = _page_pixmap(size, color)
    triangle = QPainterPath()
    triangle.moveTo(size * 0.42, size * 0.48)
    triangle.lineTo(size * 0.42, size * 0.76)
    triangle.lineTo(size * 0.66, size * 0.62)
    triangle.closeSubpath()
    painter.drawPath(triangle)
    painter.end()
    return pixmap


_TYPE_PIXMAP_FUNCS = {
    "image": image_pixmap,
    "pdf": pdf_pixmap,
    "doc": doc_pixmap,
    "sheet": sheet_pixmap,
    "slides": slides_pixmap,
    "archive": archive_pixmap,
    "audio": audio_pixmap,
    "video": video_pixmap,
}

_EXT_CATEGORY = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".bmp": "image", ".svg": "image", ".webp": "image", ".heic": "image",
    ".pdf": "pdf",
    ".doc": "doc", ".docx": "doc", ".odt": "doc", ".rtf": "doc",
    ".xls": "sheet", ".xlsx": "sheet", ".ods": "sheet", ".csv": "sheet",
    ".ppt": "slides", ".pptx": "slides", ".odp": "slides",
    ".zip": "archive", ".tar": "archive", ".gz": "archive", ".rar": "archive",
    ".7z": "archive", ".xz": "archive",
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".ogg": "audio", ".m4a": "audio",
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".avi": "video", ".webm": "video",
}

# freedesktop.org standard names for each category - what an actual icon
# theme (Papirus, Adwaita, breeze, ...) ships under, when one is installed
# and configured. Falls back to the self-drawn pixmaps above when it isn't.
_THEME_ICON_NAMES = {
    "image": "image-x-generic",
    "pdf": "application-pdf",
    "doc": "x-office-document",
    "sheet": "x-office-spreadsheet",
    "slides": "x-office-presentation",
    "archive": "package-x-generic",
    "audio": "audio-x-generic",
    "video": "video-x-generic",
}


def icon_for_item(name: str, is_folder: bool, size: int = 36) -> QIcon:
    """The one place file/folder icons are picked for the whole app. Prefers
    the user's actual system icon theme when one resolves - it looks native,
    matches whatever theme/variant they've chosen, and generally has more
    polished artwork than anything drawn here. Falls back to a self-drawn
    icon (colored and marked per category, so a file/folder/picture still
    read as distinct from each other) whenever the theme doesn't provide a
    given name, which QIcon.fromTheme() does for literally every name this
    function asks for on a system with no icon theme installed/configured
    at all - a real, not-hypothetical case (confirmed on this exact machine
    before Papirus was installed), and one that can't be assumed away just
    because it happens not to be true right now."""
    if is_folder:
        theme_icon = QIcon.fromTheme("folder")
        if not theme_icon.isNull():
            return theme_icon
        return QIcon(folder_pixmap(size, FOLDER_COLOR))

    category = _EXT_CATEGORY.get(Path(name).suffix.lower())
    theme_icon = QIcon.fromTheme(_THEME_ICON_NAMES.get(category, "text-x-generic"))
    if not theme_icon.isNull():
        return theme_icon

    func = _TYPE_PIXMAP_FUNCS.get(category, file_pixmap)
    return QIcon(func(size))


def initials_avatar(text: str, size: int = 44) -> QPixmap:
    """A circular initials avatar (no real profile photo available) -
    same idea as Nextcloud/Teams/etc. fall back to when there's no picture."""
    initials = "".join(w[0] for w in text.split()[:2]).upper() or "?"
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#FFFFFF"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, size, size)
    painter.setPen(QColor(BRAND_BLUE))
    font = QFont()
    font.setPointSize(int(size * 0.36))
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, initials)
    painter.end()
    return pixmap


def photo_avatar(data: bytes, size: int = 44) -> QPixmap | None:
    """A circular profile photo, cropped and clipped to the same footprint
    initials_avatar() produces - the real counterpart to it, used once an
    actual photo has been fetched via Graph (see MainWindow._refresh_avatar_
    async). Returns None if the bytes can't be decoded as an image at all
    (corrupt/partial download, unexpected content type), so callers can fall
    back to initials_avatar() the same way they would with no photo at all."""
    source = QPixmap()
    if not source.loadFromData(data):
        return None
    # KeepAspectRatioByExpanding fills the target square and crops the
    # overflow (matches every other "avatar" convention - Teams, Nextcloud,
    # etc.) rather than letterboxing a non-square source photo.
    scaled = source.scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
    )
    x = max(0, (scaled.width() - size) // 2)
    y = max(0, (scaled.height() - size) // 2)
    cropped = scaled.copy(x, y, size, size)

    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, size, size))
    painter.setClipPath(clip)
    painter.drawPixmap(0, 0, cropped)
    painter.end()
    return result


def _draw_badge_symbol(painter: QPainter, badge: str, cx: float, cy: float, s: float, pen: QPen) -> None:
    """Draws just the symbol's lines/arc/path for `badge` at (cx, cy),
    scaled to `s`, using whatever pen is already set. Called twice by
    badged_pixmap() - once with a wide white "halo" pen for contrast
    against the file icon behind it, once with the real colored pen on
    top - so it's factored out here rather than duplicated."""
    painter.setPen(pen)
    if badge == "deleted":
        painter.drawLine(int(cx - s * 0.32), int(cy - s * 0.32), int(cx + s * 0.32), int(cy + s * 0.32))
        painter.drawLine(int(cx + s * 0.32), int(cy - s * 0.32), int(cx - s * 0.32), int(cy + s * 0.32))
    elif badge == "created":
        painter.drawLine(int(cx - s * 0.32), int(cy), int(cx + s * 0.32), int(cy))
        painter.drawLine(int(cx), int(cy - s * 0.32), int(cx), int(cy + s * 0.32))
    elif badge in ("uploaded", "downloaded"):
        top, bottom = cy - s * 0.32, cy + s * 0.32
        start, end = (bottom, top) if badge == "uploaded" else (top, bottom)
        painter.drawLine(int(cx), int(start), int(cx), int(end))
        arrow_y = top if badge == "uploaded" else bottom
        direction = 1 if badge == "uploaded" else -1
        painter.drawLine(int(cx), int(arrow_y), int(cx - s * 0.22), int(arrow_y + direction * s * 0.22))
        painter.drawLine(int(cx), int(arrow_y), int(cx + s * 0.22), int(arrow_y + direction * s * 0.22))
    elif badge == "changed":
        # Solid dot - reuses "cloud"'s own halo technique (a bigger white
        # circle for the halo pass, normal size for the color pass) since
        # a plain filled circle has no interior line for the generic
        # stroke pen to trace. Distinct from "uploaded"/"downloaded"'s
        # arrows - those are reserved for a file's first trip to/from the
        # cloud (a brand-new local file, or a cloud-only placeholder being
        # fetched); this is for an ordinary edit to a file that was
        # already synced both ways. Reported directly: editing an
        # existing, already-synced file was showing the "uploaded" arrow
        # in the activity list, which reads as "this just arrived new" -
        # wrong for a routine change to a file you already had ("Bu
        # dosyada degisiklik yaptigim icin degisiklik iconu gorunmesi
        # lazim" - since I made a change to this file, the change icon
        # should show).
        r = s * 0.30
        is_halo = pen.color() == QColor("white") and pen.width() > 3
        draw_r = r * 1.35 if is_halo else r
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pen.color())
        painter.drawEllipse(QPointF(cx, cy), draw_r, draw_r)
    elif badge == "syncing":
        # Two-arrow refresh/loop symbol - the same universal "sync in
        # progress" glyph used for the Dolphin overlay-icon emblems (see
        # dolphin_integration.py's onedrive-status-syncing SVG, built from
        # this exact circle geometry) - replaced a single open arc that
        # read as an ambiguous "C" at small sizes, after being asked
        # directly for "bir daire ya da dongu isareti" (a circle or a
        # loop/cycle mark) on files with pending/in-flight changes.
        ar = s * 0.42
        rect = QRectF(cx - ar, cy - ar, ar * 2, ar * 2)
        painter.drawArc(rect, 20 * 16, 150 * 16)
        painter.drawArc(rect, 200 * 16, 150 * 16)
        aw, ah = s * 0.20, s * 0.16

        def _arrow(deg: float) -> QPolygonF:
            rad = math.radians(deg)
            tipx, tipy = cx + ar * math.cos(rad), cy - ar * math.sin(rad)
            tan, perp = math.radians(deg + 90), math.radians(deg)
            return QPolygonF([
                QPointF(tipx + aw * math.cos(tan), tipy - aw * math.sin(tan)),
                QPointF(tipx - ah * math.cos(perp), tipy + ah * math.sin(perp)),
                QPointF(tipx - aw * math.cos(tan), tipy + aw * math.sin(tan)),
            ])

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pen.color())
        painter.drawPolygon(_arrow(20))
        painter.drawPolygon(_arrow(200))
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif badge == "cloud":
        # A filled silhouette, scaled up ~28% for the halo pass and drawn
        # at normal size for the color pass on top - stroking this shape
        # with the same thick pen used for the simple line symbols just
        # blobbed the whole cloud outline together into a solid circle
        # (too much pen width for how fine this particular shape's detail
        # is), so it needs its own approach rather than reusing
        # _draw_badge_symbol's generic stroke.
        r = s * 0.5
        cloud = QPainterPath()
        cloud.addRoundedRect(QRectF(cx - r * 0.20, cy - r * 0.08, r * 0.95, r * 0.45), r * 0.2, r * 0.2)
        cloud.addEllipse(QRectF(cx - r * 0.30, cy - r * 0.32, r * 0.52, r * 0.52))
        cloud.addEllipse(QRectF(cx + r * 0.05, cy - r * 0.44, r * 0.60, r * 0.60))
        shape = cloud.simplified()
        is_halo = pen.color() == QColor("white") and pen.width() > 3
        if is_halo:
            t = QTransform()
            t.translate(cx, cy)
            t.scale(1.30, 1.30)
            t.translate(-cx, -cy)
            shape = t.map(shape)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pen.color())
        painter.drawPath(shape)
    else:
        painter.drawLine(int(cx - s * 0.3), int(cy), int(cx - s * 0.05), int(cy + s * 0.25))
        painter.drawLine(int(cx - s * 0.05), int(cy + s * 0.25), int(cx + s * 0.32), int(cy - s * 0.28))


def badged_pixmap(icon: QIcon, size: int = 28, badge: str = "check") -> QPixmap:
    """File icon with a small badge symbol in the bottom right corner -
    no background circle, just a bold colored symbol with a white halo
    stroke behind it for contrast against whatever's underneath. Picked
    directly out of 5 side-by-side style options after the original
    filled-circle look was rejected (didn't like that one). `badge` is
    one of:
      "check"      - green checkmark, the default: present on disk, no
                     more specific event to call out
      "syncing"    - blue arc, actively being uploaded/downloaded right now
      "cloud"      - blue cloud outline, only on OneDrive, not yet
                     downloaded to this device (the on-demand mount's
                     placeholder entries, before content is actually
                     fetched)
      "deleted"    - red X
      "created"    - green +
      "changed"    - blue dot: an ordinary edit to a file that was already
                     synced (not its first upload/download)
      "uploaded"   - blue up arrow: a file's first trip up to the cloud
      "downloaded" - blue down arrow: a file's first trip down from the
                     cloud
    """
    base = icon.pixmap(size, size)
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.drawPixmap(0, 0, base)

    badge_color = {
        "deleted": RED,
        "syncing": BRAND_BLUE,
        "cloud": BRAND_BLUE,
        "changed": BRAND_BLUE,
        "uploaded": BRAND_BLUE,
        "downloaded": BRAND_BLUE,
    }.get(badge, GREEN)

    s = size * 0.5
    cx, cy = size - s / 2, size - s / 2

    halo_pen = QPen(QColor("white"))
    halo_pen.setWidth(max(3, int(s * 0.30)))
    halo_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    halo_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    _draw_badge_symbol(painter, badge, cx, cy, s, halo_pen)

    color_pen = QPen(QColor(badge_color))
    color_pen.setWidth(max(2, int(s * 0.18)))
    color_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    color_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    _draw_badge_symbol(painter, badge, cx, cy, s, color_pen)

    painter.end()
    return result
