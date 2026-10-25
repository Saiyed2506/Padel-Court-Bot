"""Time helpers: the club's time zone and human-friendly formatting.

Playtomic's API speaks UTC; you think in Hamburg time. Everything that
crosses that boundary goes through this module.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _last_sunday(year: int, month: int) -> date:
    # Day 31 exists in both March and October, the only months we need.
    d = date(year, month, 31)
    return d - timedelta(days=(d.weekday() - 6) % 7)


class CentralEuropeanTime(tzinfo):
    """Europe/Berlin without needing the 'tzdata' package.

    Windows ships no time zone database, so `zoneinfo.ZoneInfo("Europe/Berlin")`
    fails there unless you `pip install tzdata`. The EU rule is simple and fixed:
    summer time runs from the last Sunday of March 01:00 UTC to the last Sunday
    of October 01:00 UTC. Good enough for booking padel courts.
    """

    def _summer_bounds_utc(self, year: int) -> tuple[datetime, datetime]:
        start = datetime.combine(_last_sunday(year, 3), datetime.min.time()) + timedelta(hours=1)
        end = datetime.combine(_last_sunday(year, 10), datetime.min.time()) + timedelta(hours=1)
        return start, end

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=1) + self.dst(dt)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        # Treat the naive local time as if it were standard time to find UTC.
        # The one ambiguous hour per year (02:00-03:00 in late October) is
        # irrelevant for court bookings.
        approx_utc = dt.replace(tzinfo=None) - timedelta(hours=1)
        start, end = self._summer_bounds_utc(dt.year)
        return timedelta(hours=1) if start <= approx_utc < end else timedelta(0)

    def fromutc(self, dt: datetime) -> datetime:
        naive_utc = dt.replace(tzinfo=None)
        start, end = self._summer_bounds_utc(naive_utc.year)
        offset = timedelta(hours=2) if start <= naive_utc < end else timedelta(hours=1)
        return (naive_utc + offset).replace(tzinfo=self)

    def tzname(self, dt: datetime | None) -> str:
        return "CEST" if self.dst(dt) else "CET"


def club_timezone() -> tzinfo:
    """Return Europe/Berlin, preferring the system database when it exists."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/Berlin")
    except Exception:  # ZoneInfoNotFoundError on Windows without tzdata
        return CentralEuropeanTime()


def parse_hhmm(value: str) -> int:
    """'16:30' -> 990 (minutes after midnight)."""
    hours, minutes = value.strip().split(":")
    h, m = int(hours), int(minutes)
    if not (0 <= h <= 24 and 0 <= m < 60) or h * 60 + m > 24 * 60:
        raise ValueError(f"invalid time {value!r}, expected HH:MM")
    return h * 60 + m


def minutes_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def local_day_bounds_utc(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """The UTC start/end of one local calendar day (00:00:00 to 23:59:59)."""
    start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def format_day(dt: datetime) -> str:
    """'Tue 28 Oct' - fixed English names, independent of your Windows language."""
    return f"{_DAY_NAMES[dt.weekday()]} {dt.day} {_MONTH_NAMES[dt.month - 1]}"


def format_time(dt: datetime) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}"
