"""Read-only access to the public data behind Playtomic's website.

Playtomic's app API (api.app.playtomic.io) now requires a logged-in account,
and the old api.playtomic.io is gone. The playtomic.com website still shows
club pages and free courts to anyone without logging in; this module reads the
same two things the website does:

  * the club page (https://playtomic.com/clubs/<name>) for the club ID and courts
  * https://playtomic.com/api/clubs/availability for free slots per day

Both are undocumented and can change at any time; failures raise PlaytomicError
and the watcher alerts you.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

WEB_BASE = "https://playtomic.com"
# Optional language part, e.g. https://playtomic.com/de/clubs/... or /en-gb/clubs/...
CLUB_URL_PATTERN = re.compile(r"^https://(www\.)?playtomic\.com/([A-Za-z]{2}(-[A-Za-z]{2})?/)?clubs/[A-Za-z0-9_-]+$")
_NEXT_DATA = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


class PlaytomicError(Exception):
    """Any failure talking to Playtomic."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class Court:
    resource_id: str
    name: str
    indoor: bool
    size: str            # "double", "single" or "" if unknown


@dataclass(frozen=True)
class Club:
    tenant_id: str
    name: str
    address: str
    courts: tuple[Court, ...]
    url: str = ""


def normalize_club_url(url: str) -> str:
    """'playtomic.com/clubs/x?date=1#a' -> 'https://playtomic.com/clubs/x', or raise ValueError."""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    url = url.split("#", 1)[0].split("?", 1)[0].rstrip("/").replace("http://", "https://", 1)
    if not CLUB_URL_PATTERN.match(url):
        raise ValueError("expected a link like https://playtomic.com/clubs/<club-name>")
    return url


def _parse_court(raw: dict) -> Court | None:
    sport = str(raw.get("sport") or raw.get("sport_id") or "PADEL").upper()
    if sport != "PADEL":
        return None  # the club also has tennis, squash and pickleball
    features = {str(f).lower() for f in raw.get("features") or []}
    props = raw.get("properties") or {}
    resource_type = str(props.get("resource_type", "")).lower()
    size = str(props.get("resource_size", "")).lower()
    if not size:
        size = "double" if "double" in features else "single" if "single" in features else ""
    return Court(
        resource_id=str(raw.get("resourceId") or raw.get("resource_id") or ""),
        name=str(raw.get("name") or "?"),
        indoor="indoor" in features or resource_type == "indoor",
        size=size,
    )


def parse_club_page(html: str, url: str) -> Club:
    """Read club ID and courts from the JSON a Next.js page embeds for itself."""
    match = _NEXT_DATA.search(html)
    if not match:
        raise PlaytomicError("club page has no embedded data (did Playtomic change its website?)",
                             retryable=False)
    try:
        tenant = json.loads(match.group(1))["props"]["pageProps"]["tenant"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise PlaytomicError("club page data has an unexpected format", retryable=False) from None
    addr = tenant.get("address") or {}
    address = ", ".join(p for p in (addr.get("street"), addr.get("postal_code"), addr.get("city")) if p)
    courts = tuple(c for c in (_parse_court(r) for r in tenant.get("resources") or []) if c)
    return Club(
        tenant_id=str(tenant.get("tenant_id", "")),
        name=str(tenant.get("tenant_name", "")),
        address=address,
        courts=courts,
        url=url,
    )


def save_club(club: Club, path: Path) -> None:
    path.write_text(json.dumps(asdict(club), indent=1, ensure_ascii=False), encoding="utf-8")


def load_club(path: Path) -> Club | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Club(raw["tenant_id"], raw["name"], raw["address"],
                    tuple(Court(**c) for c in raw["courts"]), raw.get("url", ""))
    except (OSError, ValueError, KeyError, TypeError):
        return None


class PlaytomicClient:
    def __init__(self, user_agent: str, base_url: str = WEB_BASE, timeout_s: float = 20.0):
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _fetch(self, url: str, accept: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": accept})
        label = urllib.parse.urlparse(url).path
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # 429 = slow down, 5xx = their problem: both worth retrying later.
            # Other 4xx usually mean the endpoint changed or we were blocked.
            retryable = exc.code == 429 or exc.code >= 500
            hint = " (Playtomic wants a login for this)" if exc.code == 401 else ""
            raise PlaytomicError(f"HTTP {exc.code} for {label}{hint}", status=exc.code, retryable=retryable) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise PlaytomicError(f"network error for {label}: {getattr(exc, 'reason', exc)}") from None

    def get_club_page(self, club_url: str) -> Club:
        return parse_club_page(self._fetch(club_url, "text/html,application/xhtml+xml"), club_url)

    def get_availability(self, tenant_id: str, day: date) -> list[dict]:
        """Raw free slots for one day, grouped per court."""
        query = urllib.parse.urlencode({"tenant_id": tenant_id, "date": day.isoformat(), "sport_id": "PADEL"})
        text = self._fetch(f"{self.base_url}/api/clubs/availability?{query}", "application/json")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise PlaytomicError("availability reply was not JSON", retryable=False) from None
        if not isinstance(data, list):
            raise PlaytomicError("availability reply was not a list", retryable=False)
        return data


def load_or_fetch_club(client: PlaytomicClient, club_url: str, cache: Path, refresh: bool = False) -> Club:
    """The club from club.json, or from its web page (then saved to club.json).

    Caching means the club page - the request most likely to be blocked - is
    fetched once at setup instead of on every start.
    """
    cached = None if refresh else load_club(cache)
    if cached and cached.url == club_url and cached.tenant_id:
        return cached
    club = client.get_club_page(club_url)
    if not club.tenant_id:
        raise PlaytomicError("club page did not include a club ID", retryable=False)
    save_club(club, cache)
    return club
