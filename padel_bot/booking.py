"""Auto-booking: designed in, switched off, and not wired to Playtomic yet.

Safety rules enforced here (and covered by tests):
  1. Nothing happens unless [auto_book] enabled = true.
  2. A max price is mandatory; anything pricier or without a known price is skipped.
  3. You must confirm every booking (the `confirm` callback). There is no
     setting to turn this off.
  4. dry_run = true (the default) only reports what it *would* book.
  5. Real booking needs a BookingBackend, which doesn't exist yet. Writing one
     means logging in as you and paying through Playtomic's undocumented
     payment API - read the risks in the README before doing that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .config import AutoBookSettings
from .rules import Slot


class BookingBackend(Protocol):
    def book(self, slot: Slot) -> str:
        """Book the slot for real and return a booking reference."""


@dataclass(frozen=True)
class BookingDecision:
    action: str          # "skipped", "dry_run" or "booked"
    reason: str


class AutoBooker:
    def __init__(self, settings: AutoBookSettings, confirm: Callable[[Slot], bool],
                 backend: BookingBackend | None = None):
        if settings.enabled and (settings.max_price_eur is None or settings.max_price_eur <= 0):
            raise ValueError("auto-booking requires max_price_eur > 0")
        if settings.enabled and not settings.dry_run and backend is None:
            raise ValueError("real auto-booking is not implemented; keep dry_run = true")
        self.settings = settings
        self.confirm = confirm
        self.backend = backend

    def consider(self, slot: Slot) -> BookingDecision:
        s = self.settings
        if not s.enabled:
            return BookingDecision("skipped", "auto-booking is disabled")
        if slot.price_eur is None:
            return BookingDecision("skipped", "price unknown")
        if slot.price_eur > s.max_price_eur:
            return BookingDecision("skipped", f"€{slot.price_eur:g} is above max €{s.max_price_eur:g}")
        if s.dry_run:
            # Dry run never asks you and never books; it only reports.
            return BookingDecision("dry_run", f"would ask you to confirm booking {slot.key} for €{slot.price_eur:g}")
        if not self.confirm(slot):
            return BookingDecision("skipped", "not confirmed by you")
        reference = self.backend.book(slot)
        return BookingDecision("booked", f"booked, reference {reference}")
