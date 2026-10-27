"""Friends who receive your alerts.

A friend opens your bot in Telegram and taps Start; you get an Approve /
Decline button. Approved friends receive the same court and event alerts as
you, but can't change anything. Declined chats are ignored from then on.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .notify import NotifyError

log = logging.getLogger("padel_bot")

MAX_PENDING = 20  # ignore floods of strangers


class Friends:
    def __init__(self, path: Path):
        self.path = path
        raw = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.approved: dict[str, str] = dict(raw.get("approved", {}))
        self.pending: dict[str, str] = dict(raw.get("pending", {}))
        self.blocked: set[str] = set(raw.get("blocked", []))

    def save(self) -> None:
        data = {"approved": self.approved, "pending": self.pending, "blocked": sorted(self.blocked)}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def status(self, chat_id: str) -> str:
        if chat_id in self.approved:
            return "approved"
        if chat_id in self.blocked:
            return "blocked"
        if chat_id in self.pending:
            return "pending"
        return "new"

    def request(self, chat_id: str, name: str) -> bool:
        """Add a join request. False if too many are already waiting."""
        if len(self.pending) >= MAX_PENDING:
            return False
        self.pending[chat_id] = name
        self.save()
        return True

    def approve(self, chat_id: str) -> str | None:
        name = self.pending.pop(chat_id, None)
        if name is not None:
            self.approved[chat_id] = name
            self.save()
        return name

    def decline(self, chat_id: str) -> str | None:
        name = self.pending.pop(chat_id, None)
        self.blocked.add(chat_id)
        self.save()
        return name

    def remove(self, chat_id: str) -> str | None:
        name = self.approved.pop(chat_id, None)
        self.save()
        return name


class Broadcaster:
    """Wraps the Telegram notifier: `send` goes to you only, `broadcast` to you
    and every approved friend. A friend's failure never blocks your alert."""

    def __init__(self, notifier, friends: Friends):
        self.notifier = notifier
        self.friends = friends
        self.chat_id = notifier.chat_id

    def __getattr__(self, name):  # get_updates, delete_message, ... go straight through
        return getattr(self.notifier, name)

    def send(self, text: str, **kwargs):
        return self.notifier.send(text, **kwargs)

    def broadcast(self, text: str, *, buttons=None, friend_buttons=None, silent: bool = False) -> dict[str, int]:
        """Returns {chat_id: message_id} of every copy sent."""
        extra = {"silent": True} if silent else {}
        sent = {str(self.chat_id): self.notifier.send(text, buttons=buttons, **extra)}  # raises if *your* alert fails
        for chat_id, name in list(self.friends.approved.items()):
            try:
                sent[chat_id] = self.notifier.send(text, buttons=friend_buttons, chat_id=chat_id, **extra)
            except NotifyError as exc:
                log.warning("Could not alert friend %s: %s", name, exc)
        return sent
