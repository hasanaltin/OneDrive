# OneDrive sync-status overlay icons for Dolphin

Draws a small green/blue badge on files and folders inside `~/OneDrive` (and
any Folder Pair's local folder) showing whether OneDrive for Linux Client
has the file locally, still needs to download it, or is downloading it
right now - the same idea Nextcloud's own desktop client uses.

This is a native KIO plugin (`KOverlayIconPlugin`), loaded directly into
Dolphin's own process. It has no access to the Python app's state on its
own - every request it gets from Dolphin ("what's the status of this
path?") is forwarded over a local Unix socket to the running app, which
answers from its own sync database. See `onedrive/dolphin_overlay_server.py`
for the other end of that connection.

## Build and install

Needs KDE Frameworks 6 development packages:

```bash
sudo apt install cmake extra-cmake-modules libkf6kio-dev qt6-base-dev
```

Then:

```bash
cd packaging/dolphin-overlay
cmake -B build -DCMAKE_INSTALL_PREFIX=/usr
cmake --build build
sudo cmake --install build
kbuildsycoca6 --noincremental
```

Restart Dolphin (`kquitapp6 dolphin` / close all windows, then reopen) for
it to pick up the new plugin.

## Uninstall

```bash
sudo rm /usr/lib/*/qt6/plugins/kf6/overlayicon/onedrivenativeoverlay.so
kbuildsycoca6 --noincremental
```
