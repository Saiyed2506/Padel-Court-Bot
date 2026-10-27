"""⚡ Book at opening: grab a slot the moment Playtomic releases it.

The club releases courts on a rolling basis: the slot starting at 17:00 on a
day 14 days from now becomes bookable today at 17:00 (every :00 / :30). For
every such moment that matches a wish, the bot:

  * ~40 s before: renews the login and reads your existing bookings, so
    nothing slow is left for the moment itself;
  * at +0.5 s (Playtomic's clock, corrected for your computer's): reads that
    day; if the slot is free, books it at once;
  * if it isn't visible yet: reads again at +1.5, +3, +6, +10, +20, +30 s.

That's at most 7 reads per opening - fast, but no hammering. Afterwards you
get a short report of which court was free when. If a wish has
⭐ times, only those are grabbed (else 16:00 would use up the one booking for
that day before 16:30 even opens). All booking rules apply (max price, 1/day,
automatic booking must be on).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from .playtomic import PlaytomicError
from .timeutil import DAY_KEYS, format_day, minutes_of_day
from .wishes import hhmm

log = logging.getLogger("padel_bot")

RETRIES_S = (0.5, 1.5, 3.0, 6.0, 10.0, 20.0, 30.0)   # seconds after the opening
WALLET_WARN_H = 3                                    # warn this long before an opening
PREPARE_S = 40                       # start preparing this long before


@dataclass(frozen=True)
class Opening:
    at: datetime        # when it opens (club time)
    day: date           # the day of play
    start_min: int      # the slot's start time


def _wanted_starts(windows, day: date) -> set[int]:
    """Start times on `day` worth grabbing at opening."""
    starts: set[int] = set()
    for w in windows:
        if w.on_date is not None:
            if w.on_date != day:
                continue
        elif DAY_KEYS[day.weekday()] not in w.days:
            continue
        duration = min(w.durations)
        possible = set(range(w.start_min, w.end_min - duration + 1, 30))
        starts |= (possible & set(w.preferred_starts)) if w.preferred_starts else possible
    return starts


class OpeningSniper:
    def __init__(self, watcher, bookings, *, days_ahead: int, sleep: Callable[[float], None] = time.sleep):
        self.watcher = watcher
        self.bookings = bookings
        self.days_ahead = days_ahead
        self.sleep = sleep
        self.enabled = False
        self.done: set[tuple[str, int]] = set()   # openings already handled
        self.missed_note_day: date | None = None
        self.wallet_warned: date | None = None

    @property
    def active(self) -> bool:
        return self.enabled and self.bookings.auto_on and not self.watcher.paused

    def openings(self, now: datetime, hours: float = 24) -> list[Opening]:
        """Upcoming openings that match your wishes."""
        found = []
        mark = now.replace(second=0, microsecond=0)
        mark += timedelta(minutes=(30 - mark.minute % 30) % 30)
        if mark <= now - timedelta(seconds=PREPARE_S):
            mark += timedelta(minutes=30)
        end = now + timedelta(hours=hours)
        while mark <= end:
            day = mark.date() + timedelta(days=self.days_ahead)
            start = minutes_of_day(mark)
            if start in _wanted_starts(self.watcher.settings.windows, day) and (day.isoformat(), start) not in self.done:
                found.append(Opening(mark, day, start))
            mark += timedelta(minutes=30)
        return found

    def next_opening(self, now: datetime) -> Opening | None:
        if not self.active:
            return None
        upcoming = [o for o in self.openings(now, hours=6) if o.at + timedelta(seconds=RETRIES_S[-1]) > now]
        return upcoming[0] if upcoming else None

    def seconds_until_prepare(self, now: datetime) -> float | None:
        nxt = self.next_opening(now)
        return None if nxt is None else (nxt.at - now).total_seconds() - PREPARE_S

    def maybe_fire(self) -> bool:
        """Called from the main loop. Handles an opening that's about to happen."""
        now = self.watcher.clock()
        nxt = self.next_opening(now)
        if nxt is None or (nxt.at - now).total_seconds() > PREPARE_S:
            return False
        self.fire(nxt)
        return True

    def _offset(self) -> timedelta:
        """Playtomic's clock minus your computer's (from its replies)."""
        return timedelta(seconds=getattr(self.watcher.client, "clock_offset_s", 0.0) or 0.0)

    def fire(self, opening: Opening) -> bool:
        self.done.add((opening.day.isoformat(), opening.start_min))
        label = f"{format_day(datetime(opening.day.year, opening.day.month, opening.day.day))} {hhmm(opening.start_min)}"
        self.bookings.prepare()  # fresh login + your bookings, before the moment
        at_limit = bool(self.bookings.rules.max_active and self.bookings.state.get("at_limit"))
        local_open = opening.at - self._offset()
        seen: dict[str, list[float]] = {}   # court -> [first seen free, first seen gone]
        told_free = False
        for delay in RETRIES_S:
            wait = (local_open + timedelta(seconds=delay) - self.watcher.clock()).total_seconds()
            if wait > 0:
                self.sleep(wait)
            try:
                matches = self.watcher.fetch_matches(opening.day, self.watcher.clock())
            except PlaytomicError as exc:
                log.warning("Opening %s: couldn't read the day: %s", label, exc)
                continue
            slots = [m for m in matches if minutes_of_day(m.start) == opening.start_min]
            free_now = {m.court_name for m in slots}
            for court in free_now:
                seen.setdefault(court, [delay, None])
            for court, times in seen.items():
                if court not in free_now and times[1] is None:
                    times[1] = delay
            if at_limit:
                if slots and not told_free:
                    told_free = True
                    limit = self.bookings.rules.max_active
                    self._send(f"🎾 Free now: {', '.join(sorted(free_now))} - {label}. Not booked "
                               f"({getattr(self.bookings, 'active_count', limit)}/{limit} active) - "
                               "cancel a match to book it yourself.")
                continue
            for slot in slots:
                outcome = self.bookings.book(slot, "auto", fast=True)
                log.info("Opening %s (+%gs): %s", label, delay, outcome.text.splitlines()[0])
                if outcome.booked:
                    self._send(f"⚡ Grabbed at opening (+{delay:g}s)!\n{outcome.text}")
                    return True
                if "Someone else" not in outcome.text and "Payment" not in outcome.text:
                    if outcome.text:
                        self._send(outcome.text)  # a rule or Playtomic said no: don't keep trying
                    return False
        self._send(self.report(opening, seen, label), silent=True)
        return False

    def report(self, opening: Opening, seen: dict, label: str) -> str:
        """'👀 Watched Tue 10 Nov 17:00 (opened Tue 27 Oct 17:00): Indoor 2: free from +0.5 s, gone after 6 s'"""
        wanted = [c.name for c in self.watcher.courts.values()
                  if c.indoor and (self.watcher.settings.court_size == "any" or c.size == self.watcher.settings.court_size)]
        lines = []
        for court in sorted(set(wanted) | set(seen)):
            if court not in seen:
                lines.append(f"{court}: never seen free")
            else:
                first, gone = seen[court]
                lines.append(f"{court}: free from +{first:g} s" + (f", gone after {gone:g} s" if gone is not None else ""))
        return f"👀 Watched {label} (opened {opening.at.strftime('%a %d %b %H:%M')}):\n" + "\n".join(lines)

    def wallet_check(self) -> None:
        """A few hours before an opening: warn if the club wallet won't cover your share."""
        if not self.active:
            return
        wallet = self.bookings.state.get("wallet")
        now = self.watcher.clock()
        soon = [o for o in self.openings(now, hours=WALLET_WARN_H)]
        if not wallet or not soon or self.wallet_warned == now.date():
            return
        prices = sorted(m.price_eur for m in self.watcher.current_matches() if m.price_eur)
        price = prices[len(prices) // 2] if prices else self.bookings.rules.max_price_eur
        need = self.bookings.share(price)
        if wallet["balance"] >= need:
            return
        o = soon[0]
        when = datetime.fromisoformat(wallet["at"]).strftime("%a %H:%M")
        self._send(f"💰 Wallet {self.watcher.club.name}: €{wallet['balance']:.2f} (as of {when}). About €{need:.2f} "
                   f"is needed for {format_day(datetime(o.day.year, o.day.month, o.day.day))} {hhmm(o.start_min)} "
                   f"(opens {o.at.strftime('%a %H:%M')}) - if it's not enough by then, the bot pays with your card.")
        self.wallet_warned = now.date()

    def _send(self, text: str, silent: bool = False) -> None:
        try:
            self.watcher.notifier.send(text, **({"silent": True} if silent else {}))
        except Exception as exc:  # NotifyError: the booking itself is unaffected
            log.error("Could not report an opening: %s", exc)
