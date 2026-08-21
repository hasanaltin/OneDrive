import logging
from urllib.parse import quote

from onedrive import constants
from onedrive.db import Database

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = constants.APP_NAME
_KEYRING_ACCOUNT_PROXY_PASSWORD = "proxy-password"


def get_proxy_password() -> str:
    """Mirrors auth.py's TokenCacheStore keyring usage - a proxy password
    is a real credential, so it doesn't belong in the plain sync_state
    table alongside everything else (host/port/username are fine there,
    they're not secrets on their own)."""
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT_PROXY_PASSWORD) or ""
    except Exception:
        logger.debug("keyring unavailable for proxy password load", exc_info=True)
        return ""


def set_proxy_password(password: str) -> None:
    try:
        import keyring
        import keyring.errors

        if password:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT_PROXY_PASSWORD, password)
        else:
            try:
                keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT_PROXY_PASSWORD)
            except keyring.errors.PasswordDeleteError:
                pass
    except Exception:
        logger.debug("keyring unavailable for proxy password save", exc_info=True)


def get_proxy_settings(db: Database) -> tuple[dict[str, str] | None, bool]:
    """Returns (proxies, trust_env) in the exact shape both
    requests.Session and msal.PublicClientApplication expect for their
    own `proxies` parameter.

    proxy_mode (sync_state) is one of:
      "system" (default) - don't set anything explicit; requests/MSAL
        fall back to the standard HTTP_PROXY/HTTPS_PROXY environment
        variables on their own. Reading a specific desktop's own proxy
        config (GNOME's gsettings, KDE's kioslaverc, ...) isn't portable
        the way this project needs to stay (see the distro-agnostic
        requirement) - the environment variables are the one thing every
        desktop can be told to set consistently.
      "none" - explicitly ignore even those environment variables
        (trust_env=False), for a network that genuinely has none.
      "manual" - this app's own configured host/port/credentials,
        independent of the environment either way.
    """
    mode = db.get_sync_state("proxy_mode") or "system"
    if mode == "none":
        return None, False
    if mode == "manual":
        host = (db.get_sync_state("proxy_host") or "").strip()
        if not host:
            return None, True
        port = (db.get_sync_state("proxy_port") or "").strip()
        auth_enabled = db.get_sync_state("proxy_auth_enabled") == "1"
        username = (db.get_sync_state("proxy_username") or "").strip()
        auth_part = ""
        if auth_enabled and username:
            password = get_proxy_password()
            auth_part = f"{quote(username)}:{quote(password)}@"
        netloc = f"{host}:{port}" if port else host
        url = f"http://{auth_part}{netloc}"
        return {"http": url, "https": url}, True
    return None, True
