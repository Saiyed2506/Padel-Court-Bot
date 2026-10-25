"""Telegram commands: change wishes, pause, and ask for status from your phone.

Only messages from your own chat (TELEGRAM_CHAT_ID) are obeyed; anyone else
who finds the bot is ignored.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable

from .notify import NotifyError, booking_link, build_messages
from .wishes import MAX_WISHES, WishError, WishStore, describe, parse_wish

log = logging.getLogger("padel_bot")

HELP = """🎾 Padel bot commands

/wishes - show what I'm watching
/wish <days> <time> - add a wish, e.g.
   /wish tue 17:00   (exactly 17:00, 90 min)
   /wish weekend 09:30-12:30   (any game inside)
   /wish mon-fri 16:00-20:30 prefer 16:30,17:00
   /wish sat 10:00 120   (120-minute game)
   days: mon..sun, mon-fri, sat,sun, weekdays, weekend
/remove <number> - delete a wish (see /wishes)
/reset - back to the wishes in config.toml
/now - free slots matching your wishes right now
/status - is the bot running, last check
/pause - stop alerts   /resume - start again

Events (classes, Open Play):
/events - upcoming dates of watched events
/watch <name> - watch events with this in their name
/unwatch <number> - stop watching (see /events)"""

# Shown in Telegram's "/" menu.
MENU = [
    ("wishes", "Show what I'm watching"),
    ("wish", "Add a wish, e.g. /wish tue 17:00"),
    ("remove", "Delete a wish: /remove 2"),
    ("now", "Free slots right now"),
    ("status", "Is the bot running?"),
    ("events", "Upcoming watched events"),
    ("watch", "Watch an event name: /watch Afterwork"),
    ("unwatch", "Stop watching an event name"),
    ("pause", "Stop alerts"),
    ("resume", "Start alerts again"),
    ("help", "All commands"),
]


class CommandCenter:
    def __init__(self, watcher, notifier, store: WishStore, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.watcher = watcher
        self.notifier = notifier
        self.store = store
        self.monotonic = monotonic
        self.sleep = sleep
        self.offset: int | None = None
        self.ignored_chats: set[str] = set()

    # --- the commands ----------------------------------------------------

    def wishes_text(self) -> str:
        windows = self.watcher.settings.windows
        if not windows:
            return "No wishes - I'm not watching anything. Add one with /wish, or /reset."
        lines = [f"{i}. {describe(w)}" for i, w in enumerate(windows, 1)]
        paused = "\n\n⏸ Paused - send /resume to get alerts." if self.watcher.paused else ""
        return "I'm watching (indoor doubles):\n" + "\n".join(lines) + paused

    def _set_windows(self, windows) -> None:
        self.store.save(tuple(windows))
        self.watcher.set_windows(tuple(windows))

    def handle(self, text: str) -> str:
        words = text.strip().split(maxsplit=1)
        command = words[0].lower().split("@", 1)[0] if words else ""
        args = words[1] if len(words) > 1 else ""
        windows = list(self.watcher.settings.windows)

        if command in ("/start", "/help"):
            return HELP
        if command == "/wishes":
            return self.wishes_text()
        if command == "/wish":
            if not args:
                return "Example: /wish tue 17:00  or  /wish weekend 09:30-12:30\n(/help for more)"
            if len(windows) >= MAX_WISHES:
                return f"That's {MAX_WISHES} wishes already - /remove one first."
            try:
                window = parse_wish(args)
            except WishError as exc:
                return f"🤔 {exc}"
            if window in windows:
                return "You already have that wish.\n\n" + self.wishes_text()
            self._set_windows(windows + [window])
            return f"✅ Added: {describe(window)}\nI'll check it in the next minute.\n\n" + self.wishes_text()
        if command == "/remove":
            if not args.strip().isdigit() or not 1 <= int(args) <= len(windows):
                return "Which one? e.g. /remove 2\n\n" + self.wishes_text()
            removed = windows.pop(int(args) - 1)
            self._set_windows(windows)
            return f"🗑 Removed: {describe(removed)}\n\n" + self.wishes_text()
        if command == "/reset":
            self.watcher.set_windows(self.store.reset())
            return "↩️ Back to the wishes from config.toml.\n\n" + self.wishes_text()
        if command == "/pause":
            self.watcher.paused = True
            return "⏸ Paused. No checks and no alerts until you send /resume."
        if command == "/resume":
            self.watcher.paused = False
            self.watcher.request_check()
            return "▶️ Resumed. Checking now."
        if command == "/status":
            return self.watcher.status_text()
        if command == "/now":
            return self._now_text()
        if command in ("/events", "/watch", "/unwatch"):
            return self._events_command(command, args.strip())
        return "I only understand commands. Send /help to see them."

    def _events_command(self, command: str, args: str) -> str:
        events = self.watcher.events
        if events is None:
            return "Event watching is off (config.toml: [events] enabled = false)."
        words = events.words.load()
        if command == "/watch":
            if not args:
                return "Which name? e.g. /watch Afterwork"
            if any(w.lower() == args.lower() for w in words):
                return f"Already watching '{args}'."
            events.words.save(words + [args])
            events.restart_watching()
            self.watcher.request_check()
            return f"👀 Watching events named '{args}'. I'll list the dates in a moment."
        if command == "/unwatch":
            if not args.isdigit() or not 1 <= int(args) <= len(words):
                listed = "\n".join(f"{i}. {w}" for i, w in enumerate(words, 1)) or "(none)"
                return f"Which one? e.g. /unwatch 1\n\nWatched names:\n{listed}"
            removed = words.pop(int(args) - 1)
            events.words.save(words)
            events.restart_watching()
            self.watcher.request_check()
            return f"🗑 Stopped watching '{removed}'."
        listed = "\n".join(f"{i}. {w}" for i, w in enumerate(words, 1)) or "(none - add with /watch <name>)"
        if events.last_check is None:
            return f"Watched names:\n{listed}\n\nFirst check is still to come - try again in a minute."
        problem = f"\n\n⚠️ {events.open_play_problem}" if events.open_play_problem else ""
        return (events.overview_text() + f"\n\nWatched names:\n{listed}\n"
                f"Last checked {events.last_check.strftime('%a %H:%M')}" + problem)

    def _now_text(self) -> str:
        w = self.watcher
        slots = w.current_matches()
        if not slots:
            when = w.last_round_at.strftime("%H:%M") if w.last_round_at else "not yet"
            return f"No free slots match your wishes right now (last check: {when})."
        messages = build_messages(slots, w.settings.club_name,
                                  lambda d: booking_link(w.settings.booking_url, w.club, d))
        text = messages[0].replace("new free court slot(s)", "free slot(s) right now", 1)
        return text if len(messages) == 1 else text + "\n\n(more than fit in one message)"

    # --- talking to Telegram ---------------------------------------------

    def register_menu(self) -> None:
        try:
            self.notifier.set_commands(MENU)
        except NotifyError as exc:
            log.warning("Could not set the Telegram command menu: %s", exc)

    def process(self, updates: list[dict]) -> None:
        for update in updates:
            self.offset = int(update.get("update_id", 0)) + 1
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = message.get("text") or ""
            if not text:
                continue
            if chat_id != str(self.notifier.chat_id):
                if chat_id not in self.ignored_chats:
                    log.warning("Ignoring a message from an unknown Telegram chat (%s)", chat_id)
                    self.ignored_chats.add(chat_id)
                continue
            log.info("Command from you: %s", text[:60])
            try:
                reply = self.handle(text)
            except Exception:  # a bug in one command must not stop the watcher
                log.exception("Command failed")
                reply = "Sorry, something went wrong with that command."
            try:
                self.notifier.send(reply)
            except NotifyError as exc:
                log.error("Could not answer the command: %s", exc)

    def listen(self, seconds: float) -> None:
        """Wait up to `seconds`, answering commands meanwhile. Returns early
        when a command asks for a check (e.g. a new wish)."""
        deadline = self.monotonic() + seconds
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0 or self.watcher.check_requested:
                return
            try:
                updates = self.notifier.get_updates(self.offset, timeout=int(min(25, max(1, remaining))))
            except NotifyError as exc:
                log.warning("Telegram: %s", exc)
                self.sleep(min(30, max(0, remaining)))
                continue
            self.process(updates)
