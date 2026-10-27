"""Button menu in Telegram: tap instead of typing commands.

Every screen is one message that gets edited in place as you tap. Button
presses ("callback queries") are only accepted from your own chat.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Window
from .notify import NotifyError
from .timeutil import DAY_KEYS, format_day, parse_hhmm
from .wishes import MAX_WISHES, describe, drop_past, hhmm

log = logging.getLogger("padel_bot")

MENU_BUTTON = "🎾 Menu"
_DAY_LABELS = dict(zip(DAY_KEYS, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]))
_QUIET_CHOICES = [("23:00", "07:00"), ("00:00", "07:00"), ("01:00", "06:00"), None]

MAIN = [
    [("➕ Court wish", "cw"), ("📅 One-time wish", "ow")],
    [("🎫 Event", "ev"), ("📋 My wishes", "mw")],
    [("👥 Friends", "fr"), ("⚙️ Settings", "st")],
    [("📖 Guide", "g")],
]
BACK = [("⬅️ Menu", "m")]

GUIDE = """📖 How the padel bot works

🎾 Courts: I check the club every few minutes for free indoor courts that match your wishes and message you with a booking link. ⭐ = your favourite times.

➕ Court wish: every week, e.g. "Mon-Fri, start 16:00-19:00, 90 min".
📅 One-time wish: one date only, e.g. "Tue 4 Nov, 17:00". It disappears after that day.
🎫 Event: watch events by name (Newcomer, Afterwork): new dates, free spots, Sunday overview.
👥 Friends: friends open this bot and tap Start; you approve them and they get the same alerts.
⚙️ Settings: pause, quiet hours, only ⭐ times, Playtomic login.

You can also type commands - send /help for the list."""


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:4]


class RuntimeSettings:
    """Settings you change with buttons, kept across restarts (bot_settings.json)."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def update(self, **changes) -> None:
        data = {**self.load(), **changes}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def apply(self, watcher) -> None:
        data = self.load()
        if "paused" in data:
            watcher.paused = bool(data["paused"])
        changes = {}
        if "only_preferred" in data:
            changes["only_preferred"] = bool(data["only_preferred"])
        if "quiet_hours" in data:
            q = data["quiet_hours"]
            changes["quiet_hours"] = (parse_hhmm(q[0]), parse_hhmm(q[1])) if q else None
        if "min_notice_hours" in data:
            changes["min_notice_min"] = int(data["min_notice_hours"]) * 60
        if "daily_report" in data:
            changes["daily_report_min"] = parse_hhmm(data["daily_report"]) if data["daily_report"] else None
        if changes:
            watcher.settings = replace(watcher.settings, **changes)
        if getattr(watcher, "sniper", None) is not None:
            watcher.sniper.enabled = bool(data.get("snipe", False))
        today = watcher.clock().date()
        watcher.muted_days = {d for d in (date.fromisoformat(x) for x in data.get("muted_days", [])) if d >= today}


