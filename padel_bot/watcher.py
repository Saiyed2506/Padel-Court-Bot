"""The polling loop: decide which days to check, find new slots, alert you."""

from __future__ import annotations

import logging
import random
import time
from datetime import date, datetime, timedelta, tzinfo
from typing import Callable

from .booking import AutoBooker
from .config import Settings
from .notify import NotifyError, booking_link, build_messages
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
                 state: SeenState, tz: tzinfo, booker: AutoBooker, *,
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
        self.booker = booker
        self.clock = clock or (lambda: datetime.now(tz))
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.last_fetch: dict[date, datetime] = {}
        self.failed_rounds = 0
        self.failure_alert_sent = False
        self.backoff_s = 0.0

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
            new_keys = self.state.update_day(day, {m.key for m in matches})
            new = [m for m in matches if m.key in new_keys]
            if new:
                new_by_day[day] = new

        self.state.forget_before(now.date())
        self.last_fetch = {d: t for d, t in self.last_fetch.items() if d >= now.date()}
        new_slots = [s for day in sorted(new_by_day) for s in new_by_day[day]]
        if new_slots:
            self._announce(new_slots, new_by_day)
        self.state.save()
        self._track_health(errors, checked=len(due))
        log.info("Checked %d day(s): %d new slot(s), %d error(s)", len(due), len(new_slots), len(errors))
        return new_slots

    def _announce(self, new_slots: list[Slot], new_by_day: dict[date, list[Slot]]) -> None:
        s = self.settings
        messages = build_messages(new_slots, s.club_name,
                                  lambda d: booking_link(s.booking_url, self.club, d))
        try:
            for text in messages:
                self.notifier.send(text)
        except NotifyError as exc:
            log.error("%s - will retry these slots next round", exc)
            # Forget them, so they count as "new" again next round.
            for day, slots in new_by_day.items():
                self.state.forget_keys(day, {x.key for x in slots})
            return
        for slot in new_slots:
            log.info("Alerted: %s %s %s", format_day(slot.start), slot.start.strftime("%H:%M"), slot.court_name)
            decision = self.booker.consider(slot)
            if decision.action != "skipped" or self.booker.settings.enabled:
                log.info("Auto-book %s: %s", decision.action, decision.reason)

    def _track_health(self, errors: list[PlaytomicError], checked: int) -> None:
        if not errors:
            self.backoff_s = 0
            if self.failure_alert_sent:
                self._safe_send("✅ Padel bot: Playtomic is reachable again, watching resumed.")
                self.failure_alert_sent = False
            self.failed_rounds = 0
            return
        self.failed_rounds += 1
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
            if in_quiet_hours(now, self.settings.quiet_hours):
                wait = seconds_until_quiet_end(now, self.settings.quiet_hours)
                log.info("Quiet hours - sleeping %.0f minutes", wait / 60)
                self.sleep(wait + self.rng.uniform(0, 120))
                continue
            try:
                self.run_round()
            except Exception:  # keep running; one bad reply shouldn't stop the watcher
                log.exception("Unexpected error in this round")
                self.backoff_s = min(max(self.backoff_s * 2, self.settings.near_interval_s), MAX_BACKOFF_S)
            wait = self.next_sleep_s()
            log.info("Next check in %.1f minutes", wait / 60)
            self.sleep(wait)
