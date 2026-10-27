"""Your wishes: the time windows the bot watches, changeable from Telegram.

config.toml holds the starting wishes. The first `/wish` or `/remove` saves
the current list to wishes.json, which from then on wins over config.toml.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from datetime import date, datetime

from .config import Window
from .timeutil import DAY_KEYS, format_day, parse_hhmm

MAX_WISHES = 10
_DAY_GROUPS = {
    "weekdays": DAY_KEYS[:5], "weekday": DAY_KEYS[:5],
    "weekend": DAY_KEYS[5:], "weekends": DAY_KEYS[5:],
    "daily": DAY_KEYS, "everyday": DAY_KEYS, "all": DAY_KEYS,
}
# German day names too, since the club and app are German.
_DAY_ALIASES = {"mo": "mon", "di": "tue", "mi": "wed", "do": "thu", "fr": "fri", "sa": "sat", "so": "sun"}


class WishError(ValueError):
    """A wish that couldn't be understood; the message is shown to you in Telegram."""


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _day(token: str) -> str:
    token = token.strip().lower()
    token = _DAY_ALIASES.get(token, token[:3])
    if token not in DAY_KEYS:
        raise WishError(f"unknown day {token!r} - use mon, tue, ... sun, weekdays or weekend")
    return token


def parse_days(text: str) -> frozenset[str]:
    """'tue', 'sat,sun', 'mon-fri', 'weekdays', 'weekend' -> day keys."""
    days: set[str] = set()
    for part in text.lower().split(","):
        part = part.strip()
        if part in _DAY_GROUPS:
            days.update(_DAY_GROUPS[part])
        elif "-" in part:
            first, last = (DAY_KEYS.index(_day(p)) for p in part.split("-", 1))
            i = first
            while True:  # also handles wrap-around such as fri-mon
                days.add(DAY_KEYS[i])
                if i == last:
                    break
                i = (i + 1) % 7
        elif part:
            days.add(_day(part))
    if not days:
        raise WishError("which days? e.g. tue, sat,sun, mon-fri, weekdays, weekend")
    return frozenset(days)


_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.?(\d{2,4})?$")


def parse_date(token: str, today: date) -> date | None:
    """'10.11.2026', '10.11.', '10.11' or '2026-11-10' -> date; None if it isn't a date."""
    m = _DATE.match(token)
    try:
        if m:
            day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
            if year:
                found = date(int(year) + (2000 if len(year) == 2 else 0), month, day)
            else:
                found = date(today.year, month, day)
                if found < today:
                    found = date(today.year + 1, month, day)
            return found
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            return date.fromisoformat(token)
    except ValueError:
        raise WishError(f"{token} isn't a real date") from None
    return None


