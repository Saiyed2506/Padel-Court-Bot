"""Turn Playtomic's raw availability into slots, and decide which ones you want."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo

from .config import Settings, Window
from .playtomic import Court
from .timeutil import DAY_KEYS, minutes_of_day


@dataclass(frozen=True)
class Slot:
    resource_id: str
    court_name: str
    start: datetime              # club local time, timezone-aware
    duration: int                # minutes
    price_eur: float | None
    price_text: str
    preferred: bool = False

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration)

    @property
    def key(self) -> str:
        """Stable ID used to remember which slots you were already told about."""
        start_utc = self.start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        return f"{self.resource_id}|{start_utc}|{self.duration}"


def parse_price(text: str) -> float | None:
    """'48 EUR' -> 48.0, '48,50 EUR' -> 48.5, '' -> None."""
    parts = (text or "").split()
    if not parts:
        return None
    try:
        return float(parts[0].replace(",", "."))
    except ValueError:
        return None


def parse_availability(raw: list[dict], courts: dict[str, Court], tz: tzinfo) -> list[Slot]:
    """Flatten the API reply into Slot objects in local time.

    The API gives `start_date` + `start_time` in UTC (confirmed by the
    open-source padel-cli client; verify with `py -m padel_bot check --all`).
    """
    slots: list[Slot] = []
    for resource in raw:
        resource_id = str(resource.get("resource_id", ""))
        court = courts.get(resource_id)
        day = str(resource.get("start_date", ""))[:10]
        for item in resource.get("slots") or []:
            start_time = str(item.get("start_time", ""))
            if len(start_time) == 5:
                start_time += ":00"
            try:
                start_utc = datetime.fromisoformat(f"{day}T{start_time}").replace(tzinfo=timezone.utc)
                duration = int(item.get("duration", 0))
            except ValueError:
                continue
            price_text = str(item.get("price", "") or "")
            slots.append(Slot(
                resource_id=resource_id,
                court_name=court.name if court else resource_id,
                start=start_utc.astimezone(tz),
                duration=duration,
                price_eur=parse_price(price_text),
                price_text=price_text,
            ))
    return slots


def court_is_wanted(court: Court | None, settings: Settings) -> bool:
    # Unknown courts are skipped: better to miss one than to alert for outdoor.
    if court is None or court.name in settings.exclude_courts:
        return False
    if settings.indoor_only and not court.indoor:
        return False
    return settings.court_size == "any" or court.size == settings.court_size


def matching_window(slot: Slot, windows: tuple[Window, ...]) -> Window | None:
    """The first window this slot fits completely inside, or None."""
    day = DAY_KEYS[slot.start.weekday()]
    start = minutes_of_day(slot.start)
    for window in windows:
        if (day in window.days
                and slot.duration in window.durations
                and start >= window.start_min
                and start + slot.duration <= window.end_min):
            return window
    return None


def find_matches(slots: list[Slot], courts: dict[str, Court], settings: Settings,
                 now: datetime, target_day=None) -> list[Slot]:
    """Slots on wanted courts, inside a window, not already started.

    Sorted by day; within a day preferred start times come first.
    """
    matches: list[Slot] = []
    for slot in slots:
        if target_day is not None and slot.start.date() != target_day:
            continue  # the UTC query range can spill into neighbouring days
        if slot.start <= now or not court_is_wanted(courts.get(slot.resource_id), settings):
            continue
        window = matching_window(slot, settings.windows)
        if window is None:
            continue
        preferred = minutes_of_day(slot.start) in window.preferred_starts
        if settings.only_preferred and window.preferred_starts and not preferred:
            continue
        matches.append(replace(slot, preferred=preferred))
    matches.sort(key=lambda s: (s.start.date(), not s.preferred, s.start, s.court_name))
    return matches
