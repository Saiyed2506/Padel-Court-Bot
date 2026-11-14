"""The abilities ("tools") Jarvis can use, and the code that carries them out.

How tools work: we describe each tool to Claude (name, what it does, what
inputs it takes). When Claude decides it needs one, it answers with a
"tool_use" request instead of text. Our code runs the tool and sends the
result back, and Claude continues. Claude never runs anything itself - it can
only ask, and only for the tools listed here.
"""

from __future__ import annotations

import json
from datetime import datetime

from .store import REPEATS, Store

WHEN_FORMAT = "%Y-%m-%d %H:%M"


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,  # Claude's inputs always match the schema exactly
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("remember",
          "Save a lasting fact about the user (people, preferences, plans, routines) so you still know it "
          "in future conversations. Use it whenever the user tells you something worth keeping, or asks "
          "you to remember something. One fact per call, written as a short sentence.",
          {"fact": {"type": "string", "description": "e.g. 'Plays padel on Tuesdays with Omar.'"}}),
    _tool("forget",
          "Delete a saved fact by its number, e.g. when it is wrong, outdated, or the user asks.",
          {"memory_id": {"type": "integer"}}),
    _tool("list_memories",
          "List all saved facts with their numbers.",
          {}),
    _tool("set_reminder",
          "Schedule a Telegram message to the user at a given local time. The message is sent even "
          "when the user is not chatting. Use repeat for routines (every day, every weekday, every week).",
          {"text": {"type": "string", "description": "What to remind about, written to the user."},
           "when": {"type": "string", "description": "Local date and time, format YYYY-MM-DD HH:MM"},
           "repeat": {"type": "string", "enum": list(REPEATS)}}),
    _tool("list_reminders",
          "List upcoming reminders with their numbers and times.",
          {}),
    _tool("cancel_reminder",
          "Delete a reminder by its number.",
          {"reminder_id": {"type": "integer"}}),
]


def web_search_tool(city: str, timezone_name: str = "Europe/Berlin") -> dict:
    """Anthropic's built-in web search. It runs on Anthropic's side; we only switch it on."""
    tool = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
    if city:
        tool["user_location"] = {"type": "approximate", "city": city, "timezone": timezone_name}
    return tool


def describe_reminder(r, tz) -> str:
    local = r.due.astimezone(tz)
    repeat = "" if r.repeat == "none" else f" (repeats {r.repeat})"
    return f"#{r.id} {local:%a %d %b %Y %H:%M}{repeat}: {r.text}"


class ToolBox:
    def __init__(self, store: Store):
        self.store = store

    def run(self, name: str, args: dict, now: datetime) -> tuple[str, bool]:
        """Runs one tool. Returns (result text for Claude, is_error)."""
        try:
            handler = getattr(self, f"_{name}")
        except AttributeError:
            return f"Unknown tool {name}", True
        try:
            return handler(now=now, **args), False
        except (ValueError, TypeError, KeyError) as exc:
            # Tell Claude what went wrong so it can fix its input or explain.
            return f"Error: {exc}", True

    def _remember(self, now, fact: str) -> str:
        return f"Saved as memory #{self.store.remember(fact, now)}."

    def _forget(self, now, memory_id: int) -> str:
        fact = self.store.forget(int(memory_id))
        return f"Deleted memory #{memory_id}: {fact}" if fact else f"There is no memory #{memory_id}."

    def _list_memories(self, now) -> str:
        items = self.store.memories()
        if not items:
            return "No saved memories."
        return "\n".join(f"#{m['id']}: {m['fact']}" for m in items)

    def _set_reminder(self, now, text: str, when: str, repeat: str) -> str:
        try:
            due = datetime.strptime(when.strip(), WHEN_FORMAT).replace(tzinfo=self.store.tz)
        except ValueError:
            raise ValueError(f"'when' must look like 2026-11-20 18:30, got {when!r}") from None
        if due <= now:
            raise ValueError(f"{when} is in the past (it is now {now:{WHEN_FORMAT}}). Ask the user or pick a future time.")
        reminder = self.store.add_reminder(text, due, repeat)
        return f"Reminder set: {describe_reminder(reminder, self.store.tz)}"

    def _list_reminders(self, now) -> str:
        items = self.store.reminders()
        if not items:
            return "No reminders."
        return "\n".join(describe_reminder(r, self.store.tz) for r in items)

    def _cancel_reminder(self, now, reminder_id: int) -> str:
        r = self.store.cancel_reminder(int(reminder_id))
        return f"Cancelled {describe_reminder(r, self.store.tz)}" if r else f"There is no reminder #{reminder_id}."


def tool_input(raw) -> dict:
    """Tool inputs arrive as a dict; parse defensively if they ever come as text."""
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)
