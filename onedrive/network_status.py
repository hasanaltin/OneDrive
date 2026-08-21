import logging
import re
import subprocess

logger = logging.getLogger(__name__)

# NetworkManager's own NMMetered D-Bus enum: 0=unknown, 1=yes, 2=no,
# 3=guess-yes, 4=guess-no. Both real and "guessed" yes count as metered -
# NetworkManager's guess is usually right (it recognizes common cases like
# a phone's Wi-Fi hotspot) and erring toward "treat as metered" is the
# safer default for a feature whose whole point is avoiding data charges.
_METERED_VALUES = {1, 3}


def is_metered() -> bool:
    """Whether NetworkManager considers the current connection metered -
    queried fresh on every call (deliberately not cached; the active
    connection can change while the app keeps running, e.g. switching from
    home Wi-Fi to a phone hotspot). Fails closed to "not metered" on any
    error (no NetworkManager, gdbus missing, no system D-Bus) rather than
    silently blocking sync on a system this can't be determined for -
    verified directly against this machine's real NetworkManager."""
    try:
        result = subprocess.run(
            [
                "gdbus", "call", "--system",
                "--dest", "org.freedesktop.NetworkManager",
                "--object-path", "/org/freedesktop/NetworkManager",
                "--method", "org.freedesktop.DBus.Properties.Get",
                "org.freedesktop.NetworkManager", "Metered",
            ],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return False
        match = re.search(r"uint32 (\d+)", result.stdout)
        if not match:
            return False
        return int(match.group(1)) in _METERED_VALUES
    except (subprocess.SubprocessError, OSError):
        logger.debug("is_metered: query failed, assuming not metered", exc_info=True)
        return False


# NetworkManager's own NMConnectivityState D-Bus enum: 0=unknown, 1=none,
# 2=portal (captive portal, no real access yet), 3=limited, 4=full. Only
# FULL counts as online here - a captive portal or limited connection
# means real internet access isn't actually working yet, same practical
# effect as no connection at all for anything this app needs to do.
_ONLINE_VALUE = 4


def is_online() -> bool:
    """Whether NetworkManager currently reports full internet
    connectivity - queried fresh on every call, same reasoning as
    is_metered() about not caching. Fails OPEN to "online" on any error
    (no NetworkManager, gdbus missing, no system D-Bus) - the opposite of
    is_metered()'s fail-closed default, deliberately: a wrongly-gray tray
    icon claiming "offline" when this app actually has no way to tell is a
    more misleading failure mode than just letting the normal blue icon
    show and finding out for real the next time a sync attempt runs."""
    try:
        result = subprocess.run(
            [
                "gdbus", "call", "--system",
                "--dest", "org.freedesktop.NetworkManager",
                "--object-path", "/org/freedesktop/NetworkManager",
                "--method", "org.freedesktop.DBus.Properties.Get",
                "org.freedesktop.NetworkManager", "Connectivity",
            ],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return True
        match = re.search(r"uint32 (\d+)", result.stdout)
        if not match:
            return True
        return int(match.group(1)) == _ONLINE_VALUE
    except (subprocess.SubprocessError, OSError):
        logger.debug("is_online: query failed, assuming online", exc_info=True)
        return True
