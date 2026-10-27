"""Booking courts through Playtomic with your login - in three stages.

  1. 🧪 Test: start a booking for a real slot but do NOT confirm it. Shows the
     price and the payment methods Playtomic offers you. Nothing is paid.
  2. 💳 One booking with a tap (and a "Yes, book" confirmation).
  3. 🤖 Automatic: only unlockable after stage 2 worked; switched on in Settings.

Safety rules (BookingSettings, config.toml [booking]):
  * never above max_price_eur (whole-court price), never a slot that has started
  * at most max_per_day bot bookings per day of play (and max_per_week if set)
  * the same slot is never booked twice; it's re-checked as free right before
  * automatic bookings only auto_min_hours (24h) ahead, so they can be
    cancelled for free, and not on days you already play (checked in Playtomic)
  * payment methods are tried in your order (club wallet, then card); if a
    payment needs a confirmation in your bank app, the bot stops and tells you
  * if the outcome is unclear (network error while confirming), the bot never
    retries - it asks you to check the app, so nothing is booked twice
  * every attempt is written to bookings_log.jsonl

This uses Playtomic's undocumented app API (as the open-source padel-cli
does). It can break or be refused at any time, and Playtomic could restrict
an account that books automatically.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .account import AuthError, PlaytomicAccount
from .config import BookingSettings
from .playtomic import PlaytomicError
from .rules import Slot
from .timeutil import format_day, format_time

log = logging.getLogger("padel_bot")

CART_PAYMENT_TYPES = ["OFFER", "CASH", "MERCHANT_WALLET", "DIRECT", "SWISH", "IDEAL", "BANCONTACT",
                      "PAYTRAIL", "CREDIT_CARD", "QUICK_PAY"]
METHOD_NAMES = {"MERCHANT_WALLET": "club wallet", "CREDIT_CARD": "card"}
MATCH_LINK = "https://app.playtomic.com/matches/{id}"  # unverified format


class BookingError(Exception):
    """A booking that didn't happen; the message is shown to you."""


class UnclearOutcome(BookingError):
    """We can't tell whether it was booked - never retry automatically."""


def describe_slot(slot: Slot) -> str:
    return (f"{format_day(slot.start)} {format_time(slot.start)}-{format_time(slot.end)}, "
            f"{slot.court_name}")


def _find(data, keys: tuple[str, ...]):
    """First value for any of `keys`, searching nested dicts/lists."""
    if isinstance(data, dict):
        for key in keys:
            if data.get(key) not in (None, ""):
                return data[key]
        for value in data.values():
            found = _find(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find(value, keys)
            if found not in (None, ""):
                return found
    return None


def _amount(value) -> float | None:
    """'45 EUR' / '45.00' / 45 / {'amount': 45} -> 45.0"""
    if isinstance(value, dict):
        value = value.get("amount") or value.get("value")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.split()[0].replace(",", "."))
        except (ValueError, IndexError):
            return None
    return None


