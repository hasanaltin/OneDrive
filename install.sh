#!/usr/bin/env bash
#
# OneDrive for Linux Client - installer
#
# Automates the steps documented in README.md's Installation/Usage sections:
# creates the venv, installs dependencies, and enables autostart on login.
# Safe to re-run (idempotent) - does not sign in or launch the app itself,
# that's still an interactive step (device-code sign-in).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
AUTOSTART_DIR="$HOME/.config/autostart"
AUTOSTART_FILE="$AUTOSTART_DIR/onedrive.desktop"
APPLICATIONS_DIR="$HOME/.local/share/applications"
APPLICATIONS_FILE="$APPLICATIONS_DIR/onedrive.desktop"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
ICON_FILE="$ICON_DIR/onedrive-linux-client.png"

SKIP_AUTOSTART=0
for arg in "$@"; do
    case "$arg" in
        --skip-autostart) SKIP_AUTOSTART=1 ;;
        -h|--help)
            echo "Usage: $0 [--skip-autostart]"
            echo "  --skip-autostart   Set up the venv/dependencies only, don't enable autostart on login."
            exit 0
            ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

echo "==> Checking prerequisites"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found - install Python 3.11+ first." >&2
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python 3.11+ required, found $(python3 -c 'import sys; print(sys.version.split()[0])')." >&2
    exit 1
fi

if ! ldconfig -p 2>/dev/null | grep -qi 'libfuse3'; then
    echo "Warning: libfuse3 not detected (e.g. 'sudo apt install libfuse3-4' on Debian/Ubuntu," >&2
    echo "'sudo dnf install fuse3-libs' on Fedora). Needed to mount - continuing anyway." >&2
fi

echo "==> Setting up virtual environment at $VENV_DIR"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv --system-site-packages "$VENV_DIR"
else
    echo "    already exists, reusing"
fi

echo "==> Installing dependencies"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"

if ! "$VENV_DIR/bin/python3" -c "import PyQt6" >/dev/null 2>&1; then
    echo "==> PyQt6 not available via --system-site-packages, installing it into the venv"
    "$VENV_DIR/bin/pip" install PyQt6
fi

echo "==> Generating app icon"
mkdir -p "$ICON_DIR"
# Same lookup the running app uses for its tray/window icon (theme.py's
# app_icon()): prefers the OneDrive-styled icon an installed icon theme
# ships (e.g. Papirus's "ms-onedrive") so the Applications-menu/autostart
# entry matches what actually shows in the tray, falling back to a plain
# self-drawn cloud on a system without such a theme (most don't ship a
# "folder-onedrive"/"ms-onedrive" icon, which is why that fallback exists).
QT_QPA_PLATFORM=offscreen "$VENV_DIR/bin/python3" - <<PYEOF
import sys
sys.path.insert(0, "$REPO_DIR")
from PyQt6.QtGui import QGuiApplication
app = QGuiApplication(sys.argv)
from onedrive.gui.theme import app_icon
app_icon(256).pixmap(256, 256).save("$ICON_FILE")
PYEOF

RESOLVED_DESKTOP_FILE="$(mktemp)"
sed \
    -e "s#^Exec=.*#Exec=$VENV_DIR/bin/python3 -m onedrive#" \
    -e "s#^Path=.*#Path=$REPO_DIR#" \
    -e "s#^Icon=.*#Icon=$ICON_FILE#" \
    "$REPO_DIR/onedrive.desktop" > "$RESOLVED_DESKTOP_FILE"

# Application-menu entry (so it shows up in the Applications menu / KRunner /
# any other launcher, not just as something that happens to autostart) -
# ~/.config/autostart is scanned only by login-autostart mechanisms, a
# completely separate location from ~/.local/share/applications, which is
# what every desktop's actual app launcher/search scans. Installed
# unconditionally - a user might want it in the menu even with
# --skip-autostart.
echo "==> Adding an Applications menu entry"
mkdir -p "$APPLICATIONS_DIR"
cp "$RESOLVED_DESKTOP_FILE" "$APPLICATIONS_FILE"
chmod 644 "$APPLICATIONS_FILE"
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
elif command -v kbuildsycoca5 >/dev/null 2>&1; then
    kbuildsycoca5 --noincremental >/dev/null 2>&1 || true
fi
update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true

if [ "$SKIP_AUTOSTART" -eq 0 ]; then
    echo "==> Enabling autostart on login"
    mkdir -p "$AUTOSTART_DIR"
    cp "$RESOLVED_DESKTOP_FILE" "$AUTOSTART_FILE"
    chmod 644 "$AUTOSTART_FILE"
else
    echo "==> Skipping autostart setup (--skip-autostart)"
fi
rm -f "$RESOLVED_DESKTOP_FILE"

echo ""
CLIENT_ID_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/OneDrive/client_id"
if [ ! -s "$CLIENT_ID_FILE" ] && [ -z "${ONEDRIVE_NATIVE_CLIENT_ID:-}" ]; then
    echo "Done. One more required step before signing in - this app has no Azure app"
    echo "identity built in, so register your own first:"
    echo "  ./register_azure_app.sh"
    echo ""
fi
echo "To sign in and mount your OneDrive:"
echo "  source \"$VENV_DIR/bin/activate\""
echo "  python -m onedrive"
if [ "$SKIP_AUTOSTART" -eq 0 ]; then
    echo ""
    echo "It will also start automatically on your next login."
fi
