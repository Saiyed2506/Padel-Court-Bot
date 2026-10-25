"""One-off diagnosis: where does Playtomic serve event (tournament) data?

Run once from your laptop with `py -m padel_bot explore-events <event link>`
(or explore-events.bat). It reads the event page and a few likely
activity-list pages (about 6 read-only requests) and writes a short report,
explore_report.txt, meant to be pasted into the chat.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from .playtomic import Club, _enclosing_objects, _page_texts, page_lines

_EVENT_KEY = re.compile(r'"(?:tournament_id|tournamentId)"\s*:')


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


_HREF = re.compile(r'href="([^"#]{2,200})"')
_API = re.compile(r'(https?://[a-z0-9.-]*playtomic\.[a-z]+/[A-Za-z0-9_./{}-]*api[A-Za-z0-9_./{}-]*'
                  r'|https?://api[a-z0-9.-]*playtomic\.[a-z]+[A-Za-z0-9_./{}-]*'
                  r'|"/api/[A-Za-z0-9_./{}-]{1,80}")')
_INTERESTING = re.compile(r"tournament|activit|event|turnier|class|lesson|match|open", re.IGNORECASE)


def analyze(body: str, word: str) -> dict:
    """What a page holds around `word`, which links it has and which API
    addresses it mentions - enough to plan a parser without seeing the page."""
    objects, snippets = [], []
    for text in _page_texts(body):
        pos = text.lower().find(word.lower())
        while pos >= 0 and len(objects) < 2:
            for obj in _enclosing_objects(text, pos, max_tries=60):
                if obj not in objects:
                    objects.append(obj)
                if len(objects) >= 2:
                    break
            if not objects and len(snippets) < 2:
                snippets.append(re.sub(r"\s+", " ", text[max(0, pos - 150):pos + 250]))
            pos = text.lower().find(word.lower(), pos + 1)
        if objects:
            break
    links = sorted({h for h in _HREF.findall(body) if _INTERESTING.search(h)})[:15]
    apis = sorted({a.strip('"') for a in _API.findall(body)})[:12]
    return {"objects": objects, "snippets": snippets, "links": links, "apis": apis}


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def visible_text(html: str, limit: int = 80) -> list[str]:
    """The text a visitor would read on the page, in order, without repeats."""
    return [line for line in page_lines(html, limit=2000, dedupe=True) if len(line) <= 140][:limit]


def _activity_links(html: str) -> list[str]:
    return sorted({h for h in _HREF.findall(html) if re.search(r"/(activities|tournaments|classes)/", h)})


def run(club: Club, event_url: str | None, user_agent: str, report_path: Path,
        words: tuple[str, ...] = ("Newcomer", "Afterwork")) -> None:
    """Fetch the event page and a few likely activity-list pages (read-only,
    about 6 requests), and write a short report meant to be pasted to Claude."""
    base = club.url.rstrip("/")
    event_id = _UUID.search(event_url).group(0) if event_url and _UUID.search(event_url) else None
    pages = []
    if event_url:
        pages.append(("event page", event_url))
    pages += [
        ("academy classes", f"{base}/academy/public-classes"),
        ("club tournaments?", f"{base}/tournaments"),
        ("club activities?", f"{base}/activities"),
        ("academy tournaments?", f"{base}/academy/tournaments"),
    ]
    if event_id:
        pages.append(("website event page?", re.sub(r"/clubs/.*$", f"/activities/tournaments/{event_id}", base)))

    report = [f"Club: {club.name} ({club.tenant_id})"]
    summary = list(report)
    for label, url in pages:
        status, ctype, body = fetch(url, user_agent)
        found = [w for w in words if w.lower() in body.lower()] if status == 200 else []
        links = _activity_links(body) if status == 200 else []
        line = f"[{status}] {label}: {len(body)} chars, activity links: {len(links)}"
        line += f", mentions {'/'.join(found)}" if found else ""
        summary.append(line)
        report.append(f"\n===== {label} [{status}]\n{url}")
        if status != 200:
            continue
        report.append("TEXT: " + " | ".join(visible_text(body))[:1800])
        if links:
            report.append("LINKS: " + " | ".join(links[:12])[:900])
            if label == "academy classes":  # also look at one class's own page
                first = links[0] if links[0].startswith("http") else "https://playtomic.com" + links[0]
                pages.append(("one class page", first))
        if label == "event page":
            data = analyze(body, words[0])
            if data["apis"]:
                report.append("APIS: " + " | ".join(data["apis"]))
    text = "\n".join(report)
    report_path.write_text(text, encoding="utf-8")
    print("\n".join(summary))
    print(f"\nReport saved to {report_path.name} ({len(text)} characters).")
    print("Open it in Notepad, press Ctrl+A and Ctrl+C, and paste it into the chat with Claude.")
