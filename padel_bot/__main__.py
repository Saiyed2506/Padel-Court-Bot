"""Command line entry point: `py -m padel_bot <command>` (Windows) or `python -m padel_bot <command>`."""

from __future__ import annotations

import argparse
from dataclasses import replace
import logging
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .booking import BookingManager, PlaytomicBooker
from .config import ConfigError, Settings, load_dotenv, load_settings, require_env
from .notify import NotifyError, TelegramNotifier, booking_link, format_slot
from .playtomic import Club, PlaytomicClient, PlaytomicError, load_or_fetch_club, normalize_club_url
from .rules import court_is_wanted, find_matches, parse_availability
from .state import SeenState
from .timeutil import club_timezone, format_day, format_time
from . import secure
from .account import PlaytomicAccount
from .commands import CommandCenter
from .events import EventWatcher, EventWords
from .friends import Broadcaster, Friends
from .opening import OpeningSniper
from .polls import Polls
from .reminders import Reminders
from .menu import MENU_BUTTON, RuntimeSettings
from .watcher import Watcher
from .wishes import WishStore

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


def wish_store(settings: Settings) -> WishStore:
    return WishStore(settings.state_file.parent / "wishes.json", settings.windows)


def cmd_check(args, settings: Settings) -> int:
    """One pass over all days, printed to the screen. Sends nothing, saves nothing."""
    settings = replace(settings, windows=wish_store(settings).load())
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


def cmd_new_token(args, settings: Settings) -> int:
    from .setup_wizard import change_token
    return change_token(args.config.parent / ".env")


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


def check_security(home: Path) -> list[str]:
    """Lock down secret files and warn about risky set-ups (once per start)."""
    notes = []
    for name in (".env", "playtomic_token.json", "bookings_state.json", "friends.json"):
        secure.restrict(home / name)
    synced = secure.cloud_synced(home)
    if synced:
        notes.append(f"⚠️ Security: the bot folder is inside {synced}, so your secret files (.env with the "
                     "Telegram token, the Playtomic login) are copied to the cloud. Move the folder out, e.g. to "
                     "C:\\padel-bot, and run update.bat there.")
        log.warning("Bot folder is inside %s - secret files are synced to the cloud.", synced)
    return notes


def account_for(settings: Settings) -> PlaytomicAccount:
    return PlaytomicAccount(settings.state_file.parent / "playtomic_token.json", settings.user_agent)


def cmd_login(args, settings: Settings) -> int:
    import getpass

    print("Log in to Playtomic so the bot can read the club's Open Play events.\n"
          "Your password is sent to Playtomic once and is NOT saved; the bot keeps only\n"
          "the login token Playtomic gives back (playtomic_token.json - keep it private).\n"
          "The bot only READS the event list. It never books or pays.\n")
    email = input("Playtomic email: ").strip()
    password = getpass.getpass("Playtomic password (nothing shows while you type - that's normal; Enter when done): ")
    account = account_for(settings)
    account.login(email, password)
    del password
    print("\n✅ Logged in. Password not saved.\n")

    club = get_club(settings)
    raws = account.list_tournaments(club.tenant_id, datetime.now(timezone.utc))
    print(f"{club.name} has {len(raws)} upcoming event(s) in the app:")
    for raw in raws[:20]:
        print(f"   {str(raw.get('start_date'))[:16]}  {str(raw.get('name'))[:45]:<45}  places left: {raw.get('available_places')}")
    if raws:
        print(f"\n(fields: {', '.join(sorted(raws[0]))[:300]})")
    print("\nThe times above are as Playtomic sends them (probably UTC: 10:30 here = 11:30 in Hamburg in winter).")
    print("Start run-bot.bat (again) to begin watching events.")
    return 0


def cmd_logout(args, settings: Settings) -> int:
    account_for(settings).logout()
    print("Logged out: the Playtomic login token was deleted. Open Play events are no longer read.")
    return 0


