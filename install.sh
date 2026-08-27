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

# Package-manager detection, used below to actually install missing system
# packages (libfuse3, the venv module on distros that split it out of
# python3 itself, and pyfuse3's native build dependencies) instead of just
# printing a warning and hoping.
PKG_INSTALL=""
FUSE_PKG=""
VENV_PKG=""
BUILD_PKGS=""
PYDEV_PKG=""
if command -v apt-get >/dev/null 2>&1; then
    PKG_INSTALL="sudo apt-get install -y"
    FUSE_PKG="libfuse3-4"
    # Debian/Ubuntu split both ensurepip and the Python.h headers out of
    # python3 itself into version-specific packages (e.g. python3.14-venv,
    # python3.14-dev) - the generic python3-venv/python3-dev metapackages
    # only pull those in for whichever version is currently the distro's
    # "default python3", which may not be the one actually running this
    # script (e.g. a newer interpreter from a PPA). Try the exact match
    # first, fall back to the generic name.
    PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    VENV_PKG="python${PYVER}-venv python3-venv"
    PYDEV_PKG="python${PYVER}-dev python3-dev"
    BUILD_PKGS="build-essential pkg-config libfuse3-dev"
elif command -v dnf >/dev/null 2>&1; then
    PKG_INSTALL="sudo dnf install -y"
    FUSE_PKG="fuse3-libs"
    BUILD_PKGS="gcc pkgconf-pkg-config fuse3-devel python3-devel"
elif command -v pacman >/dev/null 2>&1; then
    PKG_INSTALL="sudo pacman -S --needed --noconfirm"
    FUSE_PKG="fuse3"
    BUILD_PKGS="base-devel pkgconf fuse3"
elif command -v zypper >/dev/null 2>&1; then
    PKG_INSTALL="sudo zypper install -y"
    FUSE_PKG="libfuse3-3"
    BUILD_PKGS="gcc pkg-config libfuse3-devel python3-devel"
fi

LDCONFIG_OUT="$(ldconfig -p 2>/dev/null || true)"
if ! grep -qi 'libfuse3' <<< "$LDCONFIG_OUT"; then
    if [ -n "$PKG_INSTALL" ] && [ -n "$FUSE_PKG" ]; then
        echo "==> libfuse3 not found, installing $FUSE_PKG"
        $PKG_INSTALL "$FUSE_PKG"
    else
        echo "Warning: libfuse3 not detected and no supported package manager found to install it" >&2
        echo "automatically - install it manually (e.g. 'libfuse3-4' on Debian/Ubuntu, 'fuse3-libs'" >&2
        echo "on Fedora). Needed to mount - continuing anyway." >&2
    fi
fi

# pyfuse3 is a Cython/C extension with no prebuilt PyPI wheel for every
# Python version - pip falls back to compiling it from source (as it just
# did against this interpreter), which needs a C compiler, pkg-config,
# libfuse3's own headers/.pc file, and the Python.h headers (all separate
# "-dev"/"-devel" packages from their runtime-only counterparts).
PY_INCLUDE_DIR="$(python3 -c 'import sysconfig; print(sysconfig.get_path("include"))')"
NEED_BUILD_DEPS=0
if ! command -v pkg-config >/dev/null 2>&1; then
    NEED_BUILD_DEPS=1
elif ! pkg-config --exists fuse3; then
    NEED_BUILD_DEPS=1
fi
if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1; then
    NEED_BUILD_DEPS=1
fi
if [ ! -f "$PY_INCLUDE_DIR/Python.h" ]; then
    NEED_BUILD_DEPS=1
