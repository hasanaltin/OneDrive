import logging
import os
import time
from pathlib import Path
from typing import Callable, Iterator

import requests

from onedrive import constants
from onedrive.auth import AuthManager
from onedrive.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# requests has NO default timeout - without one, any call here can hang
# indefinitely rather than fail. This matters most for a connection that was
# already established while online and then goes dead mid-request (packets
# silently dropped, not an immediate DNS/refused-connection error) - that's
# slower to detect than "network's fully down from the start", and is
# consistent with a real reproduction: creating a file through the mount
# while offline left Dolphin's transfer dialog stuck indefinitely rather
# than failing. (connect_timeout, read_timeout) - requests applies the read
# timeout to gaps between bytes on a streamed response, not the whole
# transfer, so this doesn't prematurely kill a large-but-progressing
# download/upload, only one that's truly stalled.
_DEFAULT_TIMEOUT = (10, 30)
# A 60s read/write timeout on a ~10MB chunk needs ~175KB/s sustained
# throughput just to avoid timing out on an otherwise-healthy but slow
# upload connection - confirmed directly against a real one, where every
# single chunk of a large file timed out this way. 180s lowers that floor
# to ~58KB/s, generous enough for a genuinely slow link while still
# bounded (a truly dead connection still times out, just not this fast).
_CHUNK_UPLOAD_TIMEOUT = (10, 180)


class GraphAuthError(Exception):
    """Raised when a request can't get a valid access token (not signed in,
    or silent refresh failed and interactive sign-in is required again)."""


class GraphConflictError(Exception):
    """Raised on 409/412 - the remote item changed since we last read its
    etag, or a name collision occurred. Callers must NOT blindly retry this;
    it has to be routed back into reconciliation, not the generic
    429/503/504 retry path, since repeating the same request won't help."""

    def __init__(self, status_code: int, response: requests.Response):
        super().__init__(f"Graph conflict: HTTP {status_code}")
        self.status_code = status_code
        self.response = response


def _is_name_already_exists(exc: GraphConflictError) -> bool:
    """Whether a GraphConflictError is specifically Graph's "nameAlreadyExists"
    error code (a real create-vs-existing-item collision) rather than some
    other 409/412 (e.g. an etag mismatch on a replace)."""
    try:
        return exc.response.json().get("error", {}).get("code") == "nameAlreadyExists"
    except ValueError:
        return False


def _next_expected_start(status: dict, size: int) -> int:
    """Parses an upload session status response's nextExpectedRanges to
    find the byte offset to resume from. An empty list technically means
    "nothing left" per Graph's docs, but a session that's truly fully
    received auto-completes and stops existing (a follow-up GET on it
    404s, raising before this is ever called) - reaching here with an
    empty list in practice means the status response itself doesn't know
    yet, so treat it as "no progress confirmed" rather than "done" to
    avoid ever fabricating a fake completion."""
    ranges = status.get("nextExpectedRanges") or []
    if not ranges:
        return 0
    return int(ranges[0].split("-")[0])


