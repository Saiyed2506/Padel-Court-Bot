"""The polling loop: decide which days to check, find new slots, alert you."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, tzinfo
from typing import Callable

from .config import Settings
from .notify import NotifyError, booking_link, build_messages, format_slot
from .playtomic import Club, PlaytomicClient, PlaytomicError
from .rules import Slot, find_matches, parse_availability
from .state import SeenState
from .timeutil import format_day, minutes_of_day

log = logging.getLogger("padel_bot")

MAX_BACKOFF_S = 3600


def in_quiet_hours(now: datetime, quiet: tuple[int, int] | None) -> bool:
    if not quiet:
        return False
    start, end = quiet
    minute = minutes_of_day(now)
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end  # window wraps past midnight


def seconds_until_quiet_end(now: datetime, quiet: tuple[int, int]) -> float:
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=quiet[1])
    if end <= now:
        end += timedelta(days=1)
    return (end - now).total_seconds()


class Watcher:
    def __init__(self, settings: Settings, client: PlaytomicClient, club: Club, notifier,
                 state: SeenState, tz: tzinfo, booker=None, *,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None):
        self.settings = settings
        self.client = client
        self.club = club
        self.courts = {c.resource_id: c for c in club.courts}
        self.notifier = notifier
        self.state = state
        self.tz = tz
        self.bookings = None  # a BookingManager when booking is set up (booker arg is unused)
        self.sniper = None    # ⚡ book at opening (OpeningSniper)
        self.clock = clock or (lambda: datetime.now(tz))
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.last_fetch: dict[date, datetime] = {}
        self.failed_rounds = 0
        self.failure_alert_sent = False
        self.backoff_s = 0.0
        self.paused = False
        self.check_requested = False
        self.last_round_at: datetime | None = None
        self.last_error: str | None = None
        self.started_at = self.clock()
        self.rounds = 0
        self.matches_by_day: dict[date, list[Slot]] = {}
        self.commands = None  # a CommandCenter, when Telegram commands are on
        self.events = None    # an EventWatcher, when event watching is on
        self.polls = None     # "who's in?" voting on court alerts
        self.reminders = None  # "I booked it" -> reminder before free cancellation ends
        self.muted_days: set[date] = set()
        self.stats = {"rounds": 0, "errors": 0, "court_alerts": 0}
        now = self.started_at
        report = self.settings.daily_report_min
        # Started after today's report time: the first report comes tomorrow.
        self.last_report_day = now.date() if report is not None and now.hour * 60 + now.minute >= report else None

    @property
    def settings(self) -> Settings:
        return self._settings

    @settings.setter
    def settings(self, value: Settings) -> None:
        # Button changes (night pause, buffer, ...) apply to event checks too.
        self._settings = value
        events = getattr(self, "events", None)
        if events is not None:
            events.settings = value

    # --- controls used by Telegram commands ----------------------------

    def set_windows(self, windows: tuple) -> None:
        self.settings = replace(self.settings, windows=tuple(windows))
        self.last_fetch.clear()   # re-check every day with the new wishes
        self.matches_by_day.clear()
        self.request_check()

    def slot_still_free(self, slot: Slot) -> bool:
        """Re-read that day right before booking."""
        try:
            matches = self.fetch_matches(slot.start.date(), self.clock())
        except PlaytomicError:
            return False
        return any(m.key == slot.key for m in matches)

    def request_check(self) -> None:
        self.check_requested = True

    def current_matches(self) -> list[Slot]:
        now = self.clock()
        return [s for day in sorted(self.matches_by_day) for s in self.matches_by_day[day]
                if s.start > now and day not in self.muted_days]

    def status_text(self) -> str:
        now = self.clock()
        state = "⏸ paused" if self.paused else "▶️ running"
        if in_quiet_hours(now, self.settings.quiet_hours) and not self.paused:
            state += " (quiet hours, no checks until " + f"{self.settings.quiet_hours[1] // 60:02d}:{self.settings.quiet_hours[1] % 60:02d})"
        last = self.last_round_at.strftime("%a %H:%M") if self.last_round_at else "not yet"
        lines = [
            f"Padel bot: {state}",
            f"Running since {self.started_at.strftime('%a %d %b %H:%M')}, {self.rounds} check round(s)",
            f"Last check: {last}",
            f"Wishes: {len(self.settings.windows)} (see /wishes)",
            f"Free matching slots right now: {len(self.current_matches())} (see /now)",
        ]
        if self.failed_rounds:
            lines.append(f"⚠️ Last {self.failed_rounds} round(s) had errors: {self.last_error}")
        return "\n".join(lines)

    def daily_report_due(self, now: datetime) -> bool:
        at = self.settings.daily_report_min
        return at is not None and now.hour * 60 + now.minute >= at and self.last_report_day != now.date()

    def daily_report_text(self, now: datetime) -> str:
        st = self.stats
        events = self.events
        event_alerts = getattr(events, "alert_count", 0) if events else 0
        lines = [
            "☀️ Good morning - I'm still watching." if not self.paused else "☀️ Good morning - I'm running but PAUSED (Settings → Resume).",
            f"Since the last check-in: {st['rounds']} court checks, {st['court_alerts']} court alert(s), "
            f"{event_alerts} event alert(s), {st['errors']} error(s).",
            f"Wishes: {len(self.settings.windows)}"
            + (f" · Events: {', '.join(events.words.load()) or '-'}" if events else ""),
        ]
        if self.settings.min_notice_min:
            lines.append(f"⏱ Only games at least {self.settings.min_notice_min // 60}h ahead (free cancellation buffer).")
        if events is not None and events.open_play_problem:
            lines.append(f"⚠️ {events.open_play_problem}")
        return "\n".join(lines)

    def _maybe_daily_report(self) -> None:
        now = self.clock()
        if not self.daily_report_due(now):
            return
        self._safe_send(self.daily_report_text(now))
        self.last_report_day = now.date()
        self.stats = {"rounds": 0, "errors": 0, "court_alerts": 0}
        if self.events is not None:
            self.events.alert_count = 0

    def wait(self, seconds: float) -> None:
        """Sleep, but keep answering Telegram commands if they're on."""
        if self.commands is None:
            self.sleep(seconds)
        else:
            self.commands.listen(seconds)

    # --- scheduling ----------------------------------------------------

    def interval_for(self, offset: int) -> int:
        s = self.settings
        fast = offset < s.near_days or (s.newest_day_fast and offset == s.days_ahead)
        return s.near_interval_s if fast else s.far_interval_s

    def days_due(self, now: datetime) -> list[date]:
        # Half a fast interval of slack, so jitter never pushes a day back a whole round.
        slack = self.settings.near_interval_s / 2
        due = []
        for offset in range(self.settings.days_ahead + 1):
            day = now.date() + timedelta(days=offset)
            last = self.last_fetch.get(day)
            if last is None or (now - last).total_seconds() >= self.interval_for(offset) - slack:
                due.append(day)
        return due

    # --- one round -----------------------------------------------------

    def fetch_matches(self, day: date, now: datetime) -> list[Slot]:
        raw = self.client.get_availability(self.club.tenant_id, day)
        slots = parse_availability(raw, self.courts, self.tz, self.settings.api_times_utc)
        return find_matches(slots, self.courts, self.settings, now, target_day=day)

    def run_round(self) -> list[Slot]:
        """Check all due days once. Returns the newly found slots."""
        now = self.clock()
        self.check_requested = False
        due = self.days_due(now)
        new_by_day: dict[date, list[Slot]] = {}
        errors: list[PlaytomicError] = []

        for i, day in enumerate(due):
            if i:
                self.sleep(self.rng.uniform(*self.settings.request_pause_s))
            try:
                matches = self.fetch_matches(day, now)
            except PlaytomicError as exc:
                errors.append(exc)
                log.warning("Could not check %s: %s", day, exc)
                if exc.status == 429:
                    break  # asked to slow down: stop this round immediately
                continue
            self.last_fetch[day] = now
            self.matches_by_day[day] = matches
            if day in self.muted_days:
                continue  # not marked as seen, so unmuting brings these alerts back
            new_keys = self.state.update_day(day, {m.key for m in matches})
            new = [m for m in matches if m.key in new_keys]
            if new:
                new_by_day[day] = new

        self.state.forget_before(now.date())
        self.last_fetch = {d: t for d, t in self.last_fetch.items() if d >= now.date()}
        self.matches_by_day = {d: m for d, m in self.matches_by_day.items() if d >= now.date()}
        self.last_round_at = now
        self.rounds += 1
        self.muted_days = {d for d in self.muted_days if d >= now.date()}
        new_slots = [s for day in sorted(new_by_day) if day not in self.muted_days for s in new_by_day[day]]
        new_by_day = {d: v for d, v in new_by_day.items() if d not in self.muted_days}
        self.stats["rounds"] += 1
        self.stats["errors"] += len(errors)
        if new_slots:
            self._announce(new_slots, new_by_day)
        self.state.save()
        self._track_health(errors, checked=len(due))
        log.info("Checked %d day(s): %d new slot(s), %d error(s)", len(due), len(new_slots), len(errors))
        return new_slots

    POLL_LIMIT = 6  # up to this many new slots: one message each, with "who's in?"

    def _announce(self, new_slots: list[Slot], new_by_day: dict[date, list[Slot]]) -> None:
        s = self.settings
        link = lambda d: booking_link(s.booking_url, self.club, d)
        broadcast = getattr(self.notifier, "broadcast", None)
        try:
            if self.polls is not None and len(new_slots) <= self.POLL_LIMIT:
                for slot in new_slots:
                    text = (f"🎾 Free court at {s.club_name}\n\n"
                            f"{format_slot(slot, link(slot.start.date()), self.clock(), s.free_cancel_min)}")
                    self.polls.post(slot, text, silent=not slot.preferred)  # only ⭐ times ring
            else:
                silent = not any(slot.preferred for slot in new_slots)
                messages = build_messages(new_slots, s.club_name, link, self.clock(), s.free_cancel_min)
                menu = getattr(self.commands, "menu", None)
                buttons = menu.list_buttons(new_slots) if menu is not None else None
                for i, text in enumerate(messages):
                    last = i == len(messages) - 1
                    if broadcast is not None:
                        # booking buttons on your copy of the last part only; friends get the plain list
                        broadcast(text, silent=silent, **({"buttons": buttons, "friend_buttons": None}
                                                          if last and buttons else {}))
                    else:
                        self.notifier.send(text)
        except NotifyError as exc:
            log.error("%s - will retry these slots next round", exc)
            # Forget them, so they count as "new" again next round.
            for day, slots in new_by_day.items():
                self.state.forget_keys(day, {x.key for x in slots})
            return
        self.stats["court_alerts"] += len(new_slots)
        for slot in new_slots:
            log.info("Alerted: %s %s %s", format_day(slot.start), slot.start.strftime("%H:%M"), slot.court_name)
        if self.bookings is not None:
            self.bookings.auto_book(new_slots)  # does nothing unless automatic booking is on

    def _track_health(self, errors: list[PlaytomicError], checked: int) -> None:
        if not errors:
            self.backoff_s = 0
            if self.failure_alert_sent:
                self._safe_send("✅ Padel bot: Playtomic is reachable again, watching resumed.")
                self.failure_alert_sent = False
            self.failed_rounds = 0
            return
        self.failed_rounds += 1
        self.last_error = str(errors[-1])
        self.backoff_s = min(max(self.backoff_s * 2, self.settings.near_interval_s), MAX_BACKOFF_S)
        if self.failed_rounds >= self.settings.alert_after_failed_rounds and not self.failure_alert_sent:
            self._safe_send(
                f"⚠️ Padel bot: the last {self.failed_rounds} checks had errors "
                f"(latest: {errors[-1]}). You may be missing free courts. "
                "Playtomic may have changed its API or be blocking requests."
            )
            self.failure_alert_sent = True

    def _safe_send(self, text: str) -> None:
        try:
            self.notifier.send(text)
        except NotifyError as exc:
            log.error("%s", exc)

    # --- forever -------------------------------------------------------

    def next_sleep_s(self) -> float:
        jitter = self.rng.uniform(-self.settings.jitter_s, self.settings.jitter_s)
        return max(60.0, self.settings.near_interval_s + jitter) + self.backoff_s

    def run_forever(self) -> None:
        while True:
            now = self.clock()
            self._maybe_daily_report()
            if self.sniper is not None:
                try:
                    self.sniper.wallet_check()
                    if self.sniper.maybe_fire():
                        continue  # an opening was handled; look again right away
                except Exception:
                    log.exception("Opening booking failed")
            if self.reminders is not None:
                try:
                    self.reminders.due(now, silent=in_quiet_hours(now, self.settings.quiet_hours))
                except Exception:
                    log.exception("Reminder check failed")
            if self.paused:
                self.check_requested = False
                self.wait(300)  # nothing to check; still answer commands
                continue
            if in_quiet_hours(now, self.settings.quiet_hours):
                wait = seconds_until_quiet_end(now, self.settings.quiet_hours)
                if self.sniper is not None:
                    until = self.sniper.seconds_until_prepare(now)
                    if until is not None:
                        wait = max(1.0, min(wait, until))
                log.info("Quiet hours - sleeping %.0f minutes", wait / 60)
                self.check_requested = False
                self.wait(min(wait + self.rng.uniform(0, 120), 1800))  # re-evaluate at least every 30 min
                continue
            if self.settings.windows:
                try:
                    self.run_round()
                except Exception:  # keep running; one bad reply shouldn't stop the watcher
                    log.exception("Unexpected error in this round")
                    self.backoff_s = min(max(self.backoff_s * 2, self.settings.near_interval_s), MAX_BACKOFF_S)
            else:
                self.check_requested = False
            if self.events is not None:
                try:
                    self.events.tick()
                except Exception:
                    log.exception("Unexpected error while checking events")
            wait = self.next_sleep_s()
            if self.sniper is not None:
                until = self.sniper.seconds_until_prepare(self.clock())
                if until is not None:
                    wait = max(1.0, min(wait, until))  # wake up in time for the next opening
            log.info("Next check in %.1f minutes", wait / 60)
            self.wait(wait)
