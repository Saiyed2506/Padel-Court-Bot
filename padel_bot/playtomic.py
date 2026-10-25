"""Read-only access to Playtomic's (unofficial, undocumented) public endpoints.

These are the same endpoints the Playtomic app and website use to show free
courts. They need no login. Playtomic can change or block them at any time;
if that happens this module raises PlaytomicError and the watcher alerts you.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

BASE_URL = "https://api.playtomic.io/v1"


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


def _parse_court(raw: dict) -> Court:
    props = raw.get("properties") or {}
    return Court(
        resource_id=str(raw.get("resource_id", "")),
        name=str(raw.get("name") or raw.get("resource_id", "?")),
        indoor=str(props.get("resource_type", "")).lower() == "indoor",
        size=str(props.get("resource_size", "")).lower(),
    )


def _parse_club(raw: dict) -> Club:
    addr = raw.get("address") or {}
    address = ", ".join(p for p in (addr.get("street"), addr.get("postal_code"), addr.get("city")) if p)
    return Club(
        tenant_id=str(raw.get("tenant_id", "")),
        name=str(raw.get("tenant_name", "")),
        address=address,
        courts=tuple(_parse_court(r) for r in raw.get("resources") or []),
    )


class PlaytomicClient:
    def __init__(self, user_agent: str, timeout_s: float = 20.0):
        self.user_agent = user_agent
        self.timeout_s = timeout_s

    def _get(self, path: str, params: dict[str, str] | None = None):
        url = f"{BASE_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 429 = slow down, 5xx = their problem: both worth retrying later.
            # Other 4xx usually mean the endpoint changed or we were blocked.
            retryable = exc.code == 429 or exc.code >= 500
            raise PlaytomicError(f"HTTP {exc.code} for {path}", status=exc.code, retryable=retryable) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise PlaytomicError(f"network error for {path}: {exc}") from None
        except json.JSONDecodeError:
            raise PlaytomicError(f"unexpected non-JSON reply for {path}", retryable=False) from None

    def find_clubs(self, lat: float, lon: float, radius_m: int) -> list[Club]:
        data = self._get("/tenants", {
            "sport_id": "PADEL",
            "coordinate": f"{lat:.6f},{lon:.6f}",
            "radius": str(radius_m),
        })
        return [_parse_club(t) for t in data or []]

    def get_club(self, tenant_id: str) -> Club:
        quoted = urllib.parse.quote(tenant_id, safe="")
        club = _parse_club(self._get(f"/tenants/{quoted}"))
        if not club.courts:
            resources = self._get(f"/tenants/{quoted}/resources")
            club = Club(club.tenant_id, club.name, club.address,
                        tuple(_parse_court(r) for r in resources or []))
        return club

    def get_availability(self, tenant_id: str, start_utc: datetime, end_utc: datetime) -> list[dict]:
        """Raw availability between two UTC instants (keep the range within ~1 day)."""
        fmt = "%Y-%m-%dT%H:%M:%S"
        data = self._get("/availability", {
            "sport_id": "PADEL",
            "tenant_id": tenant_id,
            "start_min": start_utc.strftime(fmt),
            "start_max": end_utc.strftime(fmt),
        })
        if not isinstance(data, list):
            raise PlaytomicError("availability reply was not a list", retryable=False)
        return data