class PlaytomicBooker:
    """The three Playtomic calls: start a payment, pick a method, confirm."""

    def __init__(self, account: PlaytomicAccount, tenant_id: str, settings: BookingSettings):
        self.account = account
        self.tenant_id = tenant_id
        self.settings = settings

    def payload(self, slot: Slot) -> dict:
        start_utc = slot.start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return {
            "allowed_payment_method_types": CART_PAYMENT_TYPES,
            "user_id": self.account.user_id,
            "cart": {"requested_item": {
                "cart_item_type": "CUSTOMER_MATCH",
                "cart_item_voucher_id": None,
                "cart_item_data": {
                    "supports_split_payment": self.settings.split_payment,
                    "number_of_players": self.settings.players,
                    "tenant_id": self.tenant_id,
                    "resource_id": slot.resource_id,
                    "start": start_utc,
                    "duration": slot.duration,
                    "match_registrations": [{"user_id": self.account.user_id, "pay_now": True}],
                },
            }},
        }

    def create_intent(self, slot: Slot) -> dict:
        intent = self.account.send("POST", "/v1/payment_intents", self.payload(slot))
        if not isinstance(intent, dict) or not intent.get("payment_intent_id"):
            raise BookingError("Playtomic didn't start the booking (no payment id in its reply).")
        return intent

    @staticmethod
    def methods(intent: dict) -> list[tuple[str, str]]:
        """[(type, id_to_send)] as offered by Playtomic."""
        found = []
        for entry in intent.get("available_payment_methods") or []:
            if isinstance(entry, str):
                found.append((entry.upper(), entry))
            elif isinstance(entry, dict):
                kind = str(entry.get("method_type") or entry.get("type") or entry.get("code") or "").upper()
                ident = str(entry.get("payment_method_id") or entry.get("id") or entry.get("code") or kind)
                if kind:
                    found.append((kind, ident))
        return found

    def ordered_methods(self, intent: dict) -> list[tuple[str, str]]:
        offered = self.methods(intent)
        return [m for wanted in self.settings.payment_methods for m in offered if m[0] == wanted]

    def select(self, intent_id: str, method_id: str) -> None:
        self.account.send("PATCH", f"/v1/payment_intents/{intent_id}", {"selected_payment_method": method_id})

    def confirm(self, intent_id: str) -> dict:
        reply = self.account.send("POST", f"/v1/payment_intents/{intent_id}/confirmation", {})
        return reply if isinstance(reply, dict) else {}

    @staticmethod
    def needs_action(reply: dict) -> bool:
        """True when Playtomic wants an extra step (e.g. 3-D Secure in your bank app)."""
        status = str(_find(reply, ("status", "payment_status")) or "").upper()
        return bool(_find(reply, ("next_action", "redirect_url", "action_url", "client_secret"))) or \
            any(word in status for word in ("REQUIRES", "PENDING_AUTH", "ACTION"))

    @staticmethod
    def match_id(reply: dict) -> str:
        return str(_find(reply, ("match_id", "reservation_id", "booking_id")) or "")

    @staticmethod
    def price(intent: dict) -> float | None:
        return _amount(_find(intent, ("price", "total_price", "amount", "total")))

    @staticmethod
    def wallet_balance(intent: dict) -> float | None:
        """Club wallet balance, if Playtomic includes it with the payment methods."""
        for entry in intent.get("available_payment_methods") or []:
            if isinstance(entry, dict) and "WALLET" in str(entry.get("method_type") or entry.get("type") or "").upper():
                value = _find(entry, ("balance", "available_balance", "wallet_balance", "remaining_balance",
                                      "available_amount", "amount"))
                found = _amount(value)
                if found is not None:
                    return found
        return None

    def my_matches(self, from_utc: datetime) -> list[dict]:
        """Your upcoming Playtomic bookings (so automatic booking avoids days you already play)."""
        reply = self.account.get("/v1/matches", {"owner_id": self.account.user_id, "size": "50",
                                                 "sort": "start_date,DESC"})
        if isinstance(reply, dict):
            reply = reply.get("content") or []
        if not isinstance(reply, list):
            raise PlaytomicError("unexpected reply for your bookings", retryable=False)
        upcoming = []
        for match in reply:
            try:
                start = datetime.fromisoformat(str(match.get("start_date", "")).replace("Z", ""))
            except ValueError:
                continue
            if start.replace(tzinfo=timezone.utc) >= from_utc:
                upcoming.append(match)
        return upcoming


@dataclass
class Outcome:
    booked: bool
    text: str


