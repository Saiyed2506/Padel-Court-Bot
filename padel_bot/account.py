"""Logged-in, read-only access to the Playtomic app API - only for listing events.

Free courts and group classes don't need this. Open Play events have no public
list, so for those the bot uses your account, with these limits:

  * Your password is used once (`py -m padel_bot login`) and never stored.
    Only the tokens Playtomic returns are kept, in playtomic_token.json
    (git-ignored; treat it like a password).
  * Only GET requests for event lists. There is no booking or payment code.
  * The login method is undocumented (taken from the open-source
    go-playtomic-api client) and may stop working at any time.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .playtomic import PlaytomicError

APP_API = "https://api.app.playtomic.io"
ROLES = ["ROLE_CUSTOMER"]  # what the app asks for on login and refresh


def readable_error(body: str) -> str:
    """Playtomic's own error sentence if there is one, e.g. 'You have exceeded the
    maximum number of 6 active bookings and matches allowed'."""
    try:
        data = json.loads(body)
    except ValueError:
        return " ".join(body.split())[:200]
    if isinstance(data, dict):
        for key in ("localized_message", "message", "error_description", "error", "status"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()[:200]
    return " ".join(body.split())[:200]


class AuthError(PlaytomicError):
    """The login is missing or no longer accepted; you need to log in again."""

    def __init__(self, message: str):
        super().__init__(message, status=401, retryable=False)


def _expired(stamp: str | None, margin_s: int = 60) -> bool:
    if not stamp:
        return False  # unknown expiry: find out when the server says 401
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds() < margin_s


class PlaytomicAccount:
    def __init__(self, token_path: Path, user_agent: str, base_url: str = APP_API, timeout_s: float = 20.0):
        self.token_path = token_path
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.token: dict = self._load()

    # --- token storage ---------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, token: dict) -> None:
        keep = {k: token.get(k) for k in ("access_token", "refresh_token", "access_token_expiration",
                                          "refresh_token_expiration", "user_id")}
        tmp = self.token_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(keep, indent=1), encoding="utf-8")
        os.replace(tmp, self.token_path)
        self.token = keep

    @property
    def logged_in(self) -> bool:
        return bool(self.token.get("refresh_token") or self.token.get("access_token"))

    def logout(self) -> None:
        self.token_path.unlink(missing_ok=True)
        self.token = {}

    # --- HTTP --------------------------------------------------------------

    def _request(self, method: str, path: str, *, query: dict | None = None,
                 body: dict | None = None, bearer: str | None = None):
        url = self.base_url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"User-Agent": self.user_agent, "Accept": "application/json",
                   "Content-Type": "application/json"}
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            # Playtomic's error text helps when a booking is refused (e.g. "slot not available").
            detail = exc.read().decode("utf-8", "replace")[:500] if method != "POST" or "auth" not in path else ""
            raise PlaytomicError(f"HTTP {exc.code}" + (f": {readable_error(detail)}" if detail else f" for {path}"),
                                 status=exc.code, retryable=retryable) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise PlaytomicError(f"network error for {path}: {getattr(exc, 'reason', exc)}") from None
        except json.JSONDecodeError:
            raise PlaytomicError(f"unexpected reply for {path}", retryable=False) from None

    # --- login -------------------------------------------------------------

    def login(self, email: str, password: str) -> None:
        """Exchange email + password for tokens. The password is not kept."""
        try:
            token = self._request("POST", "/v3/auth/login",
                                  body={"email": email, "password": password, "requested_user_roles": ROLES})
        except PlaytomicError as exc:
            if exc.status in (400, 401, 403, 404):
                raise AuthError("Playtomic didn't accept that email/password. If you normally sign in "
                                "with Google or Apple, set a Playtomic password first (app: 'Forgot password').") from None
            raise
        if not isinstance(token, dict) or not token.get("access_token"):
            raise PlaytomicError("login reply had no token", retryable=False)
        self._save(token)

    def _refresh(self) -> None:
        refresh = self.token.get("refresh_token")
        if not refresh or _expired(self.token.get("refresh_token_expiration"), margin_s=0):
            raise AuthError("Playtomic login expired - send /login to the bot to log in again.")
        try:
            token = self._request("POST", "/v3/auth/token",
                                  body={"refresh_token": refresh, "requested_user_roles": ROLES})
        except PlaytomicError as exc:
            if exc.status in (400, 401, 403):
                raise AuthError("Playtomic login expired - send /login to the bot to log in again.") from None
            raise
        if not isinstance(token, dict) or not token.get("access_token"):
            raise AuthError("Playtomic refused to renew the login - send /login to the bot.")
        self._save(token)

    def _access_token(self) -> str:
        if not self.logged_in:
            raise AuthError("Not logged in to Playtomic - send /login to the bot.")
        if not self.token.get("access_token") or _expired(self.token.get("access_token_expiration")):
            self._refresh()
        return self.token["access_token"]

    def get(self, path: str, query: dict | None = None):
        """GET with the login; renews the token once if the server rejects it."""
        return self._authed("GET", path, query=query)

    def send(self, method: str, path: str, body: dict | None = None):
        """POST/PATCH with the login - used only by booking.py."""
        return self._authed(method, path, body=body)

    def _authed(self, method: str, path: str, *, query: dict | None = None, body: dict | None = None):
        try:
            return self._request(method, path, query=query, body=body, bearer=self._access_token())
        except PlaytomicError as exc:
            if exc.status != 401 or isinstance(exc, AuthError):
                raise
        self._refresh()
        try:
            return self._request(method, path, query=query, body=body, bearer=self.token["access_token"])
        except PlaytomicError as exc:
            if exc.status == 401:
                raise AuthError("Playtomic rejected the login - send /login to the bot.") from None
            raise

    @property
    def user_id(self) -> str:
        return str(self.token.get("user_id") or "")

    # --- the one thing we read ---------------------------------------------

    def list_tournaments(self, tenant_id: str, from_utc: datetime, max_pages: int = 3) -> list[dict]:
        """Upcoming events (Open Play, tournaments) of one club."""
        events: list[dict] = []
        for page in range(max_pages):
            batch = self.get("/v2/tournaments", {
                "tenant_id": tenant_id,
                "sport_id": "PADEL",
                "from_start_date": from_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                "size": "50",
                "page": str(page),
            })
            if isinstance(batch, dict):  # some list endpoints wrap results
                batch = batch.get("content") or batch.get("items") or []
            if not isinstance(batch, list):
                raise PlaytomicError("event list reply was not a list", retryable=False)
            events.extend(b for b in batch if isinstance(b, dict))
            if len(batch) < 50:
                break
        return events
