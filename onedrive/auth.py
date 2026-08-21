import logging
import os

import msal

from onedrive import constants

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = constants.APP_NAME
_KEYRING_ACCOUNT = "msal-token-cache"


class TokenCacheStore:
    """Persists the MSAL token cache via the OS keyring, falling back to a
    permission-locked file if no keyring backend is available."""

    def load(self) -> msal.SerializableTokenCache:
        cache = msal.SerializableTokenCache()
        data = self._load_raw()
        if data:
            cache.deserialize(data)
        return cache

    def save(self, cache: msal.SerializableTokenCache) -> None:
        if not cache.has_state_changed:
            return
        self._save_raw(cache.serialize())

    def clear(self) -> None:
        try:
            import keyring
            import keyring.errors

            try:
                keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
            except keyring.errors.PasswordDeleteError:
                pass
        except Exception:
            logger.debug("keyring clear failed, ignoring", exc_info=True)
        if constants.TOKEN_CACHE_FILE.exists():
            constants.TOKEN_CACHE_FILE.unlink()

    def _load_raw(self) -> str | None:
        try:
            import keyring

            data = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
            if data is not None:
                return data
        except Exception:
            logger.debug("keyring unavailable for load, using file fallback", exc_info=True)
        return self._load_file()

    def _save_raw(self, data: str) -> None:
        try:
            import keyring

            keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, data)
        except Exception:
            logger.debug("keyring unavailable for save, using file fallback only", exc_info=True)
        # Written unconditionally, even when keyring succeeds - not just as
        # an else-branch fallback. Confirmed via a real autostart run: the
        # keyring/Secret Service D-Bus backend isn't always up yet at the
        # exact moment autostart fires (the same log showed a portal D-Bus
        # registration failure at that instant too), so a *future* boot can
        # hit that same window and get nothing back from
        # keyring.get_password() even though a token genuinely exists -
        # _load_raw() already falls back to this file when keyring returns
        # empty, but only if the file was actually kept up to date, which it
        # wasn't (this method used to return immediately after a successful
        # keyring save, leaving the file stale/never-written for an account
        # that had only ever saved successfully via keyring).
        self._save_file(data)

    def _load_file(self) -> str | None:
        if not constants.TOKEN_CACHE_FILE.exists():
            return None
        return constants.TOKEN_CACHE_FILE.read_text()

    def _save_file(self, data: str) -> None:
        constants.ensure_dirs()
        fd = os.open(
            str(constants.TOKEN_CACHE_FILE), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600
        )
        try:
            os.write(fd, data.encode("utf-8"))
        finally:
            os.close(fd)


class AuthManager:
    def __init__(self, proxies: dict[str, str] | None = None) -> None:
        self._store = TokenCacheStore()
        self._cache = self._store.load()
        self.app: msal.PublicClientApplication | None = None
        self._proxies = proxies
        # msal.PublicClientApplication.__init__() always performs a live
        # network call (tenant discovery against login.microsoftonline.com) -
        # confirmed via MSAL's own source: "We always do a tenant discovery."
        # (authority.py), no parameter skips it. AuthManager() is built
        # inside MainWindow.__init__(), before any window is even shown, so
        # letting that exception propagate used to take the entire app down
        # with it whenever there's no network yet - routine right after
        # login/boot, since autostart fires before NetworkManager finishes
        # connecting (confirmed via a real crash: NameResolutionError from
        # this exact constructor at boot, app never got as far as showing a
        # window, let alone mounting anything). Swallowed here; _ensure_app()
        # retries lazily on every actual use instead of only once.
        try:
            self._ensure_app()
        except Exception:
            logger.warning("MSAL client init failed at startup, will retry on next use", exc_info=True)

    def _ensure_app(self) -> None:
        """(Re)constructs the MSAL client if it isn't already. Deliberately
        does NOT catch exceptions here - callers that can tolerate silent
        failure (is_signed_in) don't call this at all; callers where a
        network error must surface as a network error, not get silently
        swallowed into "not signed in", let it propagate."""
        if self.app is not None:
            return
        if not constants.CLIENT_ID:
            # No shared Client ID ships with this project (see
            # constants.py's Auth section for why) - every deployment must
            # register its own Azure AD app first. Without this check, MSAL
            # itself fails on a None/empty client_id with a much less
            # actionable error.
            raise RuntimeError(
                "No Azure app Client ID configured. Run ./register_azure_app.sh first "
                f"(or set the {constants.CLIENT_ID_ENV_VAR} environment variable) - see README.md."
            )
        self.app = msal.PublicClientApplication(
            constants.CLIENT_ID,
            authority=constants.AUTHORITY,
            token_cache=self._cache,
            proxies=self._proxies,
        )

    def set_proxies(self, proxies: dict[str, str] | None) -> None:
        """Called when the Settings panel's proxy configuration changes.
        MSAL's PublicClientApplication only takes proxies at construction
        time (no live-update method of its own) - dropping the existing
        client here makes _ensure_app() rebuild it with the new settings
        the next time it's actually needed, rather than requiring the
        whole app to restart for a proxy change to reach the sign-in/
        token-refresh calls too (GraphClient's own session picks up a
        proxy change immediately via apply_proxy(), independent of this)."""
        self._proxies = proxies
        self.app = None

    def _cached_accounts(self) -> list[dict]:
        # Reads the local token cache directly, independent of whether the
        # MSAL client object itself could be constructed - "am I signed in"
        # is a question about locally-saved state and must be answerable
        # with zero network access, unlike acquire_token_silent() etc.
        return list(self._cache.search(msal.TokenCache.CredentialType.ACCOUNT))

    @property
    def is_signed_in(self) -> bool:
        return bool(self._cached_accounts())

    @property
    def account_username(self) -> str | None:
        accounts = self._cached_accounts()
        return accounts[0]["username"] if accounts else None

    def get_token(self) -> str | None:
        if not self._cached_accounts():
            return None
        # Let a construction failure here (e.g. no network) propagate as a
        # plain exception rather than becoming a silent None - the caller
        # (GraphClient._headers) turns a None return into GraphAuthError,
        # which every sync worker treats as "needs re-authentication" and
        # pops a re-consent dialog. That's correct for a genuinely revoked/
        # expired session, but wrong for "network's just down right now" -
        # this way that case surfaces as an ordinary retryable error instead.
        self._ensure_app()
        accounts = self.app.get_accounts()
        if not accounts:
            return None
        result = self.app.acquire_token_silent(constants.SCOPES, account=accounts[0])
        self._store.save(self._cache)
        if not result or "access_token" not in result:
            return None
        return result["access_token"]

    def start_device_flow(self) -> dict:
        self._ensure_app()
        flow = self.app.initiate_device_flow(scopes=constants.SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Failed to start device flow: {flow}")
        return flow

    def complete_device_flow(self, flow: dict) -> dict:
        """Blocks until the user completes sign-in (or the flow expires).
        Must be called off the GUI thread."""
        self._ensure_app()
        result = self.app.acquire_token_by_device_flow(flow)
        self._store.save(self._cache)
        if "access_token" not in result:
            raise RuntimeError(
                f"Sign-in failed: {result.get('error_description', result)}"
            )
        return result

    def sign_out(self) -> None:
        self._ensure_app()
        for account in self.app.get_accounts():
            self.app.remove_account(account)
        self._store.save(self._cache)
        self._store.clear()
