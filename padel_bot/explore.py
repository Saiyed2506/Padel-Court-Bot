"""One-off diagnosis: where does Playtomic serve event (tournament) data?

Run once from your laptop with `py -m padel_bot explore-events <event link>`
(or explore-events.bat). It reads the club page and the event page (two
read-only requests), prints a short summary to screenshot, and writes details
to explore_report.txt.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from .playtomic import Club, _enclosing_objects, _page_texts

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


def run(club: Club, event_url: str | None, user_agent: str, report_path: Path, word: str = "Newcomer") -> None:
    pages = [("CLUB PAGE", club.url)] + ([("EVENT PAGE", event_url)] if event_url else [])
    out = [f"Club: {club.name} ({club.tenant_id})"]
    details = []
    for label, url in pages:
        status, ctype, body = fetch(url, user_agent)
        out.append(f"\n{label} [{status}], {len(body)} chars")
        details.append(f"===== {label}\n{url}\nstatus {status}, content-type {ctype}\n")
        if status != 200:
            details.append(body[:1500])
            continue
        found = analyze(body, word)
        for obj in found["objects"]:
            out.append(f"  object around '{word}', keys: {', '.join(sorted(obj))[:300]}")
            out.append("    " + json.dumps(obj, ensure_ascii=False)[:350])
        for snippet in found["snippets"]:
            out.append(f"  text around '{word}': ...{snippet[:300]}...")
        if not found["objects"] and not found["snippets"]:
            out.append(f"  '{word}' not found")
        out.append(f"  links: {' | '.join(found['links'])[:400] or '-'}")
        out.append(f"  api addresses: {' | '.join(found['apis'])[:400] or '-'}")
        details.append(json.dumps(found, indent=1, ensure_ascii=False)[:20000])
    report_path.write_text("\n".join(details), encoding="utf-8")
    print("\n".join(out))
    print(f"\nDetails saved to {report_path.name}.")
