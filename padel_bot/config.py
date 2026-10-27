"""Load settings from config.toml (non-secret) and .env (secrets).

Secrets such as your Telegram bot token never live in config.toml or in the
code. They come from environment variables, which can be put in a local `.env`
file that git ignores.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .timeutil import DAY_KEYS, parse_hhmm


class ConfigError(Exception):
    """Raised for a missing or invalid setting, with a message meant for humans."""


@dataclass(frozen=True)
class Window:
    days: frozenset[str]          # e.g. {"mon", "tue"}
    start_min: int                # earliest start, minutes after midnight
    end_min: int                  # the game must END by this time
    durations: frozenset[int]     # e.g. {90}
    preferred_starts: frozenset[int] = frozenset()
    on_date: date | None = None   # set = a one-time wish for that date only (days ignored)


@dataclass(frozen=True)
class BookingSettings:
    max_price_eur: float = 70.0          # whole-court price limit
    players: int = 4
    split_payment: bool = True           # other players pay their share in Playtomic
    payment_methods: tuple[str, ...] = ("MERCHANT_WALLET", "CREDIT_CARD")  # tried in this order
    auto_min_hours: int = 24             # automatic bookings only this far ahead (free cancellation)
    max_per_day: int = 1                 # bookings by the bot per day of play
    max_per_week: int = 0                # 0 = no weekly limit
    max_active: int = 0                  # own cap on active bookings; 0 = none (Playtomic enforces its own)


@dataclass(frozen=True)
class Settings:
    club_url: str                 # https://playtomic.com/clubs/<name>
    club_name: str
    api_times_utc: bool
    booking_url: str
    indoor_only: bool
    court_size: str               # "double", "single" or "any"
    exclude_courts: frozenset[str]
    days_ahead: int
    windows: tuple[Window, ...]
    near_days: int
    near_interval_s: int
    far_interval_s: int
    newest_day_fast: bool
    jitter_s: int
    request_pause_s: tuple[float, float]
    quiet_hours: tuple[int, int] | None
    user_agent: str
    only_preferred: bool
    min_notice_min: int             # only alert for games starting at least this far ahead
    free_cancel_min: int            # club's free-cancellation limit before a game
    daily_report_min: int | None    # time of the daily check-in message; None = off
    telegram_commands: bool
    events_enabled: bool
    event_words: tuple[str, ...]
    events_interval_s: int
    events_days_ahead: int
    events_times_utc: bool
    overview_weekday: int | None     # 0 = Monday; None = no weekly overview
    overview_min: int
    alert_after_failed_rounds: int
    state_file: Path
    club_file: Path               # cached club ID and courts
    booking: BookingSettings = field(default_factory=BookingSettings)


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, '#' comments, optional quotes.

    Variables already set in the real environment win over the file.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in "
            f"(see README, 'Telegram setup')."
        )
    return value


def _days(values: list[str], where: str) -> frozenset[str]:
    days = frozenset(v.strip().lower()[:3] for v in values)
    unknown = days - set(DAY_KEYS)
    if not days or unknown:
        raise ConfigError(f"{where}: days must be from {DAY_KEYS}, got {values}")
    return days


def _window(raw: dict, index: int) -> Window:
    where = f"[[watch.window]] #{index + 1}"
    try:
        start = parse_hhmm(raw["start"])
        end = parse_hhmm(raw["end"])
        durations = raw.get("duration_minutes", [90])
        if isinstance(durations, int):
            durations = [durations]
        preferred = frozenset(parse_hhmm(t) for t in raw.get("preferred_starts", []))
    except KeyError as exc:
        raise ConfigError(f"{where}: missing {exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    if end <= start:
        raise ConfigError(f"{where}: end must be after start")
    if not durations or any(int(d) <= 0 for d in durations):
        raise ConfigError(f"{where}: duration_minutes must be positive")
    return Window(
        days=_days(raw.get("days", []), where),
        start_min=start,
        end_min=end,
        durations=frozenset(int(d) for d in durations),
        preferred_starts=preferred,
    )


def _booking(raw: dict) -> BookingSettings:
    d = BookingSettings()
    try:
        b = BookingSettings(
            max_price_eur=float(raw.get("max_price_eur", d.max_price_eur)),
            players=int(raw.get("players", d.players)),
            split_payment=bool(raw.get("split_payment", d.split_payment)),
            payment_methods=tuple(str(m).upper() for m in raw.get("payment_methods", d.payment_methods)),
            auto_min_hours=int(raw.get("auto_min_hours", d.auto_min_hours)),
            max_per_day=int(raw.get("max_per_day", d.max_per_day)),
            max_per_week=int(raw.get("max_per_week", d.max_per_week)),
            max_active=int(raw.get("max_active", d.max_active)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[booking]: {exc}") from None
    if b.max_price_eur <= 0:
        raise ConfigError("[booking] max_price_eur must be more than 0")
    if not 2 <= b.players <= 4:
        raise ConfigError("[booking] players must be 2 or 4")
    if b.max_per_day < 1:
        raise ConfigError("[booking] max_per_day must be at least 1")
    return b


def _api_times_utc(value: str) -> bool:
    value = str(value).lower()
    if value not in ("utc", "local"):
        raise ConfigError('club.api_times must be "utc" or "local"')
    return value == "utc"


def load_settings(config_path: Path, *, require_club: bool = True) -> Settings:
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from None

    club = data.get("club", {})
    courts = data.get("courts", {})
    watch = data.get("watch", {})
    polling = data.get("polling", {})
    notify = data.get("notify", {})

    club_url = str(club.get("url", "")).strip()
    if not club_url:
        # Set up earlier but config.toml was replaced (e.g. by an update): reuse club.json.
        from .playtomic import load_club
        cached = load_club(config_path.parent / "club.json")
        club_url = cached.url if cached else ""
    if require_club and not club_url:
        raise ConfigError(
            "club.url is empty in config.toml. Double-click START-HERE.bat, or run "
            "`py -m padel_bot set-club <link to the club on playtomic.com>`."
        )

    size = str(courts.get("size", "double")).lower()
    if size not in ("double", "single", "any"):
        raise ConfigError('courts.size must be "double", "single" or "any"')

    windows = tuple(_window(w, i) for i, w in enumerate(watch.get("window", [])))
    if not windows:
        raise ConfigError("config.toml needs at least one [[watch.window]]")

    days_ahead = int(watch.get("days_ahead", 14))
    if not 0 <= days_ahead <= 30:
        raise ConfigError("watch.days_ahead must be between 0 and 30")

    near_minutes = float(polling.get("near_interval_minutes", 5))
    far_minutes = float(polling.get("far_interval_minutes", 20))
    if near_minutes < 2 or far_minutes < 2:
        # Deliberate floor: polling faster than this is how bots get blocked.
        raise ConfigError("polling intervals must be at least 2 minutes")

    report = str(notify.get("daily_report", "08:00")).strip().lower()
    try:
        daily_report_min = None if report in ("", "off", "none") else parse_hhmm(report)
    except ValueError:
        raise ConfigError('notify.daily_report must look like "08:00" or "off"') from None

    events = data.get("events", {})
    events_minutes = float(events.get("check_every_minutes", 3))
    if events_minutes < 2:
        # Each check is a logged-in request; faster than this risks your account.
        raise ConfigError("events.check_every_minutes must be at least 2")
    events_interval_s = int(events_minutes * 60)
    overview = str(events.get("weekly_overview", "sun 18:00")).strip().lower()
    if overview in ("", "off", "none"):
        overview_weekday, overview_min = None, 0
    else:
        try:
            day_text, time_text = overview.split()
            overview_weekday, overview_min = DAY_KEYS.index(day_text[:3]), parse_hhmm(time_text)
        except ValueError:
            raise ConfigError('events.weekly_overview must look like "sun 18:00" or "off"') from None

    pause = polling.get("pause_between_requests_seconds", [2, 4])
    quiet = polling.get("quiet_hours", ["23:00", "07:00"])
    quiet_hours = (parse_hhmm(quiet[0]), parse_hhmm(quiet[1])) if quiet else None

    return Settings(
        club_url=club_url,
        club_name=str(club.get("name", "your club")),
        api_times_utc=_api_times_utc(club.get("api_times", "utc")),
        booking_url=str(club.get("booking_url", "") or "{club_url}?date={date}"),
        indoor_only=bool(courts.get("indoor_only", True)),
        court_size=size,
        exclude_courts=frozenset(str(n) for n in courts.get("exclude_names", [])),
        days_ahead=days_ahead,
        windows=windows,
        near_days=int(polling.get("near_days", 4)),
        near_interval_s=int(near_minutes * 60),
        far_interval_s=int(far_minutes * 60),
        newest_day_fast=bool(polling.get("newest_day_fast", True)),
        jitter_s=int(polling.get("jitter_seconds", 60)),
        request_pause_s=(float(pause[0]), float(pause[1])),
        quiet_hours=quiet_hours,
        user_agent=str(polling.get("user_agent", "padel-court-bot/1.0 (personal availability watcher)")),
        only_preferred=bool(notify.get("only_preferred", False)),
        min_notice_min=int(float(watch.get("min_notice_hours", 0)) * 60),
        free_cancel_min=int(float(watch.get("free_cancellation_hours", 24)) * 60),
        daily_report_min=daily_report_min,
        telegram_commands=bool(notify.get("telegram_commands", True)),
        events_enabled=bool(events.get("enabled", True)),
        event_words=tuple(str(w) for w in events.get("watch", [])),
        events_interval_s=events_interval_s,
        events_days_ahead=int(events.get("days_ahead", 120)),
        events_times_utc=_api_times_utc(events.get("api_times", "utc")),
        overview_weekday=overview_weekday,
        overview_min=overview_min,
        alert_after_failed_rounds=int(notify.get("alert_after_failed_rounds", 6)),
        state_file=config_path.parent / str(data.get("state_file", "state.json")),
        club_file=config_path.parent / "club.json",
        booking=_booking(data.get("booking", {})),
    )
