"""Load settings from config.toml (non-secret) and .env (secrets).

Secrets such as your Telegram bot token never live in config.toml or in the
code. They come from environment variables, which can be put in a local `.env`
file that git ignores.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class AutoBookSettings:
    enabled: bool = False
    dry_run: bool = True
    max_price_eur: float | None = None


@dataclass(frozen=True)
class Settings:
    tenant_id: str
    club_name: str
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
    alert_after_failed_rounds: int
    state_file: Path
    auto_book: AutoBookSettings = field(default_factory=AutoBookSettings)


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


def _auto_book(raw: dict) -> AutoBookSettings:
    enabled = bool(raw.get("enabled", False))
    dry_run = bool(raw.get("dry_run", True))
    max_price = raw.get("max_price_eur")
    max_price = float(max_price) if max_price not in (None, "", 0) else None
    if enabled and (max_price is None or max_price <= 0):
        raise ConfigError("[auto_book] enabled = true requires max_price_eur > 0")
    return AutoBookSettings(enabled=enabled, dry_run=dry_run, max_price_eur=max_price)


def load_settings(config_path: Path, *, require_tenant: bool = True) -> Settings:
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

    tenant_id = str(club.get("tenant_id", "")).strip()
    if require_tenant and not tenant_id:
        raise ConfigError(
            "club.tenant_id is empty in config.toml. Run "
            "`py -m padel_bot find-club` and paste the ID it prints."
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

    pause = polling.get("pause_between_requests_seconds", [2, 4])
    quiet = polling.get("quiet_hours", ["23:00", "07:00"])
    quiet_hours = (parse_hhmm(quiet[0]), parse_hhmm(quiet[1])) if quiet else None

    return Settings(
        tenant_id=tenant_id,
        club_name=str(club.get("name", "your club")),
        booking_url=str(club.get("booking_url", "")),
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
        alert_after_failed_rounds=int(notify.get("alert_after_failed_rounds", 6)),
        state_file=config_path.parent / str(data.get("state_file", "state.json")),
        auto_book=_auto_book(data.get("auto_book", {})),
    )