class GraphClient:
    def __init__(
        self,
        auth: AuthManager,
        session: requests.Session | None = None,
        upload_limit_kbps_getter: Callable[[], float | None] | None = None,
        download_limit_kbps_getter: Callable[[], float | None] | None = None,
        proxies: dict[str, str] | None = None,
        trust_env: bool = True,
    ):
        self.auth = auth
        self.session = session or requests.Session()
        self.apply_proxy(proxies, trust_env)
        self.last_delta_link: str | None = None
        # Getter callables (not a plain stored value) so a limit changed in
        # Settings takes effect on the very next transfer - each upload/
        # download call reads the current setting fresh rather than this
        # client needing to be reconstructed or notified some other way.
        self._upload_limit_kbps_getter = upload_limit_kbps_getter
        self._download_limit_kbps_getter = download_limit_kbps_getter

    def apply_proxy(self, proxies: dict[str, str] | None, trust_env: bool) -> None:
        """Applies proxy settings to the shared session - every Graph call
        in this class goes through self.session (either _get()/_write() or
        one of the few direct self.session.* calls in the chunked-upload
        path), so setting these two attributes here covers all of them at
        once. Called from __init__ and again whenever the Settings panel's
        proxy configuration changes, so a change takes effect on the very
        next request without needing this client reconstructed."""
        self.session.proxies = proxies or {}
        self.session.trust_env = trust_env

    def _upload_limiter(self) -> RateLimiter:
        kbps = self._upload_limit_kbps_getter() if self._upload_limit_kbps_getter else None
        return RateLimiter(kbps * 1024 if kbps else None)

    def _download_limiter(self) -> RateLimiter:
        kbps = self._download_limit_kbps_getter() if self._download_limit_kbps_getter else None
        return RateLimiter(kbps * 1024 if kbps else None)

    def _headers(self) -> dict:
        token = self.auth.get_token()
        if not token:
            raise GraphAuthError("Not signed in or token refresh failed")
        return {"Authorization": f"Bearer {token}"}

    def _get(self, url: str, *, _retries: int = 5, **kwargs) -> requests.Response:
        custom_headers = kwargs.pop("headers", None) or {}
        headers = {**self._headers(), **custom_headers}
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        resp = self.session.get(url, headers=headers, **kwargs)
        if resp.status_code in (429, 503, 504) and _retries > 0:
            retry_after = int(resp.headers.get("Retry-After", 5))
            logger.warning(
                "Graph request throttled/unavailable (status=%s), retrying in %ss",
                resp.status_code,
                retry_after,
            )
            time.sleep(retry_after)
            return self._get(url, headers=custom_headers, _retries=_retries - 1, **kwargs)
        resp.raise_for_status()
        return resp

    def _write(self, method: str, url: str, *, _retries: int = 5, **kwargs) -> requests.Response:
        custom_headers = kwargs.pop("headers", None) or {}
        headers = {**self._headers(), **custom_headers}
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        resp = self.session.request(method, url, headers=headers, **kwargs)
        if resp.status_code in (409, 412):
            raise GraphConflictError(resp.status_code, resp)
        if resp.status_code in (429, 503, 504) and _retries > 0:
            retry_after = int(resp.headers.get("Retry-After", 5))
            logger.warning(
                "Graph write throttled/unavailable (status=%s), retrying in %ss",
                resp.status_code,
                retry_after,
            )
            time.sleep(retry_after)
            return self._write(method, url, headers=custom_headers, _retries=_retries - 1, **kwargs)
        resp.raise_for_status()
        return resp

    def get_drive(self) -> dict:
        return self._get(f"{constants.GRAPH_BASE_URL}/me/drive").json()

    def delta(self, delta_link: str | None) -> Iterator[list[dict]]:
        """Yields pages of changed/new items. After exhaustion,
        self.last_delta_link holds the new deltaLink to persist."""
        url = delta_link or f"{constants.GRAPH_BASE_URL}/me/drive/root/delta"
        while True:
            page = self._get(url).json()
            yield page.get("value", [])
            next_link = page.get("@odata.nextLink")
            if next_link:
                url = next_link
                continue
            self.last_delta_link = page.get("@odata.deltaLink")
            return

    def download_content(self, drive_id: str, item_id: str, dest_path: Path) -> None:
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/content"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_name(dest_path.name + ".part")
        limiter = self._download_limiter()
        # NOTE: Graph redirects /content to a separate blob-storage host.
        # `requests` correctly strips the Authorization header on cross-host
        # redirects automatically - do not "fix" this by re-adding it.
        with self._get(url, stream=True) as resp:
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
                        limiter.throttle(len(chunk))
        os.replace(tmp_path, dest_path)  # atomic on same filesystem

    def get_item(self, drive_id: str, item_id: str) -> dict:
        return self._get(f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}").json()

    def get_item_by_path(self, drive_id: str, parent_id: str, name: str) -> dict | None:
        """Looks up a child item by its parent's id + name - same colon
        addressing upload_small's own URL already uses
        (.../items/{parent_id}:/{name}), just as a plain GET instead of a
        content PUT. Returns None on 404 (genuinely doesn't exist) rather
        than raising, since callers use this specifically to find out
        whether something's there."""
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{parent_id}:/{name}"
        try:
            return self._get(url).json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    def get_display_name(self) -> str | None:
        """The signed-in user's real Graph display name (e.g. "Hasan Altin")
        - distinct from AuthManager.account_username, which is only the
        UPN/email MSAL caches locally from the token, not a human-friendly
        name. Requested directly ("My name should be seen as it is seen in
        microsoft graph from Display Name")."""
        data = self._get(f"{constants.GRAPH_BASE_URL}/me?$select=displayName").json()
        return data.get("displayName")

    def get_tenant_name(self) -> str | None:
        """The signed-in account's tenant/organization display name (e.g.
        "Contoso Ltd") - requested directly, to show the real company
        name under the display name in the tray popup rather than just the
        email domain. /me's own companyName field looked like the obvious
        source but turned out empty here - confirmed directly, it's an
        optional Azure AD field this tenant doesn't populate.
        /organization's displayName is the actual tenant name and worked
        with the scopes this app already has (Files.ReadWrite/User.Read) -
        no Organization.Read.All/Directory.Read.All needed, confirmed
        directly against the real tenant rather than assumed."""
        data = self._get(f"{constants.GRAPH_BASE_URL}/organization?$select=displayName").json()
        orgs = data.get("value") or []
        return orgs[0].get("displayName") if orgs else None

    def get_profile_photo(self) -> bytes | None:
        """The signed-in user's profile photo (raw JPEG bytes), or None if
        they don't have one set - a plain 404 there, not an error worth
        logging or retrying like a real failure."""
        url = f"{constants.GRAPH_BASE_URL}/me/photo/$value"
        try:
            return self._get(url).content
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    def create_folder(self, drive_id: str, parent_id: str, name: str) -> dict:
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{parent_id}/children"
        body = {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
        return self._write("POST", url, json=body).json()

    def upload_small(self, drive_id: str, parent_id: str, name: str, data: bytes) -> dict:
        """Creates a new file - no known remote id yet. No If-Match: undocumented
        on this endpoint, and there's nothing to match against for a brand-new item."""
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{parent_id}:/{name}:/content"
        limiter = self._upload_limiter()
        resp = self._write(
            "PUT", url, data=data, headers={"Content-Type": "application/octet-stream"}
        )
        # Not chunked (single PUT, capped at SIMPLE_UPLOAD_MAX_BYTES ~4MiB) -
        # throttling the whole payload in one shot still works with the same
        # RateLimiter: the limiter's clock started before the request, so a
        # transfer that was already slower than the target rate (real
        # network limits) correctly gets no extra sleep on top.
        limiter.throttle(len(data))
        return resp.json()

    def replace_small(self, drive_id: str, item_id: str, data: bytes) -> dict:
        """Replaces a known item's content (size <= SIMPLE_UPLOAD_MAX_BYTES).
        Caller MUST have already compared items.etag to the pair's
        last_synced_etag before calling this - If-Match isn't documented as
        supported here, so this method does no conflict check of its own."""
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/content"
        limiter = self._upload_limiter()
        resp = self._write(
            "PUT", url, data=data, headers={"Content-Type": "application/octet-stream"}
        )
        limiter.throttle(len(data))
        return resp.json()

    def create_upload_session(
        self,
        drive_id: str,
        parent_id_or_item_id: str,
        name: str,
        *,
        is_replace: bool,
        if_match: str | None = None,
    ) -> dict:
        if is_replace:
            url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{parent_id_or_item_id}/createUploadSession"
        else:
            url = (
                f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/"
                f"{parent_id_or_item_id}:/{name}:/createUploadSession"
            )
        body = {
            "item": {
                "@microsoft.graph.conflictBehavior": "replace" if is_replace else "fail",
                "name": name,
            }
        }
        headers = {}
        if is_replace and if_match:
            headers["If-Match"] = if_match
        return self._write("POST", url, json=body, headers=headers).json()

    def upload_large(self, upload_url: str, local_path: Path, size: int) -> dict:
        """Chunked upload against an existing session's uploadUrl. Strictly
        sequential - Graph rejects out-of-order chunks. Deliberately does NOT
        send Authorization on these PUTs (Graph docs: including it here can
        return 401 - the upload_url itself is the credential).

        On a connection failure mid-chunk, asks Graph's own upload-session
        status for nextExpectedRanges and resumes from there, rather than
        assuming either "that chunk made it" or "nothing did" - the PUT's
        response can be lost without telling us which actually happened.
        Confirmed as a real bug, not just theoretical: the previous version
        treated ANY 200 response as a completed upload, including the
        plain status-check response the old recovery path itself returned
        on success (a "here's what I still need" reply, not a finished
        item) - every chunk timeout produced a fake "success" with no
        'id' field, crashing downstream in db.upsert_item() with
        KeyError('id')."""
        chunk_size = constants.UPLOAD_CHUNK_SIZE
        # One limiter for the whole upload, not one per chunk - a fresh
        # RateLimiter per chunk would reset its clock every time and never
        # actually cap the aggregate rate across the transfer.
        limiter = self._upload_limiter()
        # A chronically bad connection (every chunk timing out, Graph never
        # confirming forward progress) would otherwise spin this loop
        # indefinitely within a single call, hammering the connection every
        # ~10s forever with no backoff and no chance for the outer retry
        # layer (the pair-sync pass, 60s apart) or the session-cleanup this
        # method's caller now does on failure to ever run. Give up after a
        # bounded number of consecutive failures instead - the caller's own
        # retry (with a fresh session) picks it up next pass.
        consecutive_failures = 0
        _MAX_CONSECUTIVE_FAILURES = 8
        with open(local_path, "rb") as f:
            start = 0
            while start < size:
                end = min(start + chunk_size, size) - 1
                f.seek(start)
                chunk = f.read(end - start + 1)
                headers = {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                }
                try:
                    resp = self._upload_chunk_with_retry(upload_url, chunk, headers)
                except requests.RequestException:
                    consecutive_failures += 1
                    if consecutive_failures > _MAX_CONSECUTIVE_FAILURES:
                        raise
                    logger.warning("chunk PUT failed, checking session status to resume", exc_info=True)
                    status = self._resume_upload_session(upload_url)
                    new_start = _next_expected_start(status, size)
                    if new_start <= start:
                        # Graph doesn't consider anything past where we
                        # already were to be confirmed received - nothing
                        # to do differently next loop but retry the exact
                        # same range again.
                        continue
                    start = new_start
                    continue
                consecutive_failures = 0
                limiter.throttle(len(chunk))
                if resp.status_code in (200, 201):
                    return resp.json()
                # 202 Accepted -> continue with next chunk
                start = end + 1
        raise RuntimeError("upload_large finished all chunks without a final item response")

    def _upload_chunk_with_retry(self, upload_url: str, chunk: bytes, headers: dict, _retries: int = 5):
        resp = self.session.put(upload_url, data=chunk, headers=headers, timeout=_CHUNK_UPLOAD_TIMEOUT)
        if resp.status_code in (429, 503, 504) and _retries > 0:
            retry_after = int(resp.headers.get("Retry-After", 5))
            time.sleep(retry_after)
            return self._upload_chunk_with_retry(upload_url, chunk, headers, _retries - 1)
        resp.raise_for_status()
        return resp

    def _resume_upload_session(self, upload_url: str) -> dict:
        """GETs the upload session's own status after a connection failure,
        to find out how many bytes Graph actually confirms receiving
        (nextExpectedRanges) - NOT a completed-item response, despite also
        returning 200 on success. Returns the parsed status dict (not a
        raw Response) specifically so callers can't make the mistake the
        old code did: treating "got a 200" as "the upload is done," when a
        200 here means "here's what I'm still waiting for." A 404 means
        the session expired - the caller must restart the whole upload
        from scratch."""
        resp = self.session.get(upload_url, timeout=_DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def cancel_upload_session(self, upload_url: str) -> None:
        self.session.delete(upload_url, timeout=_DEFAULT_TIMEOUT)

    def upload_file(
        self,
        drive_id: str,
        parent_id: str,
        name: str,
        local_path: Path,
        *,
        existing_item_id: str | None = None,
        if_match: str | None = None,
    ) -> dict:
        """Single call site the reconciler uses regardless of file size -
        picks simple PUT vs. upload-session based on SIMPLE_UPLOAD_MAX_BYTES.

        Self-heals a specific, confirmed-real failure mode: creating a file
        (no existing_item_id - we believe it's brand new) can 409 with
        "nameAlreadyExists" even though nothing in this app's own cache
        knows the item exists yet - verified directly against a real
        stuck file (a 394MB upload that retried every sync pass for
        hours, every single attempt failing in ~350ms - too fast to be a
        real chunk transfer, confirming it was the very first
        createUploadSession call itself getting rejected). A plain
        children listing didn't show any such file, but Graph's own error
        was unambiguous: an item with that name already exists at that
        parent. Without handling this, the pair worker's generic
        GraphConflictError handler (which just refreshes item metadata -
        a no-op when there's no known item to refresh) retries the exact
        same doomed create forever. One lookup-and-retry-as-replace here
        breaks that loop instead of reproducing it indefinitely."""
        size = local_path.stat().st_size
        try:
            return self._upload_file_once(drive_id, parent_id, name, local_path, size, existing_item_id, if_match)
        except GraphConflictError as exc:
            if existing_item_id is not None or not _is_name_already_exists(exc):
                raise
            logger.warning(
                "upload_file: create for %r conflicted with an item Graph already has that this "
                "app's own cache didn't know about - looking it up and retrying as a replace",
                name,
            )
            # The 409 itself confirms the item exists server-side, but this
            # lookup can still race a moment behind Graph's own read-after-
            # write consistency and come back empty - retried a couple of
            # times with backoff before giving up, rather than surfacing a
            # conflict for something we were just told is really there.
            discovered = None
            for delay in (0.0, 0.5, 1.5):
                if delay:
                    time.sleep(delay)
                discovered = self.get_item_by_path(drive_id, parent_id, name)
                if discovered is not None:
                    break
            if discovered is None:
                raise
            return self._upload_file_once(
                drive_id, parent_id, name, local_path, size, discovered["id"], discovered.get("eTag")
            )

    def _upload_file_once(
        self,
        drive_id: str,
        parent_id: str,
        name: str,
        local_path: Path,
        size: int,
        existing_item_id: str | None,
        if_match: str | None,
    ) -> dict:
        if size <= constants.SIMPLE_UPLOAD_MAX_BYTES:
            data = local_path.read_bytes()
            if existing_item_id:
                return self.replace_small(drive_id, existing_item_id, data)
            return self.upload_small(drive_id, parent_id, name, data)

        session = self.create_upload_session(
            drive_id,
            existing_item_id or parent_id,
            name,
            is_replace=existing_item_id is not None,
            if_match=if_match,
        )
        try:
            return self.upload_large(session["uploadUrl"], local_path, size)
        except Exception:
            # Confirmed directly as a real, recurring cause of trouble: a
            # session this app itself created but never finished was left
            # dangling, and Graph rejects creating a NEW session for the
            # same target while an old, uncanceled one is still considered
            # active - every retry after the first failure was generating
            # its own fresh 409 on createUploadSession itself, not just on
            # chunk PUTs, compounding with every attempt. Whatever went
            # wrong here, clean up after ourselves so the next retry - the
            # outer pair-sync pass, or the nameAlreadyExists self-heal
            # above - starts from a clean slate instead of piling up yet
            # another orphaned session on top.
            try:
                self.cancel_upload_session(session["uploadUrl"])
            except Exception:
                logger.debug("failed to cancel abandoned upload session", exc_info=True)
            raise

    def delete_item(self, drive_id: str, item_id: str, if_match: str | None = None) -> None:
        """Graph moves items to the OneDrive recycle bin rather than hard
        deleting - an extra safety net beyond this app's own conflict-copy
        mechanism."""
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}"
        headers = {"If-Match": if_match} if if_match else {}
        self._write("DELETE", url, headers=headers)

    def move_or_rename(
        self,
        drive_id: str,
        item_id: str,
        *,
        new_parent_id: str | None = None,
        new_name: str | None = None,
        if_match: str | None = None,
    ) -> dict:
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}"
        body: dict = {}
        if new_name is not None:
            body["name"] = new_name
        if new_parent_id is not None:
            body["parentReference"] = {"id": new_parent_id}
        headers = {"If-Match": if_match} if if_match else {}
        return self._write("PATCH", url, json=body, headers=headers).json()

    def create_share_link(self, drive_id: str, item_id: str) -> str:
        """Creates (or reuses an existing identical) sharing link and
        returns its webUrl. Deliberately view-only ("type": "view", not
        "edit") and scoped to "organization" rather than "anonymous" -
        this app is used against work/school tenants (see the device-code
        sign-in flow), and many such tenants disable anonymous/public
        sharing entirely via policy, which would make an anonymous-scoped
        request here just fail outright. "organization" (anyone signed
        into the same tenant can view) is both the safer default for a
        business account and the one far more likely to actually succeed."""
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/createLink"
        body = {"type": "view", "scope": "organization"}
        result = self._write("POST", url, json=body).json()
        return result["link"]["webUrl"]

    def search_people(self, query: str) -> list[dict]:
        """Fuzzy-searches the signed-in user's relevant people (colleagues,
        recent collaborators) by name/alias via the People API - used for
        the share dialog's live recipient picker. Each result is
        {"name": ..., "email": ...}; entries with no usable email (a
        distribution list with no direct address, an external contact with
        only an IM address, etc.) are dropped rather than passed through,
        since the caller only ever needs an address to invite."""
        url = f"{constants.GRAPH_BASE_URL}/me/people"
        params = {
            "$search": query,
            "$select": "displayName,scoredEmailAddresses,userPrincipalName",
            "$top": "10",
        }
        data = self._get(url, params=params).json()
        results = []
        for person in data.get("value", []):
            emails = person.get("scoredEmailAddresses") or []
            email = emails[0]["address"] if emails else person.get("userPrincipalName") or ""
            if not email:
                continue
            results.append({"name": person.get("displayName") or email, "email": email})
        return results

    def invite(
        self,
        drive_id: str,
        item_id: str,
        emails: list[str],
        role: str,
        message: str = "",
    ) -> None:
        """Sends a real sharing invitation (as opposed to create_share_link's
        anyone-with-the-link approach) to specific people, granting them
        "read" or "write" access and emailing them a notification -
        mirrors exactly what the OneDrive/SharePoint web UI's own "Share"
        dialog does when you type a name and hit Send."""
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/invite"
        body = {
            "recipients": [{"email": email} for email in emails],
            "message": message,
            "requireSignIn": True,
            "sendInvitation": True,
            "roles": [role],
        }
        self._write("POST", url, json=body)

    def list_permissions(self, drive_id: str, item_id: str) -> list[dict]:
        """Returns the raw permission objects Graph reports for this item -
        both named-person grants (from invite()) and sharing links (from
        create_share_link()) show up here, which is what backs the "Manage
        Access" dialog. Deliberately returns the raw dicts rather than a
        parsed dataclass: the shape varies meaningfully by permission kind
        (link vs grantedToV2) and the dialog needs to branch on that same
        way SharePoint's own UI does."""
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/permissions"
        return self._get(url).json().get("value", [])

    def delete_permission(self, drive_id: str, item_id: str, permission_id: str) -> None:
        """Revokes one specific permission (a named person's access, or a
        sharing link) - "Stop sharing" in the Manage Access dialog. Graph
        rejects deleting an inherited permission (inheritedFrom != null);
        the dialog is expected to not offer the button for those rather
        than relying on this to fail gracefully."""
        url = f"{constants.GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/permissions/{permission_id}"
        self._write("DELETE", url)
