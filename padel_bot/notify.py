"""Format slot alerts and send them via Telegram."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from .playtomic import Club
from .rules import Slot
from .timeutil import format_day, format_time

TELEGRAM_LIMIT = 4096  # max characters per Telegram message


class NotifyError(Exception):
    pass


def booking_link(template: str, club: Club, day: date) -> str:
    return template.format(club_url=club.url, tenant_id=club.tenant_id, date=day.isoformat())


def format_price(slot: Slot) -> str:
    if slot.price_eur is None:
        return slot.price_text or "price n/a"
    if "EUR" not in slot.price_text.upper() and slot.price_text:
        return slot.price_text
    return f"€{slot.price_eur:.0f}" if slot.price_eur.is_integer() else f"€{slot.price_eur:.2f}"


def format_slot(slot: Slot, link: str) -> str:
    """'Free: Tue 28 Oct, 17:00-18:30, Court 2 indoor, €48. Book: <link>'"""
    star = "⭐ " if slot.preferred else ""
    return (f"{star}Free: {format_day(slot.start)}, {format_time(slot.start)}-{format_time(slot.end)}, "
            f"{slot.court_name} indoor, {format_price(slot)}. Book: {link}")


def build_messages(slots: list[Slot], club_name: str, link_for_day) -> list[str]:
    """One alert text (split into several if Telegram's size limit requires)."""
    if not slots:
        return []
    header = f"🎾 {len(slots)} new free court slot(s) at {club_name}"
    messages: list[str] = []
    current = header
    for slot in slots:
        line = format_slot(slot, link_for_day(slot.start.date()))
        if len(current) + 2 + len(line) > TELEGRAM_LIMIT:
            messages.append(current)
            current = f"{header} (continued)"
        current += "\n\n" + line
    messages.append(current)
    return messages


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout_s: float = 20.0):
        self._token = bot_token
        self.chat_id = chat_id
        self.timeout_s = timeout_s

    def _call(self, method: str, params: dict) -> dict:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        body = urllib.parse.urlencode(params).encode("utf-8")
        try:
            with urllib.request.urlopen(url, data=body, timeout=self.timeout_s) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Never include the URL in the message: it contains your bot token.
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise NotifyError(f"Telegram {method} failed: HTTP {exc.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise NotifyError(f"Telegram {method} failed: {getattr(exc, 'reason', exc)}") from None
        if not data.get("ok"):
            raise NotifyError(f"Telegram {method} failed: {data.get('description')}")
        return data

    def send(self, text: str) -> None:
        self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text[:TELEGRAM_LIMIT],
            "disable_web_page_preview": "true",
        })

    def bot_username(self) -> str:
        """Checks the token works; returns the bot's @username."""
        return str(self._call("getMe", {})["result"].get("username", ""))

    def recent_chats(self) -> list[tuple[str, str]]:
        """(chat_id, name) of people who recently messaged the bot; for setup."""
        data = self._call("getUpdates", {"timeout": "0"})
        chats: dict[str, str] = {}
        for update in data.get("result", []):
            chat = (update.get("message") or {}).get("chat") or {}
            if "id" in chat:
                name = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
                chats[str(chat["id"])] = name
        return list(chats.items())
