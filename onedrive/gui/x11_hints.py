"""Direct EWMH _NET_WM_STATE_SKIP_TASKBAR control via raw X11 calls.

Exists because Qt's own window-type-hint machinery (Qt.WindowType.Tool,
Qt.WindowType.Dialog, with or without FramelessWindowHint) turned out not
to be enough on its own here: confirmed directly, live, that a Tool window
with native decorations still gets _NET_WM_WINDOW_TYPE set to BOTH
_UTILITY and a _NORMAL fallback, and this desktop's Task Manager widget
respects the _NORMAL fallback and shows a taskbar entry anyway - even with
Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint (the same
combination the tray popup itself uses without issue). _NET_WM_STATE_SKIP_TASKBAR
is the actual purpose-built EWMH mechanism for exactly this, independent
of window TYPE - confirmed directly, live, with 3 side-by-side test
windows against the real taskbar, this is the one that actually works.

This is standard EWMH (freedesktop.org), not a KDE/KWin-specific
mechanism - any EWMH-compliant window manager (GNOME/Mutter, XFCE, i3,
Sway via XWayland, etc.) is expected to honor it, consistent with this
project's requirement to work across distros/desktop environments, not
just KDE. Only meaningful under X11/XWayland (which this app already
requires for window positioning in general - see __main__.py); silently
does nothing if Xlib is unavailable or the call fails for any reason,
since this is a cosmetic enhancement, not something worth crashing over.
"""

import logging

logger = logging.getLogger(__name__)


def skip_taskbar(win_id: int) -> None:
    try:
        from Xlib import X, Xatom, display
        from Xlib.protocol import event
    except ImportError:
        logger.debug("python-xlib not available - skipping taskbar hint")
        return

    try:
        d = display.Display()
        window = d.create_resource_object("window", win_id)
        root = d.screen().root

        state_atom = d.intern_atom("_NET_WM_STATE")
        skip_taskbar_atom = d.intern_atom("_NET_WM_STATE_SKIP_TASKBAR")
        skip_pager_atom = d.intern_atom("_NET_WM_STATE_SKIP_PAGER")

        # Direct property set - reliably picked up by the WM on the
        # window's initial map (i.e. call this before the first show()).
        window.change_property(state_atom, Xatom.ATOM, 32, [skip_taskbar_atom, skip_pager_atom])

        # Also send the ClientMessage form of the same request (the
        # documented EWMH protocol for an ALREADY-mapped window) - a
        # harmless no-op if the direct property set above already
        # covered it, but needed if this is called again after show().
        data = (32, [1, skip_taskbar_atom, skip_pager_atom, 1, 0])  # 1 = _NET_WM_STATE_ADD
        root.send_event(
            event.ClientMessage(window=window, client_type=state_atom, data=data),
            event_mask=(X.SubstructureRedirectMask | X.SubstructureNotifyMask),
        )
        d.flush()
    except Exception:
        logger.debug("failed to set _NET_WM_STATE_SKIP_TASKBAR", exc_info=True)
