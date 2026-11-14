"""Reads jarvis.toml and the secrets in .env."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "jarvis.toml"
ENV_FILE = ROOT / ".env"
DATA_DIR = ROOT / "jarvis_data"

EFFORTS = ("low", "medium", "high")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    your_name: str = ""
    city: str = ""
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    web_search: bool = True
    daily_budget_usd: float = 1.0
    new_conversation_after_hours: float = 6
    max_conversation_messages: int = 60


def load_settings(path: Path = CONFIG_FILE) -> Settings:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")).get("jarvis", {})
    except FileNotFoundError:
        raise ConfigError(f"{path.name} is missing. Download Jarvis again (see JARVIS.md).") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} has a typo: {exc}") from None
    defaults = Settings()
    settings = Settings(
        your_name=str(raw.get("your_name", defaults.your_name)).strip(),
        city=str(raw.get("city", defaults.city)).strip(),
        model=str(raw.get("model", defaults.model)).strip(),
        effort=str(raw.get("effort", defaults.effort)).strip().lower(),
        web_search=bool(raw.get("web_search", defaults.web_search)),
        daily_budget_usd=float(raw.get("daily_budget_usd", defaults.daily_budget_usd)),
        new_conversation_after_hours=float(raw.get("new_conversation_after_hours",
                                                   defaults.new_conversation_after_hours)),
        max_conversation_messages=int(raw.get("max_conversation_messages", defaults.max_conversation_messages)),
    )
    if settings.effort not in EFFORTS:
        raise ConfigError(f'effort in {path.name} must be one of {", ".join(EFFORTS)} (it is "{settings.effort}")')
    if settings.daily_budget_usd <= 0:
        raise ConfigError(f"daily_budget_usd in {path.name} must be more than 0")
    if settings.max_conversation_messages < 10:
        raise ConfigError(f"max_conversation_messages in {path.name} must be at least 10")
    return settings


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Double-click JARVIS-SETUP.bat to set Jarvis up.")
    return value
