"""The always-on part: listens on Telegram, answers with the brain, fires reminders."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, tzinfo

from padel_bot.notify import TELEGRAM_LIMIT, NotifyError, TelegramNotifier

from .brain import Brain, BudgetReached, explain_error
from .store import Store
from .tools import describe_reminder

log = logging.getLogger("jarvis")

POLL_SECONDS = 20          # how long one Telegram check waits for a new message
IGNORE_OLDER_THAN = timedelta(minutes=30)  # messages sent while Jarvis was off for longer

COMMANDS = [
    ("help", "What I can do"),
    ("reminders", "Upcoming reminders"),
    ("memories", "What I remember about you"),
    ("cost", "Estimated AI spending"),
    ("new", "Start a fresh conversation"),
]

HELP = """I'm Jarvis. Just write to me like to a person, for example:
- Remind me to call mum tomorrow at 18:00
- Every weekday at 7:30 remind me to take my vitamins
- Remember that my padel partner is Omar
- What's the weather this weekend?

Shortcuts (free, no AI used):
/reminders - upcoming reminders
/memories - what I remember about you
/cost - estimated AI spending
/new - start a fresh conversation (I keep my memories)

Coming in later stages: voice, daily briefing, email and calendar, padel bot control."""


class JarvisTelegram(TelegramNotifier):
    def typing(self) -> None:
        """Shows 'typing...' in the chat for about 5 seconds."""
        self._call("sendChatAction", {"chat_id": self.chat_id, "action": "typing"})

    def send_long(self, text: str) -> None:
        """Sends text of any length, split at line breaks to fit Telegram's limit."""
        chunk = ""
        for line in text.split("\n"):
            while len(line) > TELEGRAM_LIMIT:
                if chunk:
                    self.send(chunk)
                    chunk = ""
                self.send(line[:TELEGRAM_LIMIT])
                line = line[TELEGRAM_LIMIT:]
            if len(chunk) + len(line) + 1 > TELEGRAM_LIMIT:
                self.send(chunk)
                chunk = ""
            chunk = f"{chunk}\n{line}" if chunk else line
        if chunk.strip():
            self.send(chunk)


class KeepTyping:
    """Keeps 'typing...' visible while Claude thinks (that can take a while)."""

    def __init__(self, telegram: JarvisTelegram):
        self.telegram = telegram
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                self.telegram.typing()
            except NotifyError:
                pass
            self.stop.wait(4.5)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()


class Jarvis:
    def __init__(self, telegram: JarvisTelegram, brain: Brain, store: Store, tz: tzinfo, clock=None):
        self.telegram = telegram
        self.brain = brain
        self.store = store
        self.tz = tz
        self.clock = clock or (lambda: datetime.now(tz))
        self.offset: int | None = store.load_offset()
        self.warned_strangers: set[str] = set()

    # ---- reminders --------------------------------------------------------
    def fire_reminders(self) -> None:
        now = self.clock()
        for r in self.store.pop_due_reminders(now):
            late = now - r.due
            when = "" if late < timedelta(minutes=10) else f" (due {r.due.astimezone(self.tz):%a %H:%M} - the PC was off or busy)"
            self.telegram.send(f"⏰ {r.text}{when}")
            self.store.add_note(f"At {now:%H:%M} you delivered reminder #{r.id}: '{r.text}'.")
            log.info("Reminder #%s sent", r.id)

    # ---- messages ---------------------------------------------------------
    def handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if not chat_id:
            return
        if chat_id != str(self.telegram.chat_id):
            # Anyone can find a Telegram bot. Jarvis only ever works for you.
            if chat_id not in self.warned_strangers:
                self.warned_strangers.add(chat_id)
                log.warning("Ignored a message from a stranger (chat %s)", chat_id)
            return
        sent = datetime.fromtimestamp(message.get("date", 0), self.tz)
        if self.clock() - sent > IGNORE_OLDER_THAN:
            log.info("Skipped an old message from %s", sent)
            return
        text = (message.get("text") or "").strip()
        if not text:
            self.telegram.send("I can only read text for now. Voice messages come in a later stage.")
            return
        reply = self.command(text) if text.startswith("/") else None
        if reply is None:
            reply = self.think(text)
        self.telegram.send_long(reply)

    def think(self, text: str) -> str:
        try:
            with KeepTyping(self.telegram):
                return self.brain.ask(text, self.clock()).text
        except BudgetReached as exc:
            return (f"Today's AI budget (${exc.args[0]:.2f}) is used up, so I'm resting until midnight. "
                    "Reminders still work. To change the limit, edit daily_budget_usd in jarvis.toml.")
        except Exception as exc:  # report every failure to you, keep running
            log.exception("Thinking failed")
            return explain_error(exc)

    def command(self, text: str) -> str | None:
        """Free shortcuts. Returns None for anything that isn't one."""
        name = text.split()[0][1:].split("@")[0].lower()
        now = self.clock()
        if name in ("start", "help"):
            return HELP
        if name == "reminders":
            items = self.store.reminders()
            return "\n".join(describe_reminder(r, self.tz) for r in items) if items else "No reminders set."
        if name == "memories":
            items = self.store.memories()
            return "\n".join(f"#{m['id']}: {m['fact']}" for m in items) if items else "I haven't saved anything yet."
        if name == "cost":
            month_start = now.date().replace(day=1)
            return (f"Estimated AI spending:\n- today: ${self.store.spent(now.date()):.2f} "
                    f"(daily limit ${self.brain.settings.daily_budget_usd:.2f})\n"
                    f"- this month: ${self.store.spent_since(month_start):.2f}\n"
                    "Estimates only - the real bill is at console.anthropic.com.")
        if name == "new":
            self.brain.start_new_conversation(now, keep_recap=False)
            return "Fresh start. I still remember everything I saved about you."
        return None

    # ---- main loop ----------------------------------------------------------
    def run_forever(self) -> None:
        self.telegram.set_commands(COMMANDS)
        self.telegram.send("Jarvis is online.", silent=True)
        log.info("Jarvis is running. Close this window to stop.")
        failures = 0
        while True:
            try:
                self.fire_reminders()
                updates = self.telegram.get_updates(self.offset, timeout=POLL_SECONDS)
                failures = 0
            except NotifyError as exc:
                failures += 1
                wait = min(300, 10 * 2 ** min(failures, 5))
                log.warning("Telegram problem (%s). Trying again in %s s.", exc, wait)
                time.sleep(wait)
                continue
            for update in updates:
                self.offset = update["update_id"] + 1
                self.store.save_offset(self.offset)  # never answer the same message twice
                try:
                    self.handle_update(update)
                except NotifyError as exc:
                    log.warning("Could not send the answer: %s", exc)
                except Exception:
                    log.exception("Unexpected problem with a message; carrying on")
