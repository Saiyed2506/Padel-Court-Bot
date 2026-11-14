"""Everything Jarvis keeps on disk, as small JSON files in jarvis_data/.

* memories.json  - facts about you ("my sister is called Aisha")
* reminders.json - things to tell you at a set time, optionally repeating
* notes.json     - things that happened while you weren't chatting
                   (e.g. a reminder that fired); Jarvis reads them next time
* usage.json     - estimated spend per day, for the daily budget brake
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

REPEATS = ("none", "daily", "weekdays", "weekly")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A damaged file must not stop Jarvis. Keep it for inspection, start empty.
        path.replace(path.with_suffix(path.suffix + ".broken"))
        return default


def write_json(path: Path, data) -> None:
    """Write via a temporary file, so a crash mid-write never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Reminder:
    id: int
    text: str
    due: datetime  # timezone-aware
    repeat: str = "none"

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "due": self.due.isoformat(), "repeat": self.repeat}

    @classmethod
    def from_dict(cls, data: dict) -> "Reminder":
        return cls(int(data["id"]), str(data["text"]), datetime.fromisoformat(data["due"]),
                   str(data.get("repeat", "none")))


def next_occurrence(due: datetime, repeat: str, tz: tzinfo) -> datetime:
    """The next time a repeating reminder fires, keeping the same wall-clock time
    across summer/winter time changes."""
    local = due.astimezone(tz).replace(tzinfo=None)
    if repeat == "daily":
        local += timedelta(days=1)
    elif repeat == "weekly":
        local += timedelta(days=7)
    elif repeat == "weekdays":
        local += timedelta(days=1)
        while local.weekday() >= 5:  # Saturday, Sunday
            local += timedelta(days=1)
    else:
        raise ValueError(f"not a repeating reminder: {repeat}")
    return local.replace(tzinfo=tz)


class Store:
    def __init__(self, data_dir: Path, tz: tzinfo):
        self.dir = data_dir
        self.tz = tz
        data_dir.mkdir(parents=True, exist_ok=True)

    # ---- memories -------------------------------------------------------
    @property
    def _memories_file(self) -> Path:
        return self.dir / "memories.json"

    def memories(self) -> list[dict]:
        return read_json(self._memories_file, [])

    def remember(self, fact: str, now: datetime) -> int:
        fact = " ".join(fact.split())
        if not fact:
            raise ValueError("nothing to remember")
        items = self.memories()
        for item in items:
            if item["fact"].lower() == fact.lower():
                return item["id"]  # already known
        new_id = max((m["id"] for m in items), default=0) + 1
        items.append({"id": new_id, "fact": fact, "saved": now.date().isoformat()})
        write_json(self._memories_file, items)
        return new_id

    def forget(self, memory_id: int) -> str | None:
        items = self.memories()
        kept = [m for m in items if m["id"] != memory_id]
        if len(kept) == len(items):
            return None
        write_json(self._memories_file, kept)
        return next(m["fact"] for m in items if m["id"] == memory_id)

    # ---- reminders ------------------------------------------------------
    @property
    def _reminders_file(self) -> Path:
        return self.dir / "reminders.json"

    def reminders(self) -> list[Reminder]:
        items = [Reminder.from_dict(r) for r in read_json(self._reminders_file, [])]
        return sorted(items, key=lambda r: r.due)

    def _save_reminders(self, items: list[Reminder]) -> None:
        write_json(self._reminders_file, [r.to_dict() for r in items])

    def add_reminder(self, text: str, due: datetime, repeat: str = "none") -> Reminder:
        if repeat not in REPEATS:
            raise ValueError(f"repeat must be one of {', '.join(REPEATS)}")
        if due.tzinfo is None:
            due = due.replace(tzinfo=self.tz)
        items = self.reminders()
        reminder = Reminder(max((r.id for r in items), default=0) + 1, " ".join(text.split()), due, repeat)
        items.append(reminder)
        self._save_reminders(items)
        return reminder

    def cancel_reminder(self, reminder_id: int) -> Reminder | None:
        items = self.reminders()
        found = next((r for r in items if r.id == reminder_id), None)
        if found:
            self._save_reminders([r for r in items if r.id != reminder_id])
        return found

    def pop_due_reminders(self, now: datetime) -> list[Reminder]:
        """Reminders whose time has come. One-off ones are removed, repeating
        ones move to their next time (missed repeats while the PC was off fire once)."""
        items = self.reminders()
        due = [r for r in items if r.due <= now]
        if not due:
            return []
        keep = [r for r in items if r.due > now]
        for r in due:
            if r.repeat != "none":
                nxt = r.due
                while nxt <= now:
                    nxt = next_occurrence(nxt, r.repeat, self.tz)
                keep.append(Reminder(r.id, r.text, nxt, r.repeat))
        self._save_reminders(keep)
        return due

    # ---- notes for the next conversation turn ----------------------------
    @property
    def _notes_file(self) -> Path:
        return self.dir / "notes.json"

    def add_note(self, text: str) -> None:
        notes = read_json(self._notes_file, [])
        notes.append(text)
        write_json(self._notes_file, notes[-20:])

    def take_notes(self) -> list[str]:
        notes = read_json(self._notes_file, [])
        if notes:
            write_json(self._notes_file, [])
        return notes

    # ---- Telegram position (which messages were already answered) -------
    def load_offset(self) -> int | None:
        return read_json(self.dir / "telegram_offset.json", None)

    def save_offset(self, offset: int) -> None:
        write_json(self.dir / "telegram_offset.json", offset)

    # ---- spending ---------------------------------------------------------
    @property
    def _usage_file(self) -> Path:
        return self.dir / "usage.json"

    def spent(self, day: date) -> float:
        return float(read_json(self._usage_file, {}).get(day.isoformat(), 0.0))

    def add_spend(self, day: date, usd: float) -> None:
        usage = read_json(self._usage_file, {})
        usage[day.isoformat()] = round(usage.get(day.isoformat(), 0.0) + usd, 6)
        # keep about two months of history
        write_json(self._usage_file, dict(sorted(usage.items())[-62:]))

    def spent_since(self, first_day: date) -> float:
        return sum(v for k, v in read_json(self._usage_file, {}).items() if k >= first_day.isoformat())
