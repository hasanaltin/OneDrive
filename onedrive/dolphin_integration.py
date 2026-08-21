import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PLACES_FILE = Path.home() / ".local" / "share" / "user-places.xbel"

# Installed into the "hicolor" theme specifically (not whatever theme the
# user has actually chosen) because hicolor is the one fallback every
# icon-theme-aware app searches regardless of the active theme, per the
# freedesktop icon theme spec - so the Dolphin overlay plugin's
# QIcon::fromTheme(name) lookups for these resolve the same way no matter
# what the user has installed/configured (Papirus, Breeze, nothing at all).
_EMBLEM_DIR = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "emblems"

_EMBLEM_SVGS = {
    "onedrive-status-local": """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <circle cx="8" cy="8" r="7" fill="#2E9E44"/>
  <path d="M4.6 8.3 L6.9 10.6 L11.4 5.6" stroke="white" stroke-width="1.7"
        fill="none" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
""",
    "onedrive-status-cloud": """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <circle cx="8" cy="8" r="7" fill="#0364B8"/>
  <path d="M4.8 10.3 h5.6 a1.7 1.7 0 0 0 0.4-3.35 a2.5 2.5 0 0 0-4.75-0.85
           a1.9 1.9 0 0 0-1.25 4.2 z" fill="white"/>
</svg>
""",
    # Two-arrow refresh/loop symbol (the universal "sync in progress" glyph -
    # Dropbox, Google Drive, Nextcloud all use the same shape) - replaced a
    # single open arc that read as an ambiguous "C" at 16px, after being
    # asked directly for "bir daire ya da dongu isareti" (a circle or a
    # loop/cycle mark) on files that have pending/in-flight changes. Path
    # coordinates computed directly (not hand-drawn) from the same circle
    # geometry gui/theme.py's badge symbols use, verified by rendering both
    # side by side before picking this one.
    "onedrive-status-syncing": """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <circle cx="8" cy="8" r="7" fill="#0364B8"/>
  <path d="M 12.08 6.52 A 4.34 4.34 0 0 0 3.73 7.25" stroke="white"
        stroke-width="1.4" fill="none" stroke-linecap="round"/>
  <path d="M 3.92 9.48 A 4.34 4.34 0 0 0 12.27 8.75" stroke="white"
        stroke-width="1.4" fill="none" stroke-linecap="round"/>
  <polygon points="11.60,5.20 11.03,6.90 12.56,7.83" fill="white"/>
  <polygon points="4.40,10.80 4.97,9.10 3.44,8.17" fill="white"/>
</svg>
""",
}


def install_overlay_emblem_icons() -> None:
    """Writes the small status-badge SVGs the Dolphin overlay-icon plugin
    (packaging/dolphin-overlay/) references by name - idempotent, cheap
    enough to just call unconditionally on every startup rather than
    tracking whether it's already been done."""
    try:
        _EMBLEM_DIR.mkdir(parents=True, exist_ok=True)
        for name, svg in _EMBLEM_SVGS.items():
            path = _EMBLEM_DIR / f"{name}.svg"
            if not path.exists() or path.read_text() != svg:
                path.write_text(svg)
    except OSError:
        logger.exception("failed to install overlay emblem icons")

_BOOKMARK_TEMPLATE = """ <bookmark href="file://{href}">
  <title>{title}</title>
  <info>
   <metadata owner="http://freedesktop.org">
    <bookmark:icon name="folder-onedrive"/>
   </metadata>
   <metadata owner="http://www.kde.org">
    <isSystemItem>false</isSystemItem>
   </metadata>
  </info>
 </bookmark>
</xbel>"""

# Bookmark icon names this app has used over time - kept so an
# already-written bookmark from an older version gets its icon upgraded in
# place instead of being stuck with whatever was current when it was first
# added (add_places_bookmark is otherwise a no-op once the href exists).
_KNOWN_OLD_ICON_NAMES = ("folder-cloud",)


def add_places_bookmark(target: Path, title: str = "OneDrive") -> bool:
    """Adds a Dolphin sidebar (Places) shortcut pointing at `target`, so it
    shows up in the left-hand folder panel. Idempotent - does nothing new if
    a bookmark for this exact path already exists (beyond upgrading a
    stale icon name left over from an older version), or if the Places file
    doesn't exist (e.g. not running under a KDE session)."""
    if not _PLACES_FILE.exists():
        logger.info("No Dolphin Places file found at %s, skipping shortcut", _PLACES_FILE)
        return False

    href = str(target)
    content = _PLACES_FILE.read_text()

    bookmark_start = content.find(f'href="file://{href}"')
    if bookmark_start != -1:
        # Scoped to just this bookmark's own block (from its <bookmark ...>
        # up to the next </bookmark>) - a blind file-wide string replace
        # would also touch any other, unrelated bookmark that happens to
        # use the same old icon name.
        block_end = content.find("</bookmark>", bookmark_start)
        if block_end != -1:
            block_end += len("</bookmark>")
            block = content[bookmark_start:block_end]
            for old_name in _KNOWN_OLD_ICON_NAMES:
                old_icon_tag = f'<bookmark:icon name="{old_name}"/>'
                if old_icon_tag in block:
                    new_block = block.replace(old_icon_tag, '<bookmark:icon name="folder-onedrive"/>')
                    content = content[:bookmark_start] + new_block + content[block_end:]
                    _PLACES_FILE.write_text(content)
                    logger.info(
                        "Upgraded Dolphin Places shortcut icon for %s: %s -> folder-onedrive", href, old_name
                    )
                    break
            else:
                logger.debug("Dolphin Places shortcut for %s already exists", href)
        return False

    if "</xbel>" not in content:
        logger.warning("Unexpected Dolphin Places file format, skipping shortcut")
        return False

    new_block = _BOOKMARK_TEMPLATE.format(href=href, title=title)
    updated = content.replace("</xbel>", new_block)
    _PLACES_FILE.write_text(updated)
    logger.info("Added Dolphin Places shortcut '%s' -> %s", title, href)
    return True