class Menu:
    def __init__(self, center, runtime: RuntimeSettings):
        self.center = center            # the CommandCenter (watcher, store, notifier, login)
        self.runtime = runtime
        self.flow: dict = {}            # the wish being built, step by step
        self.awaiting: str | None = None  # "event_name" while waiting for you to type a name

    @property
    def watcher(self):
        return self.center.watcher

    @property
    def notifier(self):
        return self.center.notifier

    # --- showing screens ----------------------------------------------------

    def show(self) -> None:
        """Send a fresh main menu (and the persistent 🎾 Menu button)."""
        self.notifier.send("Choose an option below 👇 (the 🎾 Menu button brings this back)",
                           keyboard=[[MENU_BUTTON]])
        self.notifier.send(self._main_text(), buttons=MAIN)

    def _main_text(self) -> str:
        w = self.watcher
        state = "⏸ paused" if w.paused else "▶️ watching"
        events = w.events.words.load() if w.events else []
        return (f"🎾 Padel bot - {state}\n"
                f"{len(w.settings.windows)} court wish(es), {len(events)} event name(s), "
                f"{len(self.center.friends.approved) if self.center.friends else 0} friend(s)")

    def _screen(self, message_id, text: str, buttons) -> None:
        try:
            self.notifier.edit(message_id, text, buttons=buttons)
        except NotifyError:
            self.notifier.send(text, buttons=buttons)  # too old to edit: send a new one

    # --- button presses -------------------------------------------------------

    def on_callback(self, data: str, message_id) -> str:
        """Handle one tap. Returns a short toast text (may be empty)."""
        key, _, arg = data.partition(":")
        handler = {
            "m": self._go_main, "g": self._go_guide, "x": self._cancel, "bn": self._booking_no,
            "cw": self._start_recurring, "ow": self._start_once,
            "d": self._toggle_day, "dt": self._pick_date, "s": self._pick_start,
            "l": self._pick_last, "u": self._pick_length, "ok": self._confirm,
            "mw": self._go_wishes, "dw": self._delete_wish, "de": self._delete_word,
            "ev": self._go_events, "ea": self._add_word,
            "fr": self._go_friends, "fa": self._friend_approve, "fd": self._friend_decline,
            "fx": self._friend_remove,
            "st": self._go_settings, "sp": self._toggle_pause, "so": self._toggle_only_preferred,
            "sq": self._cycle_quiet, "sl": self._login, "slo": self._logout,
            "sb": self._cycle_buffer, "sr": self._toggle_report,
            "md": self._mute_day, "um": self._unmute_day, "rb": self._remove_booking,
            "so2": self._toggle_snipe, "bt": self._test_booking, "bb": self._ask_booking, "by": self._do_booking, "sa": self._toggle_auto,
        }.get(key)
        if handler is None:
            return "This button is from an older version - tap 🎾 Menu."
        return handler(arg, message_id) or ""

    def _go_main(self, arg, mid):
        self.flow, self.awaiting = {}, None
        self._screen(mid, self._main_text(), MAIN)

    def _go_guide(self, arg, mid):
        self._screen(mid, GUIDE, [BACK])

    def _booking_no(self, arg, mid):
        self._screen(mid, "Not booked.", [])
        return "OK"

    def _cancel(self, arg, mid):
        self._go_main(arg, mid)
        return "Cancelled"

    # --- building a court wish ---------------------------------------------------

    def _start_recurring(self, arg, mid):
        if len(self.watcher.settings.windows) >= MAX_WISHES:
            return f"That's {MAX_WISHES} wishes already - delete one in 📋 My wishes."
        self.flow = {"kind": "recurring", "days": []}
        self._days_screen(mid)

    def _days_screen(self, mid):
        chosen = set(self.flow["days"])
        row = lambda keys: [(("✅ " if k in chosen else "") + _DAY_LABELS[k], f"d:{k}") for k in keys]
        buttons = [row(DAY_KEYS[:4]), row(DAY_KEYS[4:]),
                   [("Mon-Fri", "d:wk"), ("Weekend", "d:we")],
                   [("Next ➡️", "d:ok"), ("✖ Cancel", "x")]]
        picked = ", ".join(_DAY_LABELS[k] for k in DAY_KEYS if k in chosen) or "none yet"
        self._screen(mid, f"➕ Court wish (every week)\nStep 1/4: which days? Tap to select.\nSelected: {picked}", buttons)

    def _toggle_day(self, arg, mid):
        days = set(self.flow.get("days", []))
        if arg == "ok":
            if not days:
                return "Pick at least one day"
            self._start_screen(mid)
            return None
        if arg == "wk":
            days = set(DAY_KEYS[:5])
        elif arg == "we":
            days = set(DAY_KEYS[5:])
        elif arg in DAY_KEYS:
            days ^= {arg}
        self.flow["days"] = sorted(days, key=DAY_KEYS.index)
        self._days_screen(mid)

    def _start_once(self, arg, mid):
        if len(self.watcher.settings.windows) >= MAX_WISHES:
            return f"That's {MAX_WISHES} wishes already - delete one in 📋 My wishes."
        self.flow = {"kind": "once"}
        today = self.watcher.clock().date()
        days = [today + timedelta(days=i) for i in range(self.watcher.settings.days_ahead + 1)]
        label = lambda d: "Today" if d == today else format_day(datetime(d.year, d.month, d.day))
        buttons = [[(label(d), f"dt:{d.isoformat()}") for d in days[i:i + 3]] for i in range(0, len(days), 3)]
        buttons.append([("✖ Cancel", "x")])
        self._screen(mid, "📅 One-time wish\nStep 1/4: which date?", buttons)

    def _pick_date(self, arg, mid):
        try:
            self.flow["date"] = date.fromisoformat(arg).isoformat()
        except ValueError:
            return "Unknown date"
        self._start_screen(mid)

    def _flow_title(self) -> str:
        if self.flow.get("kind") == "once":
            d = date.fromisoformat(self.flow["date"])
            return f"📅 One-time wish: {format_day(datetime(d.year, d.month, d.day))}"
        return "➕ Court wish: " + ", ".join(_DAY_LABELS[k] for k in self.flow.get("days", []))

    def _start_screen(self, mid):
        times = list(range(7 * 60, 22 * 60, 30))
        buttons = [[(hhmm(t), f"s:{t}") for t in times[i:i + 5]] for i in range(0, len(times), 5)]
        buttons.append([("✖ Cancel", "x")])
        self._screen(mid, f"{self._flow_title()}\nStep 2/4: earliest start time?", buttons)

    def _pick_start(self, arg, mid):
        if not arg.isdigit():
            return "Unknown time"
        start = int(arg)
        self.flow["start"] = start
        later = [t for t in range(start + 30, min(start + 5 * 60, 22 * 60) + 1, 30)]
        buttons = [[(f"Only {hhmm(start)}", f"l:{start}")]]
        buttons += [[(hhmm(t), f"l:{t}") for t in later[i:i + 5]] for i in range(0, len(later), 5)]
        buttons.append([("✖ Cancel", "x")])
        self._screen(mid, f"{self._flow_title()}, from {hhmm(start)}\nStep 3/4: latest start time?", buttons)

    def _pick_last(self, arg, mid):
        if not arg.isdigit() or "start" not in self.flow:
            return "Start again from 🎾 Menu"
        self.flow["last"] = max(int(arg), self.flow["start"])
        buttons = [[("60 min", "u:60"), ("90 min", "u:90"), ("120 min", "u:120")], [("✖ Cancel", "x")]]
        self._screen(mid, f"{self._flow_title()}, start {self._starts_text()}\nStep 4/4: how long do you play?", buttons)

    def _starts_text(self) -> str:
        start, last = self.flow["start"], self.flow.get("last", self.flow["start"])
        return hhmm(start) if last == start else f"{hhmm(start)}-{hhmm(last)}"

    def _build_window(self) -> Window:
        f = self.flow
        duration = int(f["duration"])
        end = f["last"] + duration
        if end > 24 * 60:
            raise ValueError("the game would end after midnight")
        exact = f["last"] == f["start"]
        return Window(days=frozenset(f.get("days") or DAY_KEYS), start_min=f["start"], end_min=end,
                      durations=frozenset({duration}),
                      preferred_starts=frozenset({f["start"]}) if exact else frozenset(),
                      on_date=date.fromisoformat(f["date"]) if f.get("kind") == "once" else None)

    def _pick_length(self, arg, mid):
        if arg not in ("60", "90", "120") or "last" not in self.flow:
            return "Start again from 🎾 Menu"
        self.flow["duration"] = int(arg)
        try:
            window = self._build_window()
        except ValueError as exc:
            return str(exc)
        buttons = [[("✅ Add this wish", "ok"), ("✖ Cancel", "x")]]
        self._screen(mid, f"Add this wish?\n\n{describe(window)}", buttons)

    def _confirm(self, arg, mid):
        try:
            window = self._build_window()
        except (KeyError, ValueError):
            return "Start again from 🎾 Menu"
        windows = list(self.watcher.settings.windows)
        if window not in windows:
            self.center.set_windows(windows + [window])
        self.flow = {}
        self._screen(mid, f"✅ Added: {describe(window)}\nI'll check it in a moment.\n\n{self._wishes_text()}",
                     self._wish_buttons())
        return "Added"

    # --- my wishes --------------------------------------------------------------

    def _wishes_text(self) -> str:
        windows = drop_past(self.watcher.settings.windows, self.watcher.clock().date())
        if tuple(windows) != tuple(self.watcher.settings.windows):
            self.center.set_windows(windows)
        lines = [f"{i}. {describe(w)}" for i, w in enumerate(windows, 1)] or ["(no court wishes)"]
        words = self.watcher.events.words.load() if self.watcher.events else []
        events = [f"E{i}. {w}" for i, w in enumerate(words, 1)] or ["(no event names)"]
        muted = ", ".join(format_day(datetime(d.year, d.month, d.day)) for d in sorted(self.watcher.muted_days))
        booked = [f"📌 {i['label']}" + (" (reminder sent)" if i["sent"] else "") for i in self._bookings()]
        return ("📋 Court wishes:\n" + "\n".join(lines) + "\n\n🎫 Events:\n" + "\n".join(events)
                + (f"\n\n🔕 Muted days: {muted}" if muted else "")
                + ("\n\n📌 Booked (reminder 48h before):\n" + "\n".join(booked) if booked else ""))

    def _bookings(self) -> list[dict]:
        reminders = self.watcher.reminders
        return reminders.for_chat(str(self.notifier.chat_id)) if reminders is not None else []

    def _remove_booking(self, arg, mid):
        index, _, check = arg.partition(":")
        mine = self._bookings()
        if not index.isdigit() or int(index) >= len(mine) or _short_hash(mine[int(index)]["key"]) != check:
            self._go_wishes("", mid)
            return "That list was outdated - here's the current one"
        removed = self.watcher.reminders.remove(str(self.notifier.chat_id), int(index))
        self._go_wishes("", mid)
        return f"Removed {removed['label'][:100]}" if removed else "Already removed"

    def _wish_buttons(self):
        rows = []
        for i, w in enumerate(self.watcher.settings.windows):
            rows.append([(f"🗑 {i + 1}. {describe(w)[:40]}", f"dw:{i}:{_short_hash(describe(w))}")])
        words = self.watcher.events.words.load() if self.watcher.events else []
        for i, w in enumerate(words):
            rows.append([(f"🗑 E{i + 1}. {w[:40]}", f"de:{i}:{_short_hash(w)}")])
        for day in sorted(self.watcher.muted_days):
            label = format_day(datetime(day.year, day.month, day.day))
            rows.append([(f"🔔 Unmute {label}", f"um:{day.isoformat()}")])
        for i, item in enumerate(self._bookings()):
            rows.append([(f"🗑 📌 {item['label'][:40]}", f"rb:{i}:{_short_hash(item['key'])}")])
        rows.append([("➕ Court wish", "cw"), ("📅 One-time", "ow")])
        rows.append(BACK)
        return rows

    def _go_wishes(self, arg, mid):
        self._screen(mid, self._wishes_text(), self._wish_buttons())

    def _delete_wish(self, arg, mid):
        index, _, check = arg.partition(":")
        windows = list(self.watcher.settings.windows)
        if not index.isdigit() or int(index) >= len(windows) or _short_hash(describe(windows[int(index)])) != check:
            self._go_wishes("", mid)
            return "That list was outdated - here's the current one"
        removed = windows.pop(int(index))
        self.center.set_windows(windows)
        self._go_wishes("", mid)
        return f"Deleted: {describe(removed)[:150]}"

    def _delete_word(self, arg, mid):
        events = self.watcher.events
        if events is None:
            return "Event watching is off"
        index, _, check = arg.partition(":")
        words = events.words.load()
        if not index.isdigit() or int(index) >= len(words) or _short_hash(words[int(index)]) != check:
            self._go_wishes("", mid)
            return "That list was outdated - here's the current one"
        removed = words.pop(int(index))
        events.words.save(words)
        events.restart_watching()
        self.watcher.request_check()
        self._go_wishes("", mid)
        return f"Stopped watching '{removed}'"

    # --- events -------------------------------------------------------------------

    def _go_events(self, arg, mid):
        events = self.watcher.events
        if events is None:
            self._screen(mid, "Event watching is off (config.toml: [events] enabled = false).", [BACK])
            return
        words = events.words.load()
        if events.last_check is None:
            body = "First event check is still to come."
        else:
            body = events.overview_text()
        if events.open_play_problem:
            body += f"\n\n⚠️ {events.open_play_problem}"
        rows = [[("➕ Watch a name", "ea")]]
        rows += [[(f"🗑 {w[:40]}", f"de:{i}:{_short_hash(w)}")] for i, w in enumerate(words)]
        rows.append(BACK)
        self._screen(mid, f"🎫 Watched names: {', '.join(words) or 'none'}\n\n{body}", rows)

    def _add_word(self, arg, mid):
        self.awaiting = "event_name"
        self._screen(mid, "🎫 Type part of the event name to watch (e.g. Afterwork) and send it.",
                     [[("✖ Cancel", "x")]])

    def on_text(self, text: str) -> str | None:
        """Typed text while a screen is waiting for it. None = not for the menu."""
        if self.awaiting != "event_name":
            return None
        self.awaiting = None
        name = text.strip()[:60]
        if not name:
            return "Nothing typed - cancelled."
        return self.center.handle(f"/watch {name}")

    # --- friends -----------------------------------------------------------------

    def _go_friends(self, arg, mid):
        friends = self.center.friends
        username = self.center.bot_username()
        invite = f"https://t.me/{username}" if username else "your bot's link"
        lines = [f"👥 Friends get the same court and event alerts as you, but can't change anything.\n\n"
                 f"Invite: send them {invite} - they tap Start, then you approve here."]
        rows = []
        if friends.pending:
            lines.append("\nWaiting for approval:")
            for chat_id, name in friends.pending.items():
                lines.append(f"• {name}")
                rows.append([(f"✅ {name[:25]}", f"fa:{chat_id}"), (f"❌ {name[:25]}", f"fd:{chat_id}")])
        lines.append("\nApproved:" if friends.approved else "\nNo friends approved yet.")
        for chat_id, name in friends.approved.items():
            lines.append(f"• {name}")
            rows.append([(f"🗑 Remove {name[:30]}", f"fx:{chat_id}")])
        rows.append(BACK)
        self._screen(mid, "\n".join(lines), rows)

    def _friend_approve(self, arg, mid):
        name = self.center.friends.approve(arg)
        if name is None:
            return "Already handled"
        self._tell(arg, "✅ You're in! You'll now get the padel alerts. Send /stop to leave.")
        self._go_friends("", mid)
        return f"Approved {name}"

    def _friend_decline(self, arg, mid):
        name = self.center.friends.decline(arg)
        self._go_friends("", mid)
        return f"Declined {name}" if name else "Already handled"

    def _friend_remove(self, arg, mid):
        name = self.center.friends.remove(arg)
        if name:
            self._tell(arg, "You've been removed from the padel alerts.")
        self._go_friends("", mid)
        return f"Removed {name}" if name else "Already removed"

    def _tell(self, chat_id: str, text: str) -> None:
        try:
            self.notifier.send(text, chat_id=chat_id)
        except NotifyError as exc:
            log.warning("Could not message a friend: %s", exc)

    # --- settings ----------------------------------------------------------------

    def _settings_buttons(self):
        w = self.watcher
        quiet = w.settings.quiet_hours
        quiet_text = f"{hhmm(quiet[0])}-{hhmm(quiet[1])}" if quiet else "off"
        account = w.events.account if w.events else None
        if w.bookings is not None:
            label = {"verified": "ON" if w.bookings.auto_on else "off",
                     "tested": "🔒 book once with a tap first", "new": "🔒 test + book once first"}[w.bookings.stage]
            rows_booking = [[(f"🤖 Automatic booking: {label}", "sa")]]
            if w.sniper is not None:
                snipe = ("ON" if w.sniper.enabled else "off") if w.bookings.auto_on else "🔒 needs 🤖 automatic"
                rows_booking.append([(f"⚡ Book at opening (beat others): {snipe}", "so2")])
        else:
            rows_booking = []
        notice = w.settings.min_notice_min // 60
        report = w.settings.daily_report_min
        rows = [
            [("▶️ Resume alerts" if w.paused else "⏸ Pause alerts", "sp")],
            [(f"⭐ Only favourite times: {'ON' if w.settings.only_preferred else 'off'}", "so")],
            [(f"⏱ Only games ≥{notice}h ahead" if notice else "⏱ All games, also last-minute", "sb")],
            [(f"🌙 Night pause: {quiet_text}", "sq")],
            [(f"☀️ Daily check-in: {hhmm(report) if report is not None else 'off'}", "sr")],
        ] + rows_booking
        if account is not None:
            rows.append([("🔐 Playtomic: log out", "slo")] if account.logged_in
                        else [("🔐 Playtomic: log in", "sl")])
        rows.append(BACK)
        return rows

    def _go_settings(self, arg, mid):
        self._screen(mid, "⚙️ Settings - tap to change", self._settings_buttons())

    def _toggle_pause(self, arg, mid):
        self.center.handle("/resume" if self.watcher.paused else "/pause")
        self._go_settings("", mid)
        return "Paused" if self.watcher.paused else "Resumed"

    def _toggle_only_preferred(self, arg, mid):
        value = not self.watcher.settings.only_preferred
        self.watcher.settings = replace(self.watcher.settings, only_preferred=value)
        self.runtime.update(only_preferred=value)
        self._go_settings("", mid)
        return "Only ⭐ times" if value else "All matching times"

    def _cycle_quiet(self, arg, mid):
        current = self.watcher.settings.quiet_hours
        as_text = (hhmm(current[0]), hhmm(current[1])) if current else None
        index = _QUIET_CHOICES.index(as_text) if as_text in _QUIET_CHOICES else -1
        choice = _QUIET_CHOICES[(index + 1) % len(_QUIET_CHOICES)]
        self.watcher.settings = replace(self.watcher.settings,
                                        quiet_hours=(parse_hhmm(choice[0]), parse_hhmm(choice[1])) if choice else None)
        self.runtime.update(quiet_hours=list(choice) if choice else None)
        self.watcher.request_check()
        self._go_settings("", mid)
        return f"Night pause {choice[0]}-{choice[1]}" if choice else "Night pause off - checks around the clock"

    def _cycle_buffer(self, arg, mid):
        choices = [0, 24, 48]
        current = self.watcher.settings.min_notice_min // 60
        hours = choices[(choices.index(current) + 1) % len(choices)] if current in choices else 48
        self.watcher.settings = replace(self.watcher.settings, min_notice_min=hours * 60)
        self.runtime.update(min_notice_hours=hours)
        self.watcher.set_windows(self.watcher.settings.windows)  # re-check all days with the new rule
        self._go_settings("", mid)
        return (f"Alerts only for games ≥{hours}h ahead" if hours
                else "All games, also last-minute (marked ⚠️ when under 24h)")

    def _toggle_report(self, arg, mid):
        on = self.watcher.settings.daily_report_min is None
        self.watcher.settings = replace(self.watcher.settings, daily_report_min=8 * 60 if on else None)
        self.runtime.update(daily_report="08:00" if on else None)
        self._go_settings("", mid)
        return "Daily check-in at 08:00" if on else "Daily check-in off"

    def _mute_day(self, arg, mid):
        try:
            day = date.fromisoformat(arg)
        except ValueError:
            return "Unknown day"
        self.watcher.muted_days.add(day)
        self.runtime.update(muted_days=sorted(d.isoformat() for d in self.watcher.muted_days))
        return f"🔕 No more court alerts for {format_day(datetime(day.year, day.month, day.day))} (undo in 📋 My wishes)"

    def _unmute_day(self, arg, mid):
        try:
            self.watcher.muted_days.discard(date.fromisoformat(arg))
        except ValueError:
            return "Unknown day"
        self.runtime.update(muted_days=sorted(d.isoformat() for d in self.watcher.muted_days))
        self.watcher.set_windows(self.watcher.settings.windows)  # re-check: alerts for that day come back
        self._go_wishes("", mid)
        return "🔔 Alerts for that day are back"

    # --- booking -------------------------------------------------------------------

    def booking_button(self, pid: str, poll: dict):
        """Extra button on your copy of a court alert."""
        bookings = self.watcher.bookings
        if bookings is None:
            return None
        if bookings.stage == "new":
            return [("🧪 Test booking (no payment)", f"bt:{pid}")]
        price = (poll.get("slot") or {}).get("price_eur")
        return [(f"💳 Book{f' €{price:g}' if price else ''}", f"bb:{pid}")]

    def list_buttons(self, slots, limit: int = 12):
        """One 🧪/💳 button per start time (first free court of that time), for list alerts and /now."""
        polls, bookings = self.watcher.polls, self.watcher.bookings
        if polls is None or bookings is None:
            return None
        from .notify import group_by_time

        rows, row = [], []
        for group in group_by_time(slots)[:limit]:
            slot = group[0]
            pid = polls.register(slot)
            icon = "🧪" if bookings.stage == "new" else "💳"
            row.append((f"{icon} {slot.start.strftime('%d.%m %H:%M')}", f"{'bt' if icon == '🧪' else 'bb'}:{pid}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows or None

    def _alert_slot(self, pid):
        polls = self.watcher.polls
        return polls.slot(pid) if polls is not None else None

    def _test_booking(self, arg, mid):
        slot = self._alert_slot(arg)
        if slot is None or self.watcher.bookings is None:
            return "This alert is too old"
        self.notifier.send(self.watcher.bookings.test(slot))
        return "Test done - see the message"

    def _ask_booking(self, arg, mid):
        from .booking import describe_slot
        from .notify import cancel_warning

        slot, bookings = self._alert_slot(arg), self.watcher.bookings
        if slot is None or bookings is None:
            return "This alert is too old"
        reason = bookings.refuse_reason(slot, auto=False)
        if reason:
            return reason[:190]
        rules = bookings.rules
        warn = cancel_warning(slot.start, self.watcher.clock(), self.watcher.settings.free_cancel_min)
        split = (f"\nSplit payment: others pay their share (about €{slot.price_eur / rules.players:.2f} each) "
                 "when they join." if rules.split_payment else "")
        text = (f"💳 Book {describe_slot(slot)} for €{slot.price_eur:g}?{split}\n"
                f"Payment: {' → '.join(m.replace('MERCHANT_WALLET', 'club wallet').replace('CREDIT_CARD', 'card').lower() for m in rules.payment_methods)}"
                f"{chr(10) + warn.strip() if warn else ''}")
        self.notifier.send(text, buttons=[[(f"✅ Yes, book €{slot.price_eur:g}", f"by:{arg}"), ("✖ No", "bn")]])
        return ""

    def _do_booking(self, arg, mid):
        slot, bookings = self._alert_slot(arg), self.watcher.bookings
        if slot is None or bookings is None:
            return "This alert is too old"
        self._screen(mid, "⏳ Booking...", [])
        outcome = bookings.book(slot, "tap")
        self._screen(mid, outcome.text, [])
        if outcome.booked:
            self._tell_players(arg, outcome.text)
        return "Booked ✅" if outcome.booked else "Not booked"

    def _tell_players(self, pid: str, booked_text: str) -> None:
        """Send the booking to friends who tapped ✅ I'm in, so they can join and pay their share."""
        poll = (self.watcher.polls.polls if self.watcher.polls else {}).get(pid) or {}
        owner = str(self.notifier.chat_id)
        link = next((line for line in booked_text.splitlines() if line.startswith("Match: ")), "")
        for chat_id in poll.get("in", {}):
            if chat_id == owner:
                continue
            self._tell(chat_id, f"🎾 Booked: {poll.get('label', 'the court')}. "
                                f"Join the match in Playtomic and pay your share.\n{link}".strip())

    def _toggle_snipe(self, arg, mid):
        sniper, bookings = self.watcher.sniper, self.watcher.bookings
        if sniper is None or bookings is None:
            return "Not available"
        if not bookings.auto_on:
            return "🔒 Switch on 🤖 automatic booking first"
        sniper.enabled = not sniper.enabled
        self.runtime.update(snipe=sniper.enabled)
        self._go_settings("", mid)
        nxt = sniper.next_opening(self.watcher.clock())
        when = f" Next: {nxt.at.strftime('%a %H:%M')} for {nxt.day.strftime('%d.%m.')} {hhmm(nxt.start_min)}." if nxt else ""
        return ("⚡ Booking at opening ON." + when) if sniper.enabled else "⚡ Booking at opening off"

    def _toggle_auto(self, arg, mid):
        bookings = self.watcher.bookings
        if bookings is None:
            return "Booking isn't set up"
        toast = bookings.set_auto(not bookings.auto_on)
        if bookings.auto_on:
            bookings.auto_book(self.watcher.current_matches())  # also slots already found
        self._go_settings("", mid)
        return toast

    def _login(self, arg, mid):
        self.notifier.send(self.center.handle("/login"))
        return "Follow the steps in the chat"

    def _logout(self, arg, mid):
        self.notifier.send(self.center.handle("/logout"))
        self._go_settings("", mid)
        return "Logged out"