class BookingManager:
    def __init__(self, settings_fn: Callable, booker: PlaytomicBooker, state_path: Path, log_path: Path,
                 notifier, tz, clock: Callable[[], datetime], *,
                 slot_still_free: Callable[[Slot], bool] | None = None, reminders=None):
        self.settings_fn = settings_fn          # current Settings (buttons can change them)
        self.booker = booker
        self.state_path = state_path
        self.log_path = log_path
        self.notifier = notifier
        self.tz = tz
        self.clock = clock
        self.slot_still_free = slot_still_free
        self.reminders = reminders
        try:
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("stage", "new")      # new -> tested -> verified
        self.state.setdefault("auto", False)
        self.state.setdefault("bookings", [])
        self.state.setdefault("wallet", None)       # {"balance": 11.84, "at": iso} - last seen
        self.state.setdefault("at_limit", False)    # at max_active bookings

    # --- state -----------------------------------------------------------------

    @property
    def rules(self) -> BookingSettings:
        return self.settings_fn().booking

    @property
    def stage(self) -> str:
        return self.state["stage"]

    @property
    def auto_on(self) -> bool:
        return bool(self.state.get("auto")) and self.stage == "verified"

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _log(self, **entry) -> None:
        entry["at"] = self.clock().isoformat()
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def set_auto(self, on: bool) -> str:
        if on and self.stage != "verified":
            return "🔒 First book once with a tap (💳 on a court alert) - then automatic can be switched on."
        self.state["auto"] = on
        self._save()
        return "🤖 Automatic booking ON" if on else "🤖 Automatic booking off"

    def share(self, price: float) -> float:
        rules = self.rules
        return price / rules.players if rules.split_payment else price

    def _note_wallet(self, intent: dict) -> float | None:
        balance = self.booker.wallet_balance(intent)
        if balance is not None:
            self.state["wallet"] = {"balance": balance, "at": self.clock().isoformat()}
            self._save()
        return balance

    def note_active(self, count: int) -> None:
        """Track Playtomic's active-booking limit; say when booking can resume."""
        limit = self.rules.max_active
        self.active_count = count
        if not limit:
            return  # no own cap: Playtomic decides
        if count >= limit and not self.state["at_limit"]:
            self.state["at_limit"] = True
            self._save()
        elif count < limit and self.state["at_limit"]:
            self.state["at_limit"] = False
            self._save()
            if self.auto_on:
                self._say(f"🎾 You're back at {count} active court booking(s) - I'll book automatically again.")
        self.active_count = count

    def _say(self, text: str, silent: bool = False) -> None:
        try:
            self.notifier.send(text, **({"silent": True} if silent else {}))
        except Exception as exc:
            log.error("Could not send: %s", exc)

    def bookings_on(self, day) -> list[dict]:
        return [b for b in self.state["bookings"] if b["day"] == day.isoformat()]

    # --- checks ------------------------------------------------------------------

    def refuse_reason(self, slot: Slot, auto: bool) -> str | None:
        now, rules = self.clock(), self.rules
        if not self.booker.account.logged_in:
            return "Not logged in to Playtomic - send /login first."
        if slot.start <= now:
            return "That game has already started."
        if slot.price_eur is None:
            return "The price is unknown, so I won't book it."
        if slot.price_eur > rules.max_price_eur:
            return f"€{slot.price_eur:g} is above your limit of €{rules.max_price_eur:g}."
        if any(b["key"] == slot.key for b in self.state["bookings"]):
            return "I already booked this slot."
        if len(self.bookings_on(slot.start.date())) >= rules.max_per_day:
            return f"There's already a bot booking on {format_day(slot.start)} (limit {rules.max_per_day}/day)."
        if rules.max_per_week:
            week = slot.start.isocalendar()[:2]
            same_week = [b for b in self.state["bookings"]
                         if datetime.fromisoformat(b["start"]).isocalendar()[:2] == week]
            if len(same_week) >= rules.max_per_week:
                return f"Weekly limit of {rules.max_per_week} bookings reached."
        if auto:
            if self.stage != "verified" or not self.state.get("auto"):
                return "Automatic booking is off."
            if (slot.start - now) < timedelta(hours=rules.auto_min_hours):
                return f"Less than {rules.auto_min_hours}h ahead - automatic booking only when cancellable for free."
        return None

    def _check_my_day(self, slot: Slot) -> str | None:
        """For automatic booking: skip days you already play (any booking in Playtomic)."""
        cache = getattr(self, "_my_cache", None)
        if cache is not None and (self.clock() - cache[0]).total_seconds() < 600:
            matches = cache[1]
        else:
            try:
                matches = self.booker.my_matches(self.clock().astimezone(timezone.utc))
            except PlaytomicError as exc:
                return f"Couldn't check your existing Playtomic bookings ({exc}), so I didn't auto-book."
            self.note_active(len(matches))
        if self.rules.max_active and len(matches) >= self.rules.max_active:
            return f"Not booked ({len(matches)}/{self.rules.max_active} active)"
        for match in matches:
            try:
                start = datetime.fromisoformat(str(match.get("start_date", "")).replace("Z", ""))
            except ValueError:
                continue
            if start.replace(tzinfo=timezone.utc).astimezone(self.tz).date() == slot.start.date():
                return f"You already have a Playtomic booking on {format_day(slot.start)}."
        return None

    # --- stage 1: test ---------------------------------------------------------------

    def test(self, slot: Slot) -> str:
        """Start a booking without confirming it: shows price and payment options."""
        reason = self.refuse_reason(slot, auto=False)
        if reason:
            return f"🧪 Test not run: {reason}"
        try:
            intent = self.booker.create_intent(slot)
        except (PlaytomicError, BookingError) as exc:
            self._log(kind="test", slot=slot.key, error=str(exc))
            return f"🧪 Test failed - Playtomic refused to start a booking:\n{exc}\n(Nothing was paid.)"
        offered = self.booker.methods(intent)
        usable = self.booker.ordered_methods(intent)
        price = self.booker.price(intent)
        try:
            matches = f"{len(self.booker.my_matches(self.clock().astimezone(timezone.utc)))} upcoming booking(s) found"
        except PlaytomicError as exc:
            matches = f"couldn't read your bookings ({exc})"
        self._log(kind="test", slot=slot.key, intent_keys=sorted(intent), intent=intent)
        wallet = self._note_wallet(intent)
        if self.stage == "new":
            self.state["stage"] = "tested"
            self._save()
        lines = [
            f"🧪 Test booking - NOT confirmed, nothing paid.",
            f"Slot: {describe_slot(slot)}, listed at €{slot.price_eur:g}",
            f"Playtomic's price: {'€%g' % price if price is not None else 'not shown'}"
            + (f" (split {self.rules.players} ways: about €{(price or slot.price_eur) / self.rules.players:.2f} each)"
               if self.rules.split_payment else ""),
            "Payment methods offered: " + (", ".join(t for t, _ in offered) or "none listed"),
            "Club wallet balance: " + (f"€{wallet:.2f}" if wallet is not None else "not shown by Playtomic"),
            "I would pay with: " + (", ".join(METHOD_NAMES.get(t, t) for t, _ in usable) or
                                    "❌ none of your choices (club wallet / card) - booking can't work yet"),
            f"Your bookings check: {matches}",
            f"(Reply fields for Claude: {', '.join(sorted(intent))[:250]})",
            "",
            "The started booking just expires. If this looks right, the next court alert has a "
            "'💳 Book' button for one real booking.",
        ]
        return "\n".join(lines)

    # --- stages 2 and 3: book ------------------------------------------------------------

    def prepare(self) -> None:
        """Before an opening: renew the login and read your bookings now, not at the moment."""
        try:
            self._my_cache = (self.clock(), self.booker.my_matches(self.clock().astimezone(timezone.utc)))
            self.note_active(len(self._my_cache[1]))
        except PlaytomicError as exc:
            self._my_cache = None
            log.warning("Couldn't prepare for the opening: %s", exc)

    def book(self, slot: Slot, mode: str, fast: bool = False) -> Outcome:
        """mode 'tap' (you confirmed) or 'auto'. Never raises; returns a message.

        fast=True (at an opening): the slot was just read as free, and your
        bookings were read in prepare(), so no extra requests before booking.
        """
        auto = mode == "auto"
        reason = self.refuse_reason(slot, auto=auto)
        if not reason and auto:
            reason = self._check_my_day(slot)
        if not reason and not fast and self.slot_still_free is not None and not self.slot_still_free(slot):
            reason = "Someone else just booked it."
        if reason:
            return Outcome(False, f"Not booked: {reason}")
        try:
            outcome = self._book(slot, mode)
        except UnclearOutcome as exc:
            self.state["auto"] = False  # stop automatic booking until you've looked
            self._save()
            self._log(kind="book", mode=mode, slot=slot.key, error=str(exc), unclear=True)
            return Outcome(False, f"⚠️ {exc}\nPlease check your bookings in the Playtomic app. "
                                  "Automatic booking is paused until you switch it on again.")
        except (BookingError, PlaytomicError) as exc:
            self._log(kind="book", mode=mode, slot=slot.key, error=str(exc))
            if isinstance(exc, AuthError):
                return Outcome(False, f"Not booked: {exc}")
            if "active bookings" in str(exc).lower() or getattr(exc, "status", None) == 409:
                if self.state.get("limit_notified"):
                    return Outcome(False, "")  # already told you about Playtomic's limit
                self.state["limit_notified"] = True
                self._save()
            return Outcome(False, f"❌ Didn't get it: {describe_slot(slot)} ({exc})")
        return outcome

    def _book(self, slot: Slot, mode: str) -> Outcome:
        intent = self.booker.create_intent(slot)
        intent_id = intent["payment_intent_id"]
        price = self.booker.price(intent)
        wallet_before = self._note_wallet(intent)
        if price is not None and price > self.rules.max_price_eur:
            raise BookingError(f"Playtomic's price €{price:g} is above your limit of €{self.rules.max_price_eur:g}.")
        methods = self.booker.ordered_methods(intent)
        if not methods:
            offered = ", ".join(t for t, _ in self.booker.methods(intent)) or "none"
            raise BookingError(f"None of your payment methods is offered (Playtomic offers: {offered}).")
        last_error = None
        for kind, method_id in methods:
            try:
                self.booker.select(intent_id, method_id)
            except PlaytomicError as exc:
                last_error = exc
                continue  # e.g. not enough wallet credit: try the next method
            try:
                reply = self.booker.confirm(intent_id)
            except PlaytomicError as exc:
                if exc.status == 409:
                    raise BookingError(str(exc)) from None  # e.g. too many active bookings: not a payment problem
                if exc.status is not None and 400 <= exc.status < 500:
                    last_error = exc
                    continue  # payment refused: nothing charged, next method
                raise UnclearOutcome(f"I don't know whether the booking went through ({exc}).") from None
            self._log(kind="book", mode=mode, slot=slot.key, method=kind, reply=reply)
            if self.booker.needs_action(reply):
                return Outcome(False, f"💳 Playtomic needs you to confirm the payment ({METHOD_NAMES.get(kind, kind)}). "
                                      "Open the Playtomic app to finish it - the slot may already be held for you.")
            return self._success(slot, mode, kind, self.booker.match_id(reply), price, wallet_before)
        raise BookingError(f"Payment didn't work with any of your methods ({last_error}).")

    def _success(self, slot: Slot, mode: str, method: str, match_id: str, price: float | None,
                 wallet_before: float | None = None) -> Outcome:
        now = self.clock()
        self._my_cache = None  # your bookings just changed
        self.state["limit_notified"] = False
        self.state["bookings"].append({
            "key": slot.key, "day": slot.start.date().isoformat(), "start": slot.start.isoformat(),
            "court": slot.court_name, "price": price or slot.price_eur, "match_id": match_id,
            "mode": mode, "method": method, "at": now.isoformat()})
        self.state["bookings"] = [b for b in self.state["bookings"]
                                  if datetime.fromisoformat(b["start"]) > now - timedelta(days=60)]
        if mode == "tap":
            self.state["stage"] = "verified"
        self._save()
        rules = self.rules
        deadline = slot.start - timedelta(hours=24)
        total = price or slot.price_eur
        mine = self.share(total)
        paid = (f"your share €{mine:.2f} (court €{total:g})" if rules.split_payment else f"€{total:g}")
        wallet_left = ""
        if method == "MERCHANT_WALLET" and wallet_before is not None:
            left = wallet_before - mine
            self.state["wallet"] = {"balance": left, "at": now.isoformat()}
            self._save()
            wallet_left = f" Wallet left €{left:.2f}."
        lines = [
            f"{'🤖 BOOKED' if mode == 'auto' else '✅ BOOKED'}: {slot.court_name} {format_day(slot.start)} "
            f"{format_time(slot.start)}-{format_time(slot.end)} ({slot.duration} min), {paid} via "
            f"{METHOD_NAMES.get(method, method)}.{wallet_left}"
            + (" Invite your fellow players in the app now." if rules.split_payment else ""),
            (f"Free cancellation until {format_day(deadline)} {format_time(deadline)} (in the Playtomic app)."
             if deadline > now else "⚠️ Less than 24h ahead - this one can't be cancelled for free."),
        ]
        if match_id:
            lines.append(f"Match: {MATCH_LINK.format(id=match_id)}")
        if self.reminders is not None and deadline > now:
            self.reminders.add(str(self.notifier.chat_id), f"b:{slot.key}", describe_slot(slot), slot.start, now)
            lines.append("⏰ I'll remind you a day before free cancellation ends.")
        if mode == "tap" and not self.state.get("auto"):
            lines.append("\nThe first real booking worked. Automatic booking can now be switched on in ⚙️ Settings.")
        return Outcome(True, "\n".join(lines))

    # --- automatic --------------------------------------------------------------------------

    def auto_book(self, slots: list[Slot]) -> list[Outcome]:
        """Called with newly found slots; books what the rules allow."""
        if not self.auto_on:
            return []
        results = []
        for slot in sorted(slots, key=lambda s: (not s.preferred, s.start)):
            if self.refuse_reason(slot, auto=True):
                continue  # silently skip what the rules don't allow
            conflict = self._check_my_day(slot)
            if conflict:
                if conflict.startswith("Couldn't"):
                    self.notifier.send(f"🤖 {conflict}")  # can't check safely: stop for this round
                    break
                continue  # you already play that day
            outcome = self.book(slot, "auto")
            results.append(outcome)
            if not outcome.text:
                continue  # nothing new to tell (e.g. Playtomic's limit, already reported)
            try:
                self.notifier.send(outcome.text)
            except Exception as exc:  # NotifyError - the booking itself is done
                log.error("Could not report a booking: %s", exc)
            if not self.auto_on:
                break  # paused itself after an unclear outcome
        return results
