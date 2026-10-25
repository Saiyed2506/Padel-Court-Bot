"""Remember which free slots you've already been told about.

Per watched day we store the set of matching slot keys seen last time. A slot
triggers a notification when it's in the new set but wasn't in the old one, so
a slot that gets booked and later frees up again alerts you a second time.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path


class SeenState:
    def __init__(self, path: Path):
        self.path = path
        self._days: dict[str, set[str]] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._days = {d: set(keys) for d, keys in raw.get("days", {}).items()}
            except (json.JSONDecodeError, AttributeError, TypeError):
                # A corrupt file is not worth crashing over; worst case you
                # get a few repeat alerts.
                self._days = {}

    def update_day(self, day: date, current_keys: set[str]) -> set[str]:
        """Store the current keys for `day` and return the ones that are new."""
        previous = self._days.get(day.isoformat(), set())
        self._days[day.isoformat()] = set(current_keys)
        return set(current_keys) - previous

    def forget_keys(self, day: date, keys: set[str]) -> None:
        """Undo part of update_day, e.g. when the alert for these keys failed."""
        self._days.get(day.isoformat(), set()).difference_update(keys)

    def forget_before(self, day: date) -> None:
        cutoff = day.isoformat()
        self._days = {d: keys for d, keys in self._days.items() if d >= cutoff}

    def save(self) -> None:
        data = {"days": {d: sorted(keys) for d, keys in sorted(self._days.items())}}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic, so a crash never leaves half a file
