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
from .playtomic import PlaytomicClient, PlaytomicError
from .rules import court_is_wanted, find_matches, parse_availability
from .state import SeenState
from .timeutil import club_timezone, format_day, format_time, local_day_bounds_utc
from .watcher import Watcher

# Halstenbek, used to search for the club's ID.
HALSTENBEK = (53.6333, 9.8500)

log = logging.getLogger("padel_bot")


def telegram_from_env() -> TelegramNotifier:
    return TelegramNotifier(require_env("TELEGRAM_BOT_TOKEN"), require_env("TELEGRAM_CHAT_ID"))


def cmd_find_club(args, settings: Settings) -> int:
    client = PlaytomicClient(settings.user_agent)
    clubs = client.find_clubs(args.lat, args.lon, args.radius)
    term = args.search.lower()
    found = [c for c in clubs if term in c.name.lower()] if term else clubs
    if not found:
        print(f"No club matching {args.search!r} among {len(clubs)} nearby clubs:")
        for club in clubs:
            print(f"  - {club.name}")
        return 1
    for club in found:
        print(f"\n{club.name}  ({club.address})")
        print(f"  tenant_id = \"{club.tenant_id}\"")
        for court in club.courts:
            mark = "  <- watched" if court_is_wanted(court, settings) else ""
            kind = "indoor" if court.indoor else "outdoor"
            print(f"    {court.name:<20} {kind:<8} {court.size or '?':<7}{mark}")
    print("\nCopy the tenant_id line into the [club] section of config.toml.")
    return 0


def cmd_check(args, settings: Settings) -> int:
    """One pass over all days, printed to the screen. Sends nothing, saves nothing."""
    tz = club_timezone()
    client = PlaytomicClient(settings.user_agent)
    club = client.get_club(settings.tenant_id)
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
        start_utc, end_utc = local_day_bounds_utc(day, tz)
        slots = parse_availability(client.get_availability(settings.tenant_id, start_utc, end_utc), courts, tz)
        if args.all:
            # Everything free on watched courts, any time: compare this with the app
            # to confirm the times are converted correctly.
            shown = sorted((s for s in slots if s.start.date() == day and court_is_wanted(courts.get(s.resource_id), settings)),
                           key=lambda s: (s.start, s.court_name))
            print(f"\n{format_day(datetime(day.year, day.month, day.day))}: {len(shown)} free slot(s) on watched courts")
            for s in shown:
                print(f"   {format_time(s.start)}-{format_time(s.end)}  {s.duration:>3} min  {s.court_name:<16} {s.price_text}")
            continue
        matches = find_matches(slots, courts, settings, now, target_day=day)
        total += len(matches)
        for m in matches:
            print(format_slot(m, booking_link(settings.booking_url, settings.tenant_id, day)))
    if not args.all:
        print(f"\n{total} matching slot(s) in the next {days + 1} day(s).")
    return 0


def cmd_test_notify(args, settings: Settings) -> int:
    link = booking_link(settings.booking_url, settings.tenant_id, datetime.now().date())
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


def cmd_run(args, settings: Settings) -> int:
    tz = club_timezone()
    notifier = telegram_from_env()
    booker = AutoBooker(settings.auto_book, confirm=lambda slot: False)  # confirmation flow: future work
    if settings.auto_book.enabled:
        log.warning("Auto-book is ON in DRY-RUN mode (max €%g): it will only log what it would do.",
                    settings.auto_book.max_price_eur)
    watcher = Watcher(settings, PlaytomicClient(settings.user_agent), notifier,
                      SeenState(settings.state_file), tz, booker)
    if not watcher.load_courts():
        raise ConfigError("No courts match [courts] in config.toml; nothing to watch.")
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

    p = sub.add_parser("find-club", help="look up the club's tenant_id and courts")
    p.add_argument("--search", default="amigos", help="part of the club name (default: amigos)")
    p.add_argument("--lat", type=float, default=HALSTENBEK[0])
    p.add_argument("--lon", type=float, default=HALSTENBEK[1])
    p.add_argument("--radius", type=int, default=15000, help="metres (default 15000)")
    p.set_defaults(func=cmd_find_club, needs_tenant=False)

    p = sub.add_parser("check", help="check once and print matches (sends nothing)")
    p.add_argument("--all", action="store_true", help="print every free slot on watched courts, any time")
    p.add_argument("--days", type=int, help="only the next N days")
    p.set_defaults(func=cmd_check, needs_tenant=True)

    p = sub.add_parser("test-notify", help="send a test Telegram message")
    p.set_defaults(func=cmd_test_notify, needs_tenant=False)

    p = sub.add_parser("telegram-chat-id", help="show your Telegram chat ID (setup helper)")
    p.set_defaults(func=cmd_telegram_chat_id, needs_tenant=False)

    p = sub.add_parser("run", help="start watching (Ctrl+C to stop)")
    p.set_defaults(func=cmd_run, needs_tenant=True)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    load_dotenv(args.config.parent / ".env")
    try:
        settings = load_settings(args.config, require_tenant=args.needs_tenant)
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
