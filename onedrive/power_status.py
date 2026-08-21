import logging
import re
import subprocess

logger = logging.getLogger(__name__)

_LOW_BATTERY_PERCENT = 20.0


def is_battery_saver() -> bool:
    """Whether the system's active power profile is "power-saver" (the
    freedesktop.org power-profiles-daemon standard, present on GNOME/KDE
    alike - not KDE-specific), matching the requested reference behavior
    ("Automatically pause sync when this device is in battery saver
    mode"). Queried fresh on every call, same reasoning as network_status
    functions about not caching - fails closed to "not battery saver" on
    any error (daemon not running, gdbus missing, no system D-Bus), the
    same safe default is_metered() uses."""
    try:
        result = subprocess.run(
            [
                "gdbus", "call", "--system",
                "--dest", "net.hadess.PowerProfiles",
                "--object-path", "/net/hadess/PowerProfiles",
                "--method", "org.freedesktop.DBus.Properties.Get",
                "net.hadess.PowerProfiles", "ActiveProfile",
            ],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return False
        return "power-saver" in result.stdout
    except (subprocess.SubprocessError, OSError):
        logger.debug("is_battery_saver: query failed, assuming not battery saver", exc_info=True)
        return False


def is_battery_low() -> bool:
    """Whether the system's aggregate battery charge (UPower's
    DisplayDevice - the same source the desktop's own low-battery
    notification is based on) is below _LOW_BATTERY_PERCENT.

    Reported directly as a real gap - sync kept running at 10%, then
    confirmed again at 7% - even with the battery-saver-mode check above
    already in place. UPower's own WarningLevel property looked like the
    more semantically precise thing to key off (it's meant to BE the
    desktop's own "is this low" judgment) but confirmed directly, live,
    that it stayed at NONE at 7% remaining on this machine - not reliable
    here, so this checks the raw percentage against an explicit threshold
    instead. Fails closed to "not low" on any error, same pattern as the
    other power/network checks."""
    try:
        result = subprocess.run(
            [
                "gdbus", "call", "--system",
                "--dest", "org.freedesktop.UPower",
                "--object-path", "/org/freedesktop/UPower/devices/DisplayDevice",
                "--method", "org.freedesktop.DBus.Properties.Get",
                "org.freedesktop.UPower.Device", "Percentage",
            ],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return False
        match = re.search(r"([\d.]+)", result.stdout)
        if not match:
            return False
        return float(match.group(1)) < _LOW_BATTERY_PERCENT
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.debug("is_battery_low: query failed, assuming not low", exc_info=True)
        return False
