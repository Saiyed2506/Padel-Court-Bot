"""Your wishes: the time windows the bot watches, changeable from Telegram.

config.toml holds the starting wishes. The first `/wish` or `/remove` saves
the current list to wishes.json, which from then on wins over config.toml.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import Window
from .timeutil import DAY_KEYS, parse_hhmm

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


def parse_wish(text: str) -> Window:
    """'<days> <HH:MM>[-HH:MM] [<minutes>] [prefer HH:MM,HH:MM]' -> Window.

    One time = exactly that start (and it's starred). A range = any game that
    fits completely inside it.
    """
    words = text.split()
    if len(words) < 2:
        raise WishError("example: /wish tue 17:00  or  /wish weekend 09:30-12:30")
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
            start_text, end_text = times.split("-", 1)
            start, end = parse_hhmm(start_text), parse_hhmm(end_text)
        else:
            start = parse_hhmm(times)
            end = start + duration
            preferred.add(start)
    except ValueError:
        raise WishError(f"can't read the time {times!r}; use HH:MM like 17:00 or 16:00-20:30") from None
    if end > 24 * 60:
        raise WishError("the game has to end by midnight")
    if end - start < duration:
        raise WishError(f"{hhmm(start)}-{hhmm(end)} is too short for a {duration}-minute game")
    outside = [p for p in preferred if not start <= p <= end - duration]
    if outside:
        raise WishError(f"preferred time {hhmm(outside[0])} isn't a possible start in {hhmm(start)}-{hhmm(end)}")
    return Window(days=days, start_min=start, end_min=end, durations=frozenset({duration}),
                  preferred_starts=frozenset(preferred))


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
    text = f"{describe_days(window.days)} {when}, {lengths} min"
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
    }


def _from_json(raw: dict) -> Window:
    return Window(
        days=frozenset(raw["days"]),
        start_min=parse_hhmm(raw["start"]),
        end_min=parse_hhmm(raw["end"]),
        durations=frozenset(int(d) for d in raw["duration_minutes"]),
        preferred_starts=frozenset(parse_hhmm(p) for p in raw.get("preferred_starts", [])),
    )


class WishStore:
    def __init__(self, path: Path, defaults: tuple[Window, ...]):
        self.path = path
        self.defaults = tuple(defaults)

    def load(self) -> tuple[Window, ...]:
        if not self.path.exists():
            return self.defaults
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return tuple(_from_json(w) for w in raw["wishes"])
        except (ValueError, KeyError, TypeError):
            return self.defaults  # damaged file: fall back to config.toml

    def save(self, windows: tuple[Window, ...]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"wishes": [_to_json(w) for w in windows]}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def reset(self) -> tuple[Window, ...]:
        self.path.unlink(missing_ok=True)
        return self.defaults
