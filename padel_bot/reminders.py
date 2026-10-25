"""'📌 I booked it' -> a reminder before free cancellation ends.

The club allows free cancellation up to 24h before a game (configurable).
The reminder comes 48h before the game, i.e. 24h before that deadline, so
there's a full day to cancel if plans change.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from .notify import NotifyError
from .timeutil import format_day, format_time

log = logging.getLogger("padel_bot")

REMIND_BEFORE_DEADLINE = timedelta(hours=24)


def _when(dt: datetime) -> str:
    return f"{format_day(dt)} {format_time(dt)}"


class Reminders:
    def __init__(self, path: Path, notifier, free_cancel_min: int):
        self.path = path
        self.notifier = notifier
        self.free_cancel = timedelta(minutes=free_cancel_min)
        try:
            self.items: list[dict] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.items = []

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.items, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, chat_id: str, key: str, label: str, start: datetime, now: datetime) -> str:
        """Remember a booking; returns the toast to show."""
        if start <= now:
            return "That game has already started."
        if any(i["chat_id"] == chat_id and i["key"] == key for i in self.items):
            return "Already noted 📌"
        deadline = start - self.free_cancel
        remind_at = deadline - REMIND_BEFORE_DEADLINE
        if deadline <= now:
            note = "Noted 📌 - free cancellation has already ended for this one."
            remind_at = None
        elif remind_at <= now:
            remind_at = now  # booked late: remind right away with the exact deadline
            note = f"Noted 📌 - free cancellation ends {_when(deadline)}."
        else:
            note = f"Noted 📌 - I'll remind you {_when(remind_at)}, a day before free cancellation ends."
        self.items.append({"chat_id": chat_id, "key": key, "label": label, "start": start.isoformat(),
                           "deadline": deadline.isoformat(),
                           "remind_at": remind_at.isoformat() if remind_at else None, "sent": remind_at is None})
        self._save()
        return note

    def for_chat(self, chat_id: str) -> list[dict]:
        return [i for i in self.items if i["chat_id"] == chat_id]

    def remove(self, chat_id: str, index: int) -> dict | None:
        mine = self.for_chat(chat_id)
        if not 0 <= index < len(mine):
            return None
        self.items.remove(mine[index])
        self._save()
        return mine[index]

    def due(self, now: datetime, silent: bool = False) -> int:
        """Send reminders whose time has come; drop games that are over."""
        sent = 0
        for item in self.items:
            if item["sent"] or not item["remind_at"] or datetime.fromisoformat(item["remind_at"]) > now:
                continue
            deadline = datetime.fromisoformat(item["deadline"])
            hours = max(0, int((deadline - now).total_seconds() // 3600))
            text = (f"⏰ Reminder: you booked {item['label']}.\n"
                    f"Free cancellation ends {_when(deadline)} (in about {hours}h).\n"
                    "Still playing? If not, cancel in the Playtomic app before then.")
            try:
                self.notifier.send(text, chat_id=item["chat_id"], **({"silent": True} if silent else {}))
                item["sent"] = True
                sent += 1
            except NotifyError as exc:
                log.warning("Could not send a reminder: %s", exc)
        before = len(self.items)
        self.items = [i for i in self.items if datetime.fromisoformat(i["start"]) > now]
        if sent or len(self.items) != before:
            self._save()
        return sent
