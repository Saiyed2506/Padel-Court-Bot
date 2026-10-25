"""Command line entry point: `py -m padel_bot <command>` (Windows) or `python -m padel_bot <command>`."""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .booking import AutoBooker
from .config import ConfigError, Settings, load_dotenv, load_settings, require_env
from .notify import NotifyError, TelegramNotifier, booking_link, format_slot
from .playtomic import Club, PlaytomicClient, PlaytomicError, load_or_fetch_club, normalize_club_url
from .rules import court_is_wanted, find_matches, parse_availability
from .state import SeenState
from .timeutil import club_timezone, format_day, format_time
from .watcher import Watcher

log = logging.getLogger("padel_bot")


def telegram_from_env() -> TelegramNotifier:
    return TelegramNotifier(require_env("TELEGRAM_BOT_TOKEN"), require_env("TELEGRAM_CHAT_ID"))


def get_club(settings: Settings, refresh: bool = False) -> Club:
    client = PlaytomicClient(settings.user_agent)
    return load_or_fetch_club(client, settings.club_url, settings.club_file, refresh=refresh)


def print_club(club: Club, settings: Settings) -> None:
    print(f"\n{club.name}  ({club.address})")
    for court in club.courts:
        mark = "  <- watched" if court_is_wanted(court, settings) else ""
        kind = "indoor" if court.indoor else "outdoor"
        print(f"    {court.name:<20} {kind:<8} {court.size or '?':<7}{mark}")


def cmd_set_club(args, settings: Settings) -> int:
    from .setup_wizard import set_club_url

    try:
        url = normalize_club_url(args.url)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    set_club_url(args.config, url)
    club = get_club(load_settings(args.config), refresh=True)
    print_club(club, settings)
    print(f"\nSaved. Club ID {club.tenant_id}; courts cached in {settings.club_file.name}.")
    return 0


def cmd_check(args, settings: Settings) -> int:
    """One pass over all days, printed to the screen. Sends nothing, saves nothing."""
    tz = club_timezone()
    client = PlaytomicClient(settings.user_agent)
    club = get_club(settings)
    courts = {c.resource_id: c for c in club.courts}
    wanted = [c.name for c in club.courts if court_is_wanted(c, settings)]
    print(f"{club.name}: watching {', '.join(wanted) or 'NO courts - check [courts] in config.toml'}")

    now = datetime.now(tz)
    days = settings.days_ahead if args.days is None else min(args.days, settings.days_ahead)
    total = 0
    for offset in range(days + 1):
        if offset:
            time.sleep(random.uniform(*settings.request_pause_s))
        day = now.date() + timedelta(days=offset)
        slots = parse_availability(client.get_availability(club.tenant_id, day), courts, tz,
                                   settings.api_times_utc)
        if args.all:
            # Everything free on watched courts, any time: compare this with the app
            # to confirm the times are converted correctly.
            shown = sorted((s for s in slots if s.start.date() == day and s.start > now
                            and court_is_wanted(courts.get(s.resource_id), settings)),
                           key=lambda s: (s.start, s.court_name))
            print(f"\n{format_day(datetime(day.year, day.month, day.day))}: {len(shown)} free slot(s) on watched courts")
            for s in shown:
                print(f"   {format_time(s.start)}-{format_time(s.end)}  {s.duration:>3} min  {s.court_name:<16} {s.price_text}")
            continue
        matches = find_matches(slots, courts, settings, now, target_day=day)
        total += len(matches)
        for m in matches:
            print(format_slot(m, booking_link(settings.booking_url, club, day)))
    if not args.all:
        print(f"\n{total} matching slot(s) in the next {days + 1} day(s).")
    return 0


def cmd_test_notify(args, settings: Settings) -> int:
    link = booking_link(settings.booking_url, get_club(settings), datetime.now().date())
    telegram_from_env().send(f"✅ Padel bot test message. Tap to check the booking link opens your club: {link}")
    print("Sent. Check Telegram on your phone, and tap the link to see where it goes.")
    return 0


def cmd_telegram_chat_id(args, settings: Settings) -> int:
    notifier = TelegramNotifier(require_env("TELEGRAM_BOT_TOKEN"), "")
    chats = notifier.recent_chats()
    if not chats:
        print("No messages found. Open your bot in Telegram, press Start / send it 'hi', then run this again.")
        return 1
    for chat_id, name in chats:
        print(f"TELEGRAM_CHAT_ID={chat_id}    ({name})")
    print("\nCopy the line for your own account into .env")
    return 0


def cmd_setup(args, settings: Settings) -> int:
    from .setup_wizard import run_setup

    def run_check(days: int) -> None:
        fresh = load_settings(args.config)
        cmd_check(argparse.Namespace(all=True, days=days), fresh)

    return run_setup(args.config, run_check)


def cmd_run(args, settings: Settings) -> int:
    tz = club_timezone()
    notifier = telegram_from_env()
    booker = AutoBooker(settings.auto_book, confirm=lambda slot: False)  # confirmation flow: future work
    if settings.auto_book.enabled:
        log.warning("Auto-book is ON in DRY-RUN mode (max €%g): it will only log what it would do.",
                    settings.auto_book.max_price_eur)
    club = get_club(settings)
    wanted = [c.name for c in club.courts if court_is_wanted(c, settings)]
    if not wanted:
        raise ConfigError("No courts match [courts] in config.toml; nothing to watch.")
    log.info("Club: %s - watching %d court(s): %s", club.name, len(wanted), ", ".join(wanted))
    watcher = Watcher(settings, PlaytomicClient(settings.user_agent), club, notifier,
                      SeenState(settings.state_file), tz, booker)
    log.info("Watching days 0-%d. Press Ctrl+C to stop.", settings.days_ahead)
    watcher.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows terminals may not default to UTF-8; avoid crashes on € and emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="padel_bot", description="Watch Playtomic for free padel courts.")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true", help="more detailed logs")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="guided first-time setup (Telegram + club)")
    p.set_defaults(func=cmd_setup, needs_club=False)

    p = sub.add_parser("set-club", help="set the club from its playtomic.com link and list its courts")
    p.add_argument("url", help="e.g. https://playtomic.com/clubs/<club-name>")
    p.set_defaults(func=cmd_set_club, needs_club=False)

    p = sub.add_parser("check", help="check once and print matches (sends nothing)")
    p.add_argument("--all", action="store_true", help="print every free slot on watched courts, any time")
    p.add_argument("--days", type=int, help="only the next N days")
    p.set_defaults(func=cmd_check, needs_club=True)

    p = sub.add_parser("test-notify", help="send a test Telegram message")
    p.set_defaults(func=cmd_test_notify, needs_club=True)

    p = sub.add_parser("telegram-chat-id", help="show your Telegram chat ID (setup helper)")
    p.set_defaults(func=cmd_telegram_chat_id, needs_club=False)

    p = sub.add_parser("run", help="start watching (Ctrl+C to stop)")
    p.set_defaults(func=cmd_run, needs_club=True)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    load_dotenv(args.config.parent / ".env")
    try:
        settings = load_settings(args.config, require_club=args.needs_club)
        return args.func(args, settings)
    except ConfigError as exc:
        print(f"Config problem: {exc}", file=sys.stderr)
    except PlaytomicError as exc:
        print(f"Playtomic problem: {exc}", file=sys.stderr)
    except NotifyError as exc:
        print(f"Telegram problem: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