def looks_like_date_wish(text: str) -> bool:
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return bool(_DATE.match(first) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", first))


def parse_wish(text: str, today: date | None = None) -> Window:
    """'<days or date> <HH:MM>[-HH:MM] [<minutes>] [prefer HH:MM,HH:MM]' -> Window.

    One time = exactly that start (and it's starred). A range = any START time
    inside it, e.g. 16:00-19:30 with 90 min = games starting 16:00 ... 19:30.
    A date (10.11.2026) makes it a one-time wish for that day.
    """
    today = today or date.today()
    # Friendly forms: "between 16:00 and 19:30", "16:00 - 19:30", "16:00 bis 19:30"
    text = re.sub(r"\b(between|zwischen|from|von|at|um)\b", " ", text, flags=re.I)
    text = re.sub(r"\s*(?:-|–|\band\b|\bbis\b|\bto\b|\bund\b)\s*(?=\d)", "-", text, flags=re.I)
    words = text.split()
    if len(words) < 2:
        raise WishError("example: /wish tue 17:00  or  /wish 10.11.2026 16:00-19:30")
    on_date = parse_date(words[0], today)
    if on_date is not None:
        if on_date < today:
            raise WishError(f"{on_date.strftime('%d.%m.%Y')} is in the past")
        days = frozenset(DAY_KEYS)
    else:
        days = parse_days(words[0])
    times = words[1]
    rest = words[2:]

    duration = 90
    preferred: set[int] = set()
    i = 0
    while i < len(rest):
        word = rest[i].lower().removesuffix("min")
        if word in ("prefer", "preferred", "star"):
            if i + 1 >= len(rest):
                raise WishError("prefer which times? e.g. prefer 16:30,17:00")
            try:
                preferred.update(parse_hhmm(t) for t in rest[i + 1].split(","))
            except ValueError:
                raise WishError(f"can't read the preferred times {rest[i + 1]!r}") from None
            i += 2
            continue
        if word.isdigit() and 30 <= int(word) <= 240:
            duration = int(word)
            i += 1
            continue
        raise WishError(f"don't understand {rest[i]!r}; game length is a number like 60, 90 or 120")

    try:
        if "-" in times:
            first_text, last_text = times.split("-", 1)
            start, last_start = parse_hhmm(first_text), parse_hhmm(last_text)
        else:
            start = last_start = parse_hhmm(times)
            preferred.add(start)
    except ValueError:
        raise WishError(f"can't read the time {times!r}; use HH:MM like 17:00 or 16:00-19:30") from None
    if last_start < start:
        raise WishError(f"{hhmm(start)}-{hhmm(last_start)}: the second time must be later")
    end = last_start + duration
    if end > 24 * 60:
        raise WishError("the game has to end by midnight")
    outside = [p for p in preferred if not start <= p <= last_start]
    if outside:
        raise WishError(f"preferred time {hhmm(outside[0])} isn't a start between {hhmm(start)} and {hhmm(last_start)}")
    return Window(days=days, start_min=start, end_min=end, durations=frozenset({duration}),
                  preferred_starts=frozenset(preferred), on_date=on_date)


def describe_days(days: frozenset[str]) -> str:
    if days == frozenset(DAY_KEYS):
        return "Every day"
    if days == frozenset(DAY_KEYS[:5]):
        return "Mon-Fri"
    if days == frozenset(DAY_KEYS[5:]):
        return "Sat-Sun"
    return ", ".join(d.capitalize() for d in DAY_KEYS if d in days)


def describe(window: Window) -> str:
    """'Mon-Fri 16:00-20:30, 90 min (starts 16:00-19:00), ⭐ 16:30 17:00'"""
    duration = min(window.durations)
    last_start = window.end_min - duration
    if last_start == window.start_min:
        when = f"{hhmm(window.start_min)}-{hhmm(window.end_min)}"
    else:
        when = (f"{hhmm(window.start_min)}-{hhmm(window.end_min)} "
                f"(starts {hhmm(window.start_min)}-{hhmm(last_start)})")
    lengths = "/".join(str(d) for d in sorted(window.durations))
    days = f"{format_day(datetime(window.on_date.year, window.on_date.month, window.on_date.day))} (once)" \
        if window.on_date else describe_days(window.days)
    text = f"{days} {when}, {lengths} min"
    stars = sorted(window.preferred_starts)
    if stars and not (last_start == window.start_min and stars == [window.start_min]):
        text += ", ⭐ " + " ".join(hhmm(p) for p in stars)
    return text


def _to_json(window: Window) -> dict:
    return {
        "days": [d for d in DAY_KEYS if d in window.days],
        "start": hhmm(window.start_min),
        "end": hhmm(window.end_min),
        "duration_minutes": sorted(window.durations),
        "preferred_starts": [hhmm(p) for p in sorted(window.preferred_starts)],
        **({"date": window.on_date.isoformat()} if window.on_date else {}),
    }


def _from_json(raw: dict) -> Window:
    return Window(
        days=frozenset(raw["days"]),
        start_min=parse_hhmm(raw["start"]),
        end_min=parse_hhmm(raw["end"]),
        durations=frozenset(int(d) for d in raw["duration_minutes"]),
        preferred_starts=frozenset(parse_hhmm(p) for p in raw.get("preferred_starts", [])),
        on_date=date.fromisoformat(raw["date"]) if raw.get("date") else None,
    )


def drop_past(windows, today: date) -> tuple[Window, ...]:
    """One-time wishes disappear once their date has passed."""
    return tuple(w for w in windows if w.on_date is None or w.on_date >= today)


class WishStore:
    def __init__(self, path: Path, defaults: tuple[Window, ...]):
        self.path = path
        self.defaults = tuple(defaults)

    def load(self, today: date | None = None) -> tuple[Window, ...]:
        if not self.path.exists():
            return self.defaults
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            windows = tuple(_from_json(w) for w in raw["wishes"])
        except (ValueError, KeyError, TypeError):
            return self.defaults  # damaged file: fall back to config.toml
        return drop_past(windows, today or date.today())

    def save(self, windows: tuple[Window, ...]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"wishes": [_to_json(w) for w in windows]}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def reset(self) -> tuple[Window, ...]:
        self.path.unlink(missing_ok=True)
        return self.defaults
