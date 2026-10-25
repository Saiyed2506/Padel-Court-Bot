"""Interactive first-time setup: `py -m padel_bot setup` (or double-click START-HERE.bat).

Asks for the Telegram token, finds your chat ID and the club ID, writes them
into .env / config.toml, sends a test message and shows today's free slots so
you can compare them with the Playtomic app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .config import load_settings
from .notify import NotifyError, TelegramNotifier, booking_link
from .playtomic import PlaytomicClient, PlaytomicError
from .rules import court_is_wanted

TOKEN_PATTERN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def write_env_values(path: Path, values: dict[str, str]) -> None:
    """Set KEY=VALUE lines in .env, keeping any other lines as they are."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def set_tenant_id(config_path: Path, tenant_id: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    new, count = re.subn(r'(?m)^tenant_id\s*=\s*".*"$', f'tenant_id = "{tenant_id}"', text, count=1)
    if count != 1:
        raise ValueError("could not find the tenant_id line in config.toml")
    config_path.write_text(new, encoding="utf-8")


def _ask(prompt: str) -> str:
    return input(prompt).strip()


def _pause(prompt: str = "Press Enter when done... ") -> None:
    input(prompt)


def _step(number: int, title: str) -> None:
    print(f"\n{'=' * 60}\nSTEP {number}: {title}\n{'=' * 60}")


def existing_telegram() -> TelegramNotifier | None:
    """The Telegram setup from .env, if it's complete and the token still works."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    notifier = TelegramNotifier(token, chat_id)
    try:
        username = notifier.bot_username()
    except NotifyError:
        return None  # e.g. the token was revoked: set it up again
    keep = _ask(f"Telegram is already set up (@{username}). Keep it? (Y/n): ").lower()
    return None if keep.startswith("n") else notifier


def setup_telegram(env_path: Path) -> TelegramNotifier:
    existing = existing_telegram()
    if existing:
        return existing
    _step(1, "Create your Telegram bot (about 2 minutes)")
    print("""On your phone or PC, in Telegram:
  1. Tap the search bar, type  BotFather  and open the one with the blue tick.
  2. Send:  /newbot
  3. It asks for a name. Send e.g.:  My Padel Alerts
  4. It asks for a username ending in 'bot'. Send e.g.:  padel_alerts_<yourname>_bot
  5. It replies with a long token like 123456789:AAH... - copy it.
""")
    while True:
        token = _ask("Paste the token here (right-click or Ctrl+V), then Enter: ")
        if not TOKEN_PATTERN.match(token):
            print("That doesn't look like a bot token (numbers, a colon, then letters). Try again.")
            continue
        notifier = TelegramNotifier(token, "")
        try:
            username = notifier.bot_username()
            break
        except NotifyError as exc:
            print(f"Telegram didn't accept that token ({exc}). Copy it again from BotFather.")

    _step(2, "Say hello to your bot")
    print(f"In Telegram, open  https://t.me/{username}  (or search @{username}) and press START.")
    while True:
        _pause("Press Enter here after you pressed START... ")
        chats = notifier.recent_chats()
        if chats:
            break
        print("No message found yet. Press START (or send 'hi') to your bot, then try again.")
    chat_id, name = chats[-1]
    if len(chats) > 1:
        for i, (cid, cname) in enumerate(chats, 1):
            print(f"  {i}. {cname} ({cid})")
        choice = _ask("Several people wrote to the bot. Which number is you? ")
        if choice.isdigit() and 1 <= int(choice) <= len(chats):
            chat_id, name = chats[int(choice) - 1]
    write_env_values(env_path, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
    os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = token, chat_id
    print(f"Saved to {env_path.name} (chat: {name}). This file stays on your laptop only.")
    return TelegramNotifier(token, chat_id)


def setup_club(config_path: Path) -> None:
    _step(3, "Find Padel Amigos on Playtomic")
    settings = load_settings(config_path, require_tenant=False)
    client = PlaytomicClient(settings.user_agent)
    clubs = client.find_clubs(53.6333, 9.8500, 15000)
    found = [c for c in clubs if "amigos" in c.name.lower()]
    if not found:
        print("Couldn't find a club with 'amigos' in its name nearby. Clubs found:")
        for c in clubs:
            print(f"  - {c.name}")
        raise SystemExit("Send this list to Claude so the search can be fixed.")
    club = found[0]
    if len(found) > 1:
        for i, c in enumerate(found, 1):
            print(f"  {i}. {c.name} ({c.address})")
        choice = _ask("Which number is the Halstenbek club? ")
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            club = found[int(choice) - 1]
    set_tenant_id(config_path, club.tenant_id)
    print(f"Found: {club.name} ({club.address}) - saved its ID to config.toml.")
    watched = [c for c in club.courts if court_is_wanted(c, settings)]
    print(f"Courts that will be watched ({len(watched)}): {', '.join(c.name for c in watched) or 'NONE'}")
    if len(watched) != 4:
        print("!! Expected 4 indoor doubles courts. Tell Claude what is listed above.")


def run_setup(config_path: Path, run_check) -> int:
    print("Padel bot setup. This takes about 10 minutes. Ctrl+C cancels at any time.")
    env_path = config_path.parent / ".env"
    notifier = setup_telegram(env_path)
    try:
        setup_club(config_path)
    except PlaytomicError as exc:
        print(f"\nCouldn't reach Playtomic: {exc}")
        if "getaddrinfo" in str(exc) or "11001" in str(exc):
            print("Your laptop could not find the address api.playtomic.io. Telegram worked, so\n"
                  "your internet is fine; a company network, VPN, or security/ad-blocking\n"
                  "software may be blocking it. Send a screenshot of this to Claude.")
        print("Your Telegram setup is saved; running setup again skips it.")
        return 1

    settings = load_settings(config_path)
    _step(4, "Test message")
    from datetime import date
    link = booking_link(settings.booking_url, settings.tenant_id, date.today())
    notifier.send(f"✅ Padel bot is set up. Tap this link to check it opens the club: {link}")
    print("A test message was sent to Telegram. Tap the link in it.")
    opens = _ask("Did the link open Padel Amigos in Playtomic? (y/n): ").lower()
    if not opens.startswith("y"):
        print("OK - tell Claude where it went instead; the alerts still work, only the link needs fixing.")

    _step(5, "Compare times with the Playtomic app")
    print("Below is every free slot on the watched courts for today and tomorrow.")
    print("Open the Playtomic app, pick 2-3 of these and check the times match.\n")
    run_check(days=1)
    same = _ask("\nDo the times match the app? (y/n): ").lower()
    print("\n" + "=" * 60)
    if same.startswith("y"):
        print("All set! Double-click  run-bot.bat  to start watching.")
    else:
        print("Don't start the bot yet - tell Claude how many hours the times are off.")
    return 0
