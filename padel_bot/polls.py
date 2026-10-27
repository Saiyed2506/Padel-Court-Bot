"""'Who's in?' on court alerts: padel needs 4 players.

Each court alert (when there are only a few) gets ✅ I'm in / ❌ Not me
buttons. You and your approved friends see the same message, and every copy
is updated with who's in, so you know when it's worth booking.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from .notify import NotifyError
from .timeutil import format_day, format_time

log = logging.getLogger("padel_bot")

PLAYERS = 4
MAX_POLLS = 200


def poll_id(slot_key: str) -> str:
    return hashlib.sha1(slot_key.encode("utf-8")).hexdigest()[:8]


class Polls:
    def __init__(self, path: Path, notifier, friends):
        self.path = path
        self.notifier = notifier      # a Broadcaster
        self.friends = friends
        self._owner_name: str | None = None
        try:
            self.polls: dict[str, dict] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.polls = {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.polls, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def owner_name(self) -> str:
        if self._owner_name is None:
            try:
                self._owner_name = self.notifier.chat_first_name(self.notifier.chat_id) or "Host"
            except (NotifyError, AttributeError):
                return "Host"
        return self._owner_name

    owner_extra = None  # set by the bot: (pid, poll) -> extra button row for you (booking)

    def buttons(self, pid: str, day_iso: str, owner: bool, poll: dict | None = None):
        rows = [[("✅ I'm in", f"p:in:{pid}"), ("❌ Not me", f"p:out:{pid}")]]
        rows.append([("📌 I booked it", f"bk:c:{pid}")] + ([("🔕 Mute this day", f"md:{day_iso}")] if owner else []))
        if owner and self.owner_extra is not None and poll is not None:
            extra = self.owner_extra(pid, poll)
            if extra:
                rows.append(extra)
        return rows

    def slot(self, pid: str):
        """Rebuild the alerted slot (for booking it)."""
        from .rules import Slot

        poll = self.polls.get(pid)
        data = (poll or {}).get("slot")
        if not data:
            return None
        return Slot(data["resource_id"], data["court_name"], datetime.fromisoformat(data["start"]),
                    int(data["duration"]), data.get("price_eur"), data.get("price_text", ""),
                    bool(data.get("preferred")))

    def text(self, poll: dict) -> str:
        ins, outs = list(poll["in"].values()), list(poll["out"].values())
        if not ins and not outs:
            votes = "👥 Who's in? Tap below."
        else:
            votes = f"👥 In ({len(ins)}/{PLAYERS}): {', '.join(ins) or '-'}"
            if outs:
                votes += f"\n🙅 Not: {', '.join(outs)}"
            if len(ins) >= PLAYERS:
                votes += "\n🎉 Enough players - book it!"
        return f"{poll['text']}\n\n{votes}"

    def post(self, slot, base_text: str, silent: bool) -> None:
        """Send one court alert with voting buttons to you and your friends."""
        pid = poll_id(slot.key)
        day = slot.start.date().isoformat()
        label = (f"{format_day(slot.start)} {format_time(slot.start)}-{format_time(slot.end)}, "
                 f"{slot.court_name}")
        poll = {"text": base_text, "start": slot.start.isoformat(), "label": label,
                "slot": {"resource_id": slot.resource_id, "court_name": slot.court_name,
                         "start": slot.start.isoformat(), "duration": slot.duration,
                         "price_eur": slot.price_eur, "price_text": slot.price_text,
                         "preferred": slot.preferred},
                "msgs": {}, "in": {}, "out": {}}
        poll["msgs"] = self.notifier.broadcast(self.text(poll), buttons=self.buttons(pid, day, True, poll),
                                               friend_buttons=self.buttons(pid, day, False), silent=silent)
        self.polls[pid] = poll
        self.prune(datetime.now(slot.start.tzinfo))
        self._save()

    def vote(self, pid: str, chat_id: str, choice: str) -> str:
        poll = self.polls.get(pid)
        if poll is None:
            return "This alert is too old to vote on."
        owner = str(self.notifier.chat_id)
        if chat_id == owner:
            name = self.owner_name()
        elif chat_id in self.friends.approved:
            name = self.friends.approved[chat_id].split(" (@")[0]
        else:
            return "Not allowed"
        poll["in"].pop(chat_id, None)
        poll["out"].pop(chat_id, None)
        (poll["in"] if choice == "in" else poll["out"])[chat_id] = name
        self._save()
        text = self.text(poll)
        day = poll["start"][:10]
        for target, message_id in poll["msgs"].items():
            try:
                self.notifier.edit(message_id, text, buttons=self.buttons(pid, day, target == owner, poll),
                                   chat_id=target)
            except NotifyError as exc:
                log.warning("Could not update a 'who's in' message: %s", exc)
        count = len(poll["in"])
        return f"You're in ({count}/{PLAYERS})" if choice == "in" else "Noted - not this time"

    def prune(self, now: datetime) -> None:
        def start(p):
            try:
                return datetime.fromisoformat(p["start"])
            except (KeyError, ValueError):
                return now
        self.polls = {k: p for k, p in self.polls.items() if start(p) > now}
        if len(self.polls) > MAX_POLLS:
            keep = sorted(self.polls, key=lambda k: self.polls[k]["start"])[-MAX_POLLS:]
            self.polls = {k: self.polls[k] for k in keep}