fi
if [ "$NEED_BUILD_DEPS" -eq 1 ]; then
    if [ -n "$PKG_INSTALL" ] && [ -n "$BUILD_PKGS" ]; then
        echo "==> Build tools for compiling pyfuse3 not fully present, installing: $BUILD_PKGS"
        $PKG_INSTALL $BUILD_PKGS
    else
        echo "Warning: a C compiler / pkg-config / libfuse3 headers may be missing and no supported" >&2
        echo "package manager was found to install them - pyfuse3 may fail to build from source." >&2
    fi
    if [ ! -f "$PY_INCLUDE_DIR/Python.h" ] && [ -n "$PKG_INSTALL" ] && [ -n "$PYDEV_PKG" ]; then
        echo "==> Python.h still missing, trying Python dev headers ($PYDEV_PKG)"
        PYDEV_INSTALLED=0
        for pkg in $PYDEV_PKG; do
            if $PKG_INSTALL "$pkg"; then
                PYDEV_INSTALLED=1
                break
            fi
        done
        if [ "$PYDEV_INSTALLED" -eq 0 ]; then
            echo "Could not install any of: $PYDEV_PKG - install Python's dev headers manually." >&2
        fi
    fi
fi

echo "==> Setting up virtual environment at $VENV_DIR"
# A directory can exist without being a working venv - e.g. a previous run
# whose `python3 -m venv` failed partway through (no bin/pip ever created),
# or a venv copied from another machine without its file permissions
# (bin/pip present but not executable). Either way, `-x` catches it and
# triggers a clean recreate instead of failing later at the pip install step.
if [ -d "$VENV_DIR" ] && [ ! -x "$VENV_DIR/bin/pip" ]; then
    echo "    existing venv at $VENV_DIR looks incomplete or broken (no working bin/pip) - recreating"
    rm -rf "$VENV_DIR"
fi
if [ ! -d "$VENV_DIR" ]; then
    if ! python3 -m venv --system-site-packages "$VENV_DIR" 2>/tmp/onedrive-venv-err.$$; then
        cat /tmp/onedrive-venv-err.$$ >&2
        rm -f /tmp/onedrive-venv-err.$$
        if [ -n "$PKG_INSTALL" ] && [ -n "$VENV_PKG" ]; then
            echo "==> venv creation failed, trying to install venv support ($VENV_PKG)"
            VENV_PKG_INSTALLED=0
            for pkg in $VENV_PKG; do
                if $PKG_INSTALL "$pkg"; then
                    VENV_PKG_INSTALLED=1
                    break
                fi
            done
            if [ "$VENV_PKG_INSTALLED" -eq 0 ]; then
                echo "Could not install any of: $VENV_PKG - install one manually and re-run." >&2
                exit 1
            fi
            python3 -m venv --system-site-packages "$VENV_DIR"
        else
            echo "python3's venv module is required and could not be auto-installed - install it" >&2
            echo "manually for your distro and re-run this script." >&2
            exit 1
        fi
    else
        rm -f /tmp/onedrive-venv-err.$$
    fi
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
    echo "identity built in, so register your own first."
    echo ""
    # Stdin itself may not be a tty here even in a genuinely interactive run -
    # e.g. bootstrap.sh's `curl ... | bash` pipes this whole script in on fd
    # 0, so `[ -t 0 ]` reads as false and a plain `read` would just get EOF
    # instantly. /dev/tty is the actual controlling terminal, still reachable
    # as long as someone's watching the run live, so ask there instead and
    # only fall back to silently printing the manual step when there's truly
    # no terminal attached at all (a real headless/CI run).
    if [ -r /dev/tty ] && [ -w /dev/tty ]; then
        read -r -p "Register an Azure app now? [Y/n] " RUN_AZURE_REG < /dev/tty > /dev/tty
        case "$RUN_AZURE_REG" in
            [nN]*)
                echo "Skipping - run it yourself later with:"
                echo "  ./register_azure_app.sh"
                ;;
            *)
                if ! bash "$REPO_DIR/register_azure_app.sh" < /dev/tty; then
                    echo "register_azure_app.sh exited with an error - you can re-run it manually:" >&2
                    echo "  ./register_azure_app.sh" >&2
                fi
                ;;
        esac
    else
        echo "No terminal attached to ask interactively - run it yourself when ready:"
        echo "  ./register_azure_app.sh"
    fi
    echo ""
fi
echo "To sign in and mount your OneDrive:"
echo "  source \"$VENV_DIR/bin/activate\""
echo "  python -m onedrive"
if [ "$SKIP_AUTOSTART" -eq 0 ]; then
    echo ""
    echo "It will also start automatically on your next login."
fi
