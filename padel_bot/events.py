"""Watch the club's events: group classes (public website) and Open Play
(needs your Playtomic login, see account.py).

You pick events by name words ("Newcomer", "Afterwork"). The bot tells you
  * when a new date of a watched event appears,
  * when a full event gets a free spot again,
  * every week (default Sunday 18:00) what's coming up.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable

from .account import AuthError, PlaytomicAccount
from .notify import NotifyError
from .playtomic import Club, PlaytomicClient, PlaytomicError, page_lines, page_links
from .timeutil import format_day, format_time

log = logging.getLogger("padel_bot")

SHARE_PAGE = "https://app.playtomic.com/tournaments/{id}"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_MONTHS = {
    "jan": 1, "januar": 1, "january": 1, "feb": 2, "februar": 2, "february": 2,
    "mär": 3, "märz": 3, "mar": 3, "march": 3, "apr": 4, "april": 4, "mai": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6, "jul": 7, "juli": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "okt": 10, "oktober": 10, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dez": 12, "dezember": 12, "dec": 12, "december": 12,
}
_LEVEL = re.compile(r"^\d+(?:[.,]\d+)?\s*[–-]\s*\d+(?:[.,]\d+)?$")
_PRICE = re.compile(r"^(?:€\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*€)$")


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: str                    # "Open Play" or "Group class"
    name: str
    start: datetime | None       # club local time
    duration: int | None = None  # minutes
    level: str = ""
    coach: str = ""
    price: str = ""
    registered: int | None = None
    capacity: int | None = None
    spots_left: int | None = None
    url: str = ""


# --- reading pages ---------------------------------------------------------

def _guess_date(day: int, month: int, today: date) -> date:
    """Pages show '30. Oktober' without a year: take the next such date."""
    candidate = date(today.year, month, day)
    return candidate if candidate >= today - timedelta(days=2) else date(today.year + 1, month, day)


def parse_day_month(text: str, today: date) -> date | None:
    """'Freitag, 30. Oktober' / 'Sat, Oct 31' -> date."""
    m = re.search(r"(\d{1,2})\.?\s+([A-Za-zÄÖÜäöü]+)", text) or None
    if m and m.group(2).lower() in _MONTHS:
        return _guess_date(int(m.group(1)), _MONTHS[m.group(2).lower()], today)
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\b", text)
    if m and m.group(1).lower() in _MONTHS:
        return _guess_date(int(m.group(2)), _MONTHS[m.group(1).lower()], today)
    return None


def parse_clock(text: str) -> tuple[int, int] | None:
    """'17:30h' / '11:30 AM' -> (hour, minute)."""
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if m.group(3):
        hour = hour % 12 + (12 if m.group(3).lower() == "pm" else 0)
    return hour, minute


def _after(lines: list[str], *labels: str) -> list[str]:
    wanted = {label.lower() for label in labels}
    for i, line in enumerate(lines):
        if line.lower() in wanted:
            return lines[i + 1:]
    return []


def _price(lines: list[str]) -> str:
    for line in lines:
        m = _PRICE.match(line)
        if m:
            amount = (m.group(1) or m.group(2)).replace(",", ".")
            value = float(amount)
            return f"€{value:.0f}" if value.is_integer() else f"€{value:.2f}"
    return ""


def parse_class_page(html: str, url: str, tz: tzinfo, today: date) -> Event:
    """A group class page on playtomic.com (German or English)."""
    lines = page_lines(html)
    event_id = (re.search(_UUID, url) or re.search(_UUID, html) or [""])[0]
    name = lines[0] if lines else "?"
    when = _after(lines, "Datum", "Date")
    start = None
    if len(when) >= 2:
        day, clock = parse_day_month(when[0], today), parse_clock(when[1])
        if day and clock:
            start = datetime(day.year, day.month, day.day, clock[0], clock[1], tzinfo=tz)
    length = _after(lines, "Dauer", "Duration")
    duration = int(re.search(r"\d+", length[0]).group(0)) if length and re.search(r"\d+", length[0]) else None
    level = next(iter(_after(lines, "Niveau", "Level")), "")
    coach = ""
    for line in _after(lines, "Trainer", "Coach", "Coaches")[:4]:
        if not re.fullmatch(r"[A-ZÄÖÜ]{1,3}", line):  # skip the avatar initials ("DA")
            coach = line
            break
    text = " | ".join(lines)
    players = re.search(r"(?:Spieler|Players)\s*\((\d+)/(\d+)\)", text) or re.search(r"(\d+)/(\d+)\s*(?:Plätze|spots)", text)
    registered, capacity = (int(players.group(1)), int(players.group(2))) if players else (None, None)
    return Event(event_id, "Group class", name, start, duration, level if _LEVEL.match(level) else "", coach,
                 _price(lines), registered, capacity,
                 capacity - registered if players else None, url)


def parse_open_play_page(html: str, url: str, tz: tzinfo, today: date) -> Event:
    """An event share page on app.playtomic.com, e.g.
    'Open Play | Sat, Oct 31 · 11:30 AM | Newcomer Club mit Coach David | Padel | 0 – 3 | ... | 0 of 10 spots | ... | €17.00'"""
    lines = page_lines(html)
    event_id = (re.search(_UUID, url) or [""])[0]
    start, name, kind = None, "", "Open Play"
    for i, line in enumerate(lines):
        if "·" in line and parse_clock(line) and parse_day_month(line, today):
            day, clock = parse_day_month(line, today), parse_clock(line)
            start = datetime(day.year, day.month, day.day, clock[0], clock[1], tzinfo=tz)
            name = lines[i + 1] if i + 1 < len(lines) else ""
            kind = lines[i - 1] if i > 0 and len(lines[i - 1]) < 40 else kind
            break
    level = next((line for line in lines if _LEVEL.match(line)), "")
    spots = next((re.search(r"(\d+)\s*(?:of|von)\s*(\d+)", line) for line in lines
                  if re.search(r"(\d+)\s*(?:of|von)\s*(\d+)\s*(?:spots|Plätze|Plätzen)", line)), None)
    registered, capacity = (int(spots.group(1)), int(spots.group(2))) if spots else (None, None)
    coach = (re.search(r"[Cc]oach\s+([A-ZÄÖÜ][\wäöüß-]+)", name) or [None, ""])[1]
    return Event(event_id, kind, name, start, None, level, coach, _price(lines),
                 registered, capacity, capacity - registered if spots else None, url)


def tournament_event(raw: dict, tz: tzinfo, times_utc: bool) -> Event:
    """An entry from the logged-in event list."""
    event_id = str(raw.get("tournament_id") or raw.get("id") or "")
    start = None
    stamp = str(raw.get("start_date") or "")
    try:
        naive = datetime.fromisoformat(stamp.replace("Z", ""))
        start = naive.replace(tzinfo=timezone.utc).astimezone(tz) if times_utc else naive.replace(tzinfo=tz)
    except ValueError:
        pass
    places = raw.get("available_places")
    name = str(raw.get("name") or raw.get("tournament_name") or "?")
    return Event(event_id, str(raw.get("type") or "Open Play").replace("_", " ").title(), name, start,
                 spots_left=places if isinstance(places, int) else None,
                 coach=(re.search(r"[Cc]oach\s+([A-ZÄÖÜ][\wäöüß-]+)", name) or [None, ""])[1],
                 url=SHARE_PAGE.format(id=event_id))


def name_matches(name: str, words: list[str]) -> bool:
    lowered = name.lower()
    return any(w.lower() in lowered for w in words)


# --- messages ----------------------------------------------------------------

def describe_event(e: Event) -> str:
    when = f"{format_day(e.start)} {format_time(e.start)}" if e.start else "date unknown"
    if e.start and e.duration:
        when += f"-{format_time(e.start + timedelta(minutes=e.duration))}"
    parts = [when, e.name]
    if e.coach and e.coach.lower() not in e.name.lower():
        parts.append(f"coach {e.coach}")
    if e.level:
        parts.append(f"level {e.level}")
    if e.price:
        parts.append(e.price)
    if e.spots_left is not None:
        spots = f"{e.spots_left} spot{'s' if e.spots_left != 1 else ''} left"
        if e.capacity:
            spots += f" (of {e.capacity})"
        parts.append("FULL" if e.spots_left == 0 else spots)
    return " · ".join(parts) + (f"\n{e.url}" if e.url else "")


# --- small JSON stores ---------------------------------------------------------

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


class EventWords:
    """Which event names to watch; changeable with /watch and /unwatch."""

    def __init__(self, path: Path, defaults: list[str]):
        self.path = path
        self.defaults = list(defaults)

    def load(self) -> list[str]:
        words = _read_json(self.path, None)
        return [str(w) for w in words] if isinstance(words, list) else list(self.defaults)

    def save(self, words: list[str]) -> None:
        _write_json(self.path, words)


# --- the watcher ---------------------------------------------------------------

class EventWatcher:
    def __init__(self, settings, web: PlaytomicClient, account: PlaytomicAccount | None, club: Club,
                 tz: tzinfo, state_path: Path, words: EventWords, notifier, *,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None):
        self.settings = settings
        self.web = web
        self.account = account
        self.club = club
        self.tz = tz
        self.state_path = state_path
        self.words = words
        self.notifier = notifier
        self.clock = clock or (lambda: datetime.now(tz))
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.state = _read_json(state_path, {})
        self.state.setdefault("events", {})
        self.state.setdefault("class_names", {})
        self.upcoming: list[Event] = []
        self.last_check: datetime | None = None
        self.open_play_problem: str | None = None

    # --- collecting ----------------------------------------------------------

    def _pause(self) -> None:
        self.sleep(self.rng.uniform(*self.settings.request_pause_s))

    def _classes(self, words: list[str], today: date) -> list[Event]:
        base = self.club.url.rstrip("/")
        listing = self.web.fetch_page(f"{base}/academy/public-classes")
        found = []
        for link in page_links(listing, r"/activities/public-classes/" + _UUID):
            class_id = re.search(_UUID, link).group(0)
            known = self.state["class_names"].get(class_id)
            if known is not None and not name_matches(known, words):
                continue  # we know this class and it isn't one you watch
            url = link if link.startswith("http") else "https://playtomic.com" + link
            self._pause()
            event = parse_class_page(self.web.fetch_page(url), url, self.tz, today)
            self.state["class_names"][class_id] = event.name
            if name_matches(event.name, words):
                found.append(replace(event, event_id=class_id))
        return found

    def _open_play(self, words: list[str], now: datetime, today: date) -> list[Event]:
        if self.account is None or not self.account.logged_in:
            raise AuthError("Not logged in to Playtomic - double-click playtomic-login.bat.")
        raws = self.account.list_tournaments(self.club.tenant_id, now.astimezone(timezone.utc))
        found = []
        for raw in raws:
            event = tournament_event(raw, self.tz, self.settings.events_times_utc)
            if not event.event_id or not name_matches(event.name, words):
                continue
            if "cancel" in str(raw.get("status", "")).lower():
                continue
            cached = self.state["events"].get(event.event_id, {})
            details_known = cached.get("details_from_page")
            if not details_known or event.spots_left is None:
                # The public event page has level, price and spots in plain text.
                self._pause()
                page = parse_open_play_page(self.web.fetch_page(event.url), event.url, self.tz, today)
                event = replace(event, level=page.level, price=page.price, registered=page.registered,
                                capacity=page.capacity, kind=page.kind or event.kind,
                                start=page.start or event.start,
                                spots_left=page.spots_left if page.spots_left is not None else event.spots_left)
                cached = {"details_from_page": True}
            else:
                event = replace(event, level=cached.get("level", ""), price=cached.get("price", ""),
                                capacity=cached.get("capacity"), kind=cached.get("kind", event.kind),
                                start=_parse_dt(cached.get("start")) or event.start)
            found.append(event)
        return found

    def collect(self, now: datetime) -> tuple[list[Event], set[str]]:
        """Watched events in the coming weeks, and which sources failed."""
        words = self.words.load()
        if not words:
            return [], set()
        today, failed, events = now.date(), set(), []
        horizon = now + timedelta(days=self.settings.events_days_ahead)
        for kind, source in (("Group class", lambda: self._classes(words, today)),
                             ("Open Play", lambda: self._open_play(words, now, today))):
            try:
                events += source()
                if kind == "Open Play":
                    self.open_play_problem = None
            except AuthError as exc:
                failed.add(kind)
                if self.open_play_problem != str(exc):
                    self.open_play_problem = str(exc)
                    self._send(f"⚠️ Events: {exc}\n(Group classes are still watched.)")
            except PlaytomicError as exc:
                failed.add(kind)
                log.warning("Could not read %s events: %s", kind, exc)
        events = [e for e in events if e.start is None or now < e.start <= horizon]
        events.sort(key=lambda e: (e.start or horizon, e.name))
        return events, failed

    # --- alerts --------------------------------------------------------------

    def _send(self, text: str) -> None:
        try:
            self.notifier.send(text)
        except NotifyError as exc:
            log.error("%s", exc)

    def restart_watching(self) -> None:
        """After the watched names change: list everything once instead of
        announcing old dates as 'new'."""
        self.state["initialized_kinds"] = []
        self.last_check = None

    def check(self) -> list[str]:
        """Look for new dates and freed spots. Returns the alert texts sent."""
        now = self.clock()
        events, failed = self.collect(now)
        previous = self.state["events"]
        initialized = set(self.state.get("initialized_kinds", []))
        fresh_kinds = {"Group class", "Open Play"} - initialized - failed
        listing, new, freed = [], [], []
        for e in events:
            group = _group(e)
            before = previous.get(e.event_id)
            if group in fresh_kinds:
                listing.append(e)  # first successful look at this source: just list it
            elif before is None:
                new.append(e)
            elif before.get("spots_left") == 0 and (e.spots_left or 0) > 0:
                freed.append(e)
        sent = []
        if fresh_kinds:
            words = ", ".join(self.words.load()) or "nothing yet (send /watch <name>)"
            body = "\n\n".join(describe_event(e) for e in listing) or "No upcoming dates right now."
            sources = " and ".join(sorted(fresh_kinds))
            sent.append(f"📅 Now watching {sources} events named: {words}\n\n{body}")
            self.state["initialized_kinds"] = sorted(initialized | fresh_kinds)
        sent += [f"🆕 New date: {describe_event(e)}" for e in new]
        sent += [f"🟢 Spot free again: {describe_event(e)}" for e in freed]
        for text in sent:
            self._send(text)

        # Remember the current state; keep old entries of sources that failed.
        kept = {k: v for k, v in previous.items() if v.get("kind_group") in failed}
        for e in events:
            entry = {key: value for key, value in asdict(e).items() if key != "start"}
            entry["start"] = e.start.isoformat() if e.start else None
            entry["kind_group"] = _group(e)
            entry["details_from_page"] = bool(previous.get(e.event_id, {}).get("details_from_page")
                                              or e.capacity is not None)
            kept[e.event_id] = entry
        self.state["events"] = {k: v for k, v in kept.items()
                                if _parse_dt(v.get("start")) is None or _parse_dt(v["start"]) > now}
        _write_json(self.state_path, self.state)
        self.upcoming = events if not failed else events + [
            _event_from_state(v) for v in kept.values() if v.get("kind_group") in failed]
        self.upcoming.sort(key=lambda e: (e.start or now + timedelta(days=999), e.name))
        self.last_check = now
        return sent

    def overview_text(self, events: list[Event] | None = None) -> str:
        """All upcoming dates of watched events, with spots left."""
        now = self.clock()
        events = [e for e in (self.upcoming if events is None else events) if e.start is None or e.start > now]
        words = ", ".join(self.words.load()) or "-"
        if not events:
            return f"📅 Upcoming events ({words}): no dates published right now."
        free = sum(1 for e in events if (e.spots_left or 0) > 0)
        return (f"📅 Upcoming events ({words}): {len(events)} date(s), {free} with free spots\n\n"
                + "\n\n".join(describe_event(e) for e in events))

    # --- schedule --------------------------------------------------------------

    def due(self, now: datetime) -> bool:
        return self.last_check is None or (now - self.last_check).total_seconds() >= self.settings.events_interval_s

    def overview_due(self, now: datetime) -> bool:
        if self.settings.overview_weekday is None:
            return False
        week = f"{now.isocalendar().year}-W{now.isocalendar().week}"
        at = self.settings.overview_weekday, self.settings.overview_min
        return (now.weekday() == at[0] and now.hour * 60 + now.minute >= at[1]
                and self.state.get("last_overview") != week)

    def tick(self) -> None:
        """Called from the main loop: checks when due, sends the weekly overview."""
        now = self.clock()
        if self.due(now):
            self.check()
        if self.overview_due(now):
            self._send(self.overview_text())
            self.state["last_overview"] = f"{now.isocalendar().year}-W{now.isocalendar().week}"
            _write_json(self.state_path, self.state)


def _group(e: Event) -> str:
    return "Group class" if e.kind == "Group class" else "Open Play"


def _event_from_state(entry: dict) -> Event:
    fields = {k: entry.get(k) for k in Event.__dataclass_fields__ if k != "start"}
    return Event(**{**fields, "start": _parse_dt(entry.get("start"))})


def _parse_dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
