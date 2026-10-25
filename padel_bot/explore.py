"""One-off diagnosis: where does Playtomic serve event (tournament) data?

Run once from your laptop with `py -m padel_bot explore-events <event link>`
(or explore-events.bat). It makes a handful of read-only requests, prints a
short summary to screenshot, and writes details to explore_report.txt.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from .playtomic import Club, _enclosing_objects, _page_texts

_EVENT_KEY = re.compile(r'"(?:tournament_id|tournamentId)"\s*:')
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def candidates(club: Club, event_url: str | None) -> list[tuple[str, str]]:
    t = club.tenant_id
    urls = [("club page", club.url)]
    event_id = None
    if event_url:
        urls.append(("event share page", event_url))
        match = _UUID.search(event_url)
        event_id = match.group(0) if match else None
    urls += [
        ("website api: club tournaments", f"https://playtomic.com/api/clubs/tournaments?tenant_id={t}"),
        ("website api: tournaments", f"https://playtomic.com/api/tournaments?tenant_id={t}&sport_id=PADEL"),
        ("app api: tournaments", f"https://api.app.playtomic.io/v2/tournaments?tenant_id={t}&sport_id=PADEL"),
    ]
    if event_id:
        urls.append(("app api: this event", f"https://api.app.playtomic.io/v2/tournaments/{event_id}"))
    return urls


def fetch(url: str, user_agent: str) -> tuple[int | str, str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent,
                                                   "Accept": "application/json,text/html;q=0.9"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", "") if exc.headers else "", exc.read().decode("utf-8", "replace")
    except Exception as exc:  # network trouble: report it, keep exploring
        return "error", "", str(getattr(exc, "reason", exc))


def _walk(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


def find_events(text: str) -> list[dict]:
    """Objects that look like events (have a tournament ID), deduplicated."""
    found: dict[str, dict] = {}
    try:
        for obj in _walk(json.loads(text)):
            if obj.get("tournament_id") or obj.get("tournamentId"):
                found.setdefault(str(obj.get("tournament_id") or obj.get("tournamentId")), obj)
    except ValueError:
        for page_text in _page_texts(text):
            for match in _EVENT_KEY.finditer(page_text):
                for obj in _enclosing_objects(page_text, match.start()):
                    key = obj.get("tournament_id") or obj.get("tournamentId")
                    if key:
                        # keep the biggest object for each event (the full record)
                        if len(json.dumps(obj)) > len(json.dumps(found.get(str(key), {}))):
                            found[str(key)] = obj
                        break
    return list(found.values())


def _short(obj: dict) -> str:
    name = obj.get("name") or obj.get("tournament_name") or obj.get("title") or "?"
    start = obj.get("start_date") or obj.get("startDate") or "?"
    places = obj.get("available_places", obj.get("availablePlaces", "?"))
    return f"{str(name)[:40]} | {start} | places left: {places}"


def run(club: Club, event_url: str | None, user_agent: str, report_path: Path, words=("Newcomer", "Afterwork")) -> None:
    lines = [f"Club: {club.name} ({club.tenant_id})", ""]
    details = []
    for label, url in candidates(club, event_url):
        status, ctype, body = fetch(url, user_agent)
        events = find_events(body) if status == 200 else []
        kind = "json" if "json" in ctype else "html" if "html" in ctype else ctype.split(";")[0] or "-"
        mentions = [w for w in words if w.lower() in body.lower()]
        lines.append(f"[{status}] {label}: {kind}, {len(body)} chars, events: {len(events)}"
                     + (f", mentions {'/'.join(mentions)}" if mentions else ""))
        for obj in events[:3]:
            lines.append(f"      - {_short(obj)}")
        details.append(f"===== {label}\n{url}\nstatus {status}, content-type {ctype}\n")
        if events:
            details.append(f"{len(events)} event object(s); keys of the first: {sorted(events[0])}\n")
            for obj in events[:2]:
                details.append(json.dumps(obj, indent=1, ensure_ascii=False)[:4000] + "\n")
        else:
            details.append(body[:1500] + "\n")
    report_path.write_text("\n".join(details), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nDetails saved to {report_path.name}.")
