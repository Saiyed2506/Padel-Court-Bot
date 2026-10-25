"""Telegram commands: change wishes, pause, and ask for status from your phone.

Only messages from your own chat (TELEGRAM_CHAT_ID) are obeyed; anyone else
who finds the bot is ignored.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from datetime import datetime
from typing import Callable

from .notify import NotifyError, booking_link, build_messages
from .playtomic import PlaytomicError
from .wishes import MAX_WISHES, WishError, WishStore, describe, parse_wish

log = logging.getLogger("padel_bot")

HELP = """🎾 Padel bot commands  (or tap 🎾 Menu for buttons)

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
/unwatch <number> - stop watching (see /events)
/login - log in to Playtomic for Open Play events
/logout - forget the Playtomic login
/cancel - stop a login in progress"""

# Shown in Telegram's "/" menu.
MENU = [
    ("menu", "Buttons for everything"),
    ("wishes", "Show what I'm watching"),
    ("wish", "Add a wish, e.g. /wish tue 17:00"),
    ("remove", "Delete a wish: /remove 2"),
    ("now", "Free slots right now"),
    ("status", "Is the bot running?"),
    ("events", "Upcoming watched events"),
    ("watch", "Watch an event name: /watch Afterwork"),
    ("unwatch", "Stop watching an event name"),
    ("login", "Log in to Playtomic (Open Play)"),
    ("pause", "Stop alerts"),
    ("resume", "Start alerts again"),
    ("help", "All commands"),
]


class CommandCenter:
    def __init__(self, watcher, notifier, store: WishStore, *, friends=None, runtime=None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        from .menu import Menu

        self.watcher = watcher
        self.notifier = notifier
        self.store = store
        self.friends = friends      # a Friends list, or None
        self.runtime = runtime      # RuntimeSettings for button-changed settings, or None
        self.menu = Menu(self, runtime) if runtime is not None else None
        self._username: str | None = None
        self.monotonic = monotonic
        self.sleep = sleep
        self.offset: int | None = None
        self.ignored_chats: set[str] = set()
        # Telegram login in two steps (email, then password); never stored or logged.
        self.login_step: str | None = None
        self.login_email = ""
        self.login_started = 0.0

    # --- the commands ----------------------------------------------------

    def wishes_text(self) -> str:
        windows = self.watcher.settings.windows
        if not windows:
            return "No wishes - I'm not watching anything. Add one with /wish, or /reset."
        lines = [f"{i}. {describe(w)}" for i, w in enumerate(windows, 1)]
        paused = "\n\n⏸ Paused - send /resume to get alerts." if self.watcher.paused else ""
        return "I'm watching (indoor doubles):\n" + "\n".join(lines) + paused

    def bot_username(self) -> str:
        if self._username is None:
            try:
                self._username = self.notifier.bot_username()
            except (NotifyError, AttributeError):
                return ""
        return self._username

    def set_windows(self, windows) -> None:
        self._set_windows(windows)

    def _set_windows(self, windows) -> None:
        self.store.save(tuple(windows))
        self.watcher.set_windows(tuple(windows))

    def handle(self, text: str) -> str:
        words = text.strip().split(maxsplit=1)
        command = words[0].lower().split("@", 1)[0] if words else ""
        args = words[1] if len(words) > 1 else ""
        windows = list(self.watcher.settings.windows)

        if self.menu is not None and (command in ("/start", "/menu") or text.strip() == "🎾 Menu"):
            self.menu.show()
            return None
        if command in ("/start", "/help"):
            return HELP
        if command in ("/login", "/logout"):
            return self._login_command(command)
        if command == "/cancel":
            was = self.login_step
            self.login_step, self.login_email = None, ""
            return "Login cancelled." if was else "Nothing to cancel."
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
            if self.runtime is not None:
                self.runtime.update(paused=True)
            return "⏸ Paused. No checks and no alerts until you send /resume."
        if command == "/resume":
            self.watcher.paused = False
            if self.runtime is not None:
                self.runtime.update(paused=False)
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
                                  lambda d: booking_link(w.settings.booking_url, w.club, d),
                                  w.clock(), w.settings.free_cancel_min)
        text = messages[0].replace("new free court slot(s)", "free slot(s) right now", 1)
        return text if len(messages) == 1 else text + "\n\n(more than fit in one message)"

    # --- talking to Telegram ---------------------------------------------

    def register_menu(self) -> None:
        try:
            self.notifier.set_commands(MENU)
        except NotifyError as exc:
            log.warning("Could not set the Telegram command menu: %s", exc)

    # --- Playtomic login via chat ------------------------------------------

    LOGIN_TIMEOUT_S = 300

    def _account(self):
        events = self.watcher.events
        return events.account if events is not None else None

    def _login_command(self, command: str) -> str:
        account = self._account()
        if account is None:
            return "Event watching is off, so there's nothing to log in for."
        if command == "/logout":
            account.logout()
            self.login_step = None
            return "Logged out: the Playtomic login token was deleted. Open Play events are no longer read."
        self.login_step, self.login_email, self.login_started = "email", "", self.monotonic()
        return ("🔐 Playtomic login for Open Play events (read-only, the bot never books or pays).\n\n"
                "Heads-up: what you type here passes through Telegram's servers. I delete your password "
                "message right after reading it and never save the password - only Playtomic's login token.\n\n"
                "Step 1/2: send your Playtomic email.  (/cancel to stop)")

    def _login_input(self, text: str, message_id) -> str:
        if self.monotonic() - self.login_started > self.LOGIN_TIMEOUT_S:
            self.login_step = None
            self._delete(message_id)  # might be a password; don't leave it lying around
            return "The login timed out. Send /login to start again."
        if self.login_step == "email":
            if "@" not in text or " " in text.strip():
                return "That doesn't look like an email address. Send your Playtomic email, or /cancel."
            self.login_email, self.login_step = text.strip(), "password"
            return "Step 2/2: send your Playtomic password. I'll delete the message right away."
        # password step
        deleted = self._delete(message_id)
        email, self.login_step, self.login_email = self.login_email, None, ""
        note = "" if deleted else "\n\n⚠️ I couldn't delete your password message - please delete it yourself (long-press → Delete)."
        try:
            self._account().login(email, text)
        except PlaytomicError as exc:
            return f"❌ Login failed: {exc}\nSend /login to try again.{note}"
        finally:
            text = ""  # drop our reference to the password
        events = self.watcher.events
        events.open_play_problem = None
        events.last_check = None
        self.watcher.request_check()
        return f"✅ Logged in to Playtomic. Password not saved. Checking events now.{note}"

    def _delete(self, message_id) -> bool:
        if message_id is None:
            return False
        try:
            self.notifier.delete_message(message_id)
            return True
        except (NotifyError, AttributeError) as exc:
            log.warning("Could not delete a message: %s", exc)
            return False

    def _button(self, query: dict) -> None:
        """A tapped menu button. Only taps from your own chat count."""
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        data = str(query.get("data", ""))
        toast = ""
        if data.startswith("p:") and self.watcher.polls is not None:
            # "who's in?" votes: you and approved friends
            _, choice, pid = (data.split(":") + ["", ""])[:3]
            try:
                toast = self.watcher.polls.vote(pid, chat_id, choice)
            except Exception:
                log.exception("Vote failed")
                toast = "Sorry, something went wrong."
        elif data.startswith("bk:"):
            toast = self._booked(chat_id, data)
        elif chat_id != str(self.notifier.chat_id) or self.menu is None:
            toast = "Not allowed"
        else:
            try:
                toast = self.menu.on_callback(str(query.get("data", "")), message.get("message_id"))
            except Exception:
                log.exception("Button failed")
                toast = "Sorry, something went wrong."
        try:
            self.notifier.answer_callback(query.get("id", ""), toast)
        except (NotifyError, AttributeError) as exc:
            log.warning("Could not answer a button: %s", exc)

    def _booked(self, chat_id: str, data: str) -> str:
        """'📌 I booked it' from you or an approved friend: remember it for a reminder."""
        reminders = self.watcher.reminders
        if reminders is None:
            return "Reminders are off"
        if chat_id != str(self.notifier.chat_id) and not (self.friends and chat_id in self.friends.approved):
            return "Not allowed"
        _, kind, ref = (data.split(":", 2) + ["", ""])[:3]
        from .timeutil import format_day, format_time
        if kind == "c" and self.watcher.polls is not None and ref in self.watcher.polls.polls:
            poll = self.watcher.polls.polls[ref]
            start, label = datetime.fromisoformat(poll["start"]), poll.get("label", "a court")
        elif kind == "e" and self.watcher.events is not None and ref in self.watcher.events.state["events"]:
            entry = self.watcher.events.state["events"][ref]
            if not entry.get("start"):
                return "This event has no date"
            start = datetime.fromisoformat(entry["start"])
            label = f"{entry.get('name', 'an event')}, {format_day(start)} {format_time(start)}"
        else:
            return "This alert is too old"
        return reminders.add(chat_id, f"{kind}:{ref}", label, start, self.watcher.clock())

    def _stranger(self, chat_id: str, message: dict, text: str) -> None:
        """A message from someone other than you: a friend, or someone asking to join."""
        sender = message.get("from") or {}
        name = " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p) or "Someone"
        if sender.get("username"):
            name += f" (@{sender['username']})"
        friends = self.friends
        status = friends.status(chat_id) if friends else "blocked"
        reply = None
        if status == "approved":
            if text.strip().lower().startswith("/stop"):
                friends.remove(chat_id)
                reply = "You won't get padel alerts anymore. Send /start to ask again."
                self._safe_send(f"👥 {name} left the alerts.")
            else:
                reply = "👋 You get the padel court and event alerts from this bot. Send /stop to leave."
        elif status == "pending":
            reply = "⏳ Still waiting for approval."
        elif status == "new":
            if friends.request(chat_id, name):
                reply = "👋 Hi! I've asked the owner of this bot to let you receive the padel alerts."
                buttons = [[(f"✅ Approve", f"fa:{chat_id}"), ("❌ Decline", f"fd:{chat_id}")]]
                try:
                    self.notifier.send(f"👥 {name} wants to get your padel alerts.", buttons=buttons)
                except NotifyError as exc:
                    log.error("Could not ask you about a friend request: %s", exc)
        else:  # blocked, or friends feature off
            if chat_id not in self.ignored_chats:
                log.warning("Ignoring a message from an unknown Telegram chat (%s)", chat_id)
                self.ignored_chats.add(chat_id)
        if reply:
            try:
                self.notifier.send(reply, chat_id=chat_id)
            except NotifyError as exc:
                log.warning("Could not answer %s: %s", name, exc)

    def _safe_send(self, text: str) -> None:
        try:
            self.notifier.send(text)
        except NotifyError as exc:
            log.error("%s", exc)

    def process(self, updates: list[dict]) -> None:
        for update in updates:
            self.offset = int(update.get("update_id", 0)) + 1
            if update.get("callback_query"):
                self._button(update["callback_query"])
                continue
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = message.get("text") or ""
            if not text:
                continue
            if chat_id != str(self.notifier.chat_id):
                self._stranger(chat_id, message, text)
                continue
            in_login = self.login_step is not None and not text.startswith("/")
            log.info("Command from you: %s", "(login details - hidden)" if in_login else text[:60])
            try:
                if in_login:
                    reply = self._login_input(text, message.get("message_id"))
                else:
                    reply = None
                    if self.menu is not None and not text.startswith("/"):
                        reply = self.menu.on_text(text)
                    if reply is None:
                        reply = self.handle(text)
            except Exception:  # a bug in one command must not stop the watcher
                log.exception("Command failed")
                reply = "Sorry, something went wrong with that command."
            if reply is None:
                continue
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