def cmd_explore_events(args, settings: Settings) -> int:
    from .explore import run

    run(get_club(settings), args.event_url, settings.user_agent,
        settings.state_file.parent / "explore_report.txt")
    return 0


def cmd_run(args, settings: Settings) -> int:
    tz = club_timezone()
    home = settings.state_file.parent
    friends = Friends(home / "friends.json")
    notifier = Broadcaster(telegram_from_env(), friends)  # alerts go to you + approved friends
    account = account_for(settings)  # one shared login: events, booking and /login use the same tokens
    club = get_club(settings)
    wanted = [c.name for c in club.courts if court_is_wanted(c, settings)]
    if not wanted:
        raise ConfigError("No courts match [courts] in config.toml; nothing to watch.")
    log.info("Club: %s - watching %d court(s): %s", club.name, len(wanted), ", ".join(wanted))
    store = wish_store(settings)
    settings = replace(settings, windows=store.load())
    watcher = Watcher(settings, PlaytomicClient(settings.user_agent), club, notifier,
                      SeenState(settings.state_file), tz)
    watcher.polls = Polls(home / "polls.json", notifier, friends, clock=watcher.clock)  # "who's in?" on court alerts
    watcher.reminders = Reminders(home / "reminders.json", notifier, settings.free_cancel_min)
    runtime = RuntimeSettings(home / "bot_settings.json")
    runtime.apply(watcher)  # pause / night pause / only-⭐ set with buttons earlier
    if settings.events_enabled:
        words = EventWords(home / "event_words.json", list(settings.event_words))
        watcher.events = EventWatcher(watcher.settings, PlaytomicClient(settings.user_agent), account,
                                      club, tz, home / "events_state.json", words, notifier)
    watcher.bookings = BookingManager(
        lambda: watcher.settings, PlaytomicBooker(account, club.tenant_id, settings.booking),
        home / "bookings_state.json", home / "bookings_log.jsonl", notifier, tz, watcher.clock,
        fresh_slot=watcher.fresh_slot, reminders=watcher.reminders)
    watcher.sniper = OpeningSniper(watcher, watcher.bookings, days_ahead=settings.days_ahead)
    runtime.apply(watcher)  # again, now that the sniper exists (its on/off is saved there)
    if settings.telegram_commands:
        watcher.commands = CommandCenter(watcher, notifier, store, friends=friends, runtime=runtime)
        watcher.commands.register_menu()
    security_notes = check_security(home)
    try:
        status = "⏸ paused - tap 🎾 Menu → Settings to resume" if watcher.paused else "watching"
        notifier.send(f"✅ Padel bot started ({status}).\n\n"
                      + (watcher.commands.wishes_text() + "\n\nTap 🎾 Menu below for buttons." if watcher.commands else ""),
                      **({"keyboard": [[MENU_BUTTON]]} if watcher.commands else {}))
        for note in security_notes:
            notifier.send(note)
    except NotifyError as exc:
        log.error("Could not send the start message: %s", exc)
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

    p = sub.add_parser("new-token", help="swap in a new Telegram token after /revoke (keeps everything else)")
    p.set_defaults(func=cmd_new_token, needs_club=False)

    p = sub.add_parser("telegram-chat-id", help="show your Telegram chat ID (setup helper)")
    p.set_defaults(func=cmd_telegram_chat_id, needs_club=False)

    p = sub.add_parser("login", help="log in to Playtomic (for Open Play events; password not saved)")
    p.set_defaults(func=cmd_login, needs_club=True)

    p = sub.add_parser("logout", help="forget the Playtomic login")
    p.set_defaults(func=cmd_logout, needs_club=False)

    p = sub.add_parser("explore-events", help="one-off: find where Playtomic serves event data")
    p.add_argument("event_url", nargs="?", help="a shared event link (optional)")
    p.set_defaults(func=cmd_explore_events, needs_club=True)

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
