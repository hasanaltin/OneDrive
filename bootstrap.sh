#!/usr/bin/env bash
#
# OneDrive for Linux Client - one-command bootstrap
#
# Sets this project up on a fresh machine in one step:
#   curl -fsSL https://raw.githubusercontent.com/hasanaltin/OneDrive/main/bootstrap.sh | bash
#
# Clones the repo into ~/onedrive-linux-client and runs its own install.sh,
# which installs system/Python dependencies, creates the venv, and enables
# autostart on login. Safe to re-run - an existing checkout is updated with
# `git pull` instead of being re-cloned.
#
# What this can't automate away (both are one-time, interactive by design):
#   1. Registering your own Azure app (register_azure_app.sh) - opens a
#      browser sign-in with an account that can create app registrations.
#   2. The app's own first sign-in (device code, shown on first launch).
# Once both are done once, every later launch - including on login via
# autostart - signs in and mounts ~/OneDrive automatically, no further
# steps needed.
set -euo pipefail

REPO_URL="https://github.com/hasanaltin/OneDrive.git"
CLONE_DIR="$HOME/onedrive-linux-client"

echo "==> Checking for git"
if ! command -v git >/dev/null 2>&1; then
    echo "    not found - installing"
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y git
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y git
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed --noconfirm git
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y git
    else
        echo "Could not detect a package manager to install git automatically." >&2
        echo "Install git yourself, then re-run this script." >&2
        exit 1
    fi
fi

if [ -d "$CLONE_DIR/.git" ]; then
    echo "==> $CLONE_DIR already exists - updating it instead of cloning"
    git -C "$CLONE_DIR" pull --ff-only
elif [ -e "$CLONE_DIR" ]; then
    echo "$CLONE_DIR already exists and isn't a git checkout of this project." >&2
    echo "Remove or rename it, then re-run this script." >&2
    exit 1
else
    echo "==> Cloning into $CLONE_DIR"
    git clone "$REPO_URL" "$CLONE_DIR"
fi

echo "==> Running install.sh"
cd "$CLONE_DIR"
# Piped into this shell's own stdin (not a tty), so install.sh's interactive
# "run register_azure_app.sh now?" prompt correctly detects that via its own
# `[ -t 0 ]` check and just prints the manual next step instead of hanging.
./install.sh

cat <<EOF

==> Done. Two one-time steps remain before OneDrive is mounted:

  1. Register your own Azure app (opens a browser sign-in):
       $CLONE_DIR/register_azure_app.sh

  2. Launch the app - from your applications menu, or:
       $CLONE_DIR/.venv/bin/python3 -m onedrive
     - and sign in with the device code it shows you.

After that, every future launch (including on login) signs in and mounts
~/OneDrive automatically - no further steps needed.
EOF
