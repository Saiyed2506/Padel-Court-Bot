"""First-time setup: Anthropic API key, a Telegram bot for Jarvis, your name."""

from __future__ import annotations

import os
import re
from pathlib import Path

import anthropic

from padel_bot.notify import NotifyError
from padel_bot.setup_wizard import _ask, _clear_screen, _step, bot_id, find_chat, write_env_values

from padel_bot.timeutil import club_timezone

from .app import JarvisTelegram
from .config import CONFIG_FILE, DATA_DIR, ENV_FILE, load_settings
from .store import Store

TOKEN_PATTERN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def _set_setting(key: str, value: str, path: Path = CONFIG_FILE) -> None:
    text = path.read_text(encoding="utf-8")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    new, count = re.subn(rf'(?m)^{key}\s*=\s*".*"$', lambda _: f'{key} = "{escaped}"', text, count=1)
    if count != 1:
        raise ValueError(f"could not find the {key} line in {path.name}")
    path.write_text(new, encoding="utf-8")


def check_api_key(key: str, model: str) -> str | None:
    """None if the key works for the model, else a reason. Free: no tokens used."""
    try:
        anthropic.Anthropic(api_key=key, max_retries=1).models.retrieve(model)
    except anthropic.AuthenticationError:
        return "Anthropic rejected that key"
    except anthropic.NotFoundError:
        return f"the key works, but model {model} isn't available to it (check 'model' in jarvis.toml)"
    except anthropic.APIConnectionError:
        return "couldn't reach Anthropic - check the internet connection"
    except anthropic.APIStatusError as exc:
        return f"Anthropic answered with error {exc.status_code}"
    return None


def setup_api_key(model: str) -> None:
    existing = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if existing and check_api_key(existing, model) is None:
        if not _ask("An Anthropic API key is already set up and works. Keep it? (Y/n): ").lower().startswith("n"):
            return
    _step(1, "Get an Anthropic API key - Jarvis' brain (about 5 minutes)")
    print("""Jarvis thinks with Claude, which you pay for per use (no subscription).
  1. Open  https://console.anthropic.com  in your browser (it may move you to
     platform.claude.com - that's the same thing) and sign up or log in.
  2. Billing: add a small amount of credit, e.g. $10. That lasts a while for
     normal chatting; /cost in Telegram shows Jarvis' estimate.
  3. IMPORTANT - set a spending limit: Settings -> Limits -> monthly limit,
     e.g. $20. Anthropic enforces it, so a bug can never cost you more.
  4. API Keys -> Create Key -> name it "jarvis" -> copy the key
     (it starts with sk-ant-). Treat it like a password.
""")
    while True:
        key = _ask("Paste the API key here (right-click or Ctrl+V), then Enter: ")
        _clear_screen()  # so a screenshot of this window never shows the key
        if not key.startswith("sk-ant-"):
            print("That doesn't look like an Anthropic API key (it starts with sk-ant-). Try again.")
            continue
        problem = check_api_key(key, model)
        if problem:
            print(f"That didn't work: {problem}. Try again.")
            continue
        write_env_values(ENV_FILE, {"ANTHROPIC_API_KEY": key})
        os.environ["ANTHROPIC_API_KEY"] = key
        print("API key accepted and saved to .env (hidden from this window on purpose).")
        return


def ask_jarvis_token() -> tuple[str, JarvisTelegram, str]:
    padel_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    while True:
        token = _ask("Paste the token here (right-click or Ctrl+V), then Enter: ")
        _clear_screen()
        if not TOKEN_PATTERN.match(token):
            print("That doesn't look like a bot token (numbers, a colon, then letters). Try again.")
            continue
        if padel_token and bot_id(token) == bot_id(padel_token):
            print("That's your padel bot's token. Jarvis needs its own, new bot (step 2 above). Try again.")
            continue
        telegram = JarvisTelegram(token, "")
        try:
            username = telegram.bot_username()
        except NotifyError as exc:
            print(f"Telegram didn't accept that token ({exc}). Copy it again from BotFather.")
            continue
        print(f"Token accepted for @{username} (hidden from this window on purpose).")
        return token, telegram, username


def setup_telegram() -> JarvisTelegram:
    token = os.environ.get("JARVIS_TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("JARVIS_CHAT_ID", "").strip()
    if token and chat_id:
        telegram = JarvisTelegram(token, chat_id)
        try:
            username = telegram.bot_username()
            if not _ask(f"Jarvis' Telegram bot is set up (@{username}). Keep it? (Y/n): ").lower().startswith("n"):
                return telegram
        except NotifyError:
            pass  # token revoked: set it up again
    _step(2, "Create a Telegram bot for Jarvis (about 2 minutes)")
    print("""Jarvis needs its OWN bot - not your padel bot. (Two programs can't both
read one bot's messages; they would steal each other's.)
In Telegram:
  1. Search  BotFather  and open the one with the blue tick.
  2. Send:  /newbot
  3. Name, e.g.:  Jarvis
  4. Username ending in 'bot', e.g.:  jarvis_<yourname>_bot
  5. Copy the token it sends you (123456789:AAH...).
""")
    token, telegram, username = ask_jarvis_token()
    _step(3, "Say hello to Jarvis")
    chat_id, name = find_chat(telegram, username)
    updates = telegram.get_updates(None, timeout=0)
    if updates:  # mark the hello as read, so Jarvis doesn't answer it later
        Store(DATA_DIR, club_timezone()).save_offset(updates[-1]["update_id"] + 1)
    write_env_values(ENV_FILE, {"JARVIS_TELEGRAM_TOKEN": token, "JARVIS_CHAT_ID": chat_id})
    os.environ["JARVIS_TELEGRAM_TOKEN"], os.environ["JARVIS_CHAT_ID"] = token, chat_id
    print(f"Saved (chat: {name}). Jarvis will only ever answer this chat.")
    return JarvisTelegram(token, chat_id)


def setup_personal() -> None:
    settings = load_settings()
    _step(4, "About you")
    name = _ask(f"What should Jarvis call you?{f' (Enter keeps {settings.your_name})' if settings.your_name else ''} ")
    if name:
        _set_setting("your_name", name)
    city = _ask(f"Which city are you in, for weather and local searches?"
                f"{f' (Enter keeps {settings.city})' if settings.city else ' (Enter to skip)'} ")
    if city:
        _set_setting("city", city)


def run_setup(first_hello) -> int:
    print("Welcome! This sets up Jarvis in 4 short steps. Your padel bot is not touched.")
    settings = load_settings()
    setup_api_key(settings.model)
    telegram = setup_telegram()
    setup_personal()
    _step(5, "First hello")
    print("Jarvis now says hello in Telegram (this uses the AI once, about $0.01-0.05)...")
    try:
        first_hello(telegram)
    except Exception as exc:  # show the problem, setup itself is done
        print(f"The hello didn't work: {exc}")
        return 1
    print("""
All set! Next:
  - Double-click run-jarvis.bat to start Jarvis. Keep that window open.
  - Optional: autostart-jarvis.bat starts Jarvis whenever you log in to Windows.
  - Then write to Jarvis in Telegram, e.g. "Remind me in 10 minutes to stretch".""")
    return 0
