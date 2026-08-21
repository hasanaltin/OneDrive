# OneDrive "Always keep on this device" / "Free up space" for Dolphin

Adds two right-click context menu actions for folders inside `~/OneDrive` (the on-demand mount) -
the same pin/unpin state the in-app "Choose folders" dialog already exposes as checkboxes, just
reachable directly from the file manager without opening this app's window first.

This is a native `KAbstractFileItemActionPlugin`, loaded directly into Dolphin's (and any other
KIO-using app's) own process. It has no access to the Python app's state on its own - every check
and change is forwarded over the same local Unix socket the overlay-icon plugin (`../dolphin-overlay/`)
already uses. See `onedrive/dolphin_overlay_server.py` for the other end of that connection.

**Why this plugin type, and not another `KIO::ThumbnailCreator` (see the reverted attempt in
CHANGELOG.md's `[0.4.91]`)**: `KFileItemActions` calls `actions()` on *every* enabled,
mimetype-matching plugin and additively combines all their results into the context menu
(confirmed directly against KDE's own `kio` source, `src/widgets/kfileitemactions.cpp`) - unlike
thumbnail plugins, there's no "only one plugin can win" resolution here to lose. Returning an
empty action list for a selection that isn't ours just means no OneDrive menu items appear; it
never suppresses another plugin's items or triggers Dolphin to fall back on something less safe.

## Build and install

Needs KDE Frameworks 6 development packages (same ones the other `packaging/dolphin-*` plugins need):

```bash
sudo apt install cmake extra-cmake-modules libkf6kio-dev qt6-base-dev
```

Then:

```bash
cd packaging/dolphin-pin-action
cmake -B build -DCMAKE_INSTALL_PREFIX=/usr
cmake --build build
sudo cmake --install build
kbuildsycoca6 --noincremental
```

Restart Dolphin (`pkill dolphin` / close all windows, then reopen) for it to pick up the new
plugin. Right-click a folder under `~/OneDrive` - "Always keep on this device" and "Free up space
(OneDrive)" should appear alongside the usual Cut/Copy/Rename entries.

## Uninstall

```bash
sudo rm /usr/lib/*/qt6/plugins/kf6/kfileitemaction/onedrivenativepinaction.so
kbuildsycoca6 --noincremental
```
