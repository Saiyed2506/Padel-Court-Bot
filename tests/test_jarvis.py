"""Offline tests for Jarvis: a fake Claude, no network, no cost. Run: py -m unittest -v"""

from __future__ import annotations

import copy
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

try:
    import anthropic
    from anthropic.types.beta import BetaMessage
except ImportError:  # the padel bot alone doesn't need the anthropic package
    anthropic = None

from padel_bot.timeutil import CentralEuropeanTime

logging.disable(logging.CRITICAL)

REPO = Path(__file__).resolve().parent.parent
TZ = CentralEuropeanTime()
NOW = datetime(2026, 11, 14, 22, 0, tzinfo=TZ)  # a Saturday


def message(content: list[dict], stop_reason: str = "end_turn", searches: int = 0):
    return BetaMessage.model_validate({
        "id": "msg", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0,
                  "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": 0}},
    })


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def thinking() -> dict:
    return {"type": "thinking", "thinking": "", "signature": "sig"}


def tool_use(name: str, args: dict, id_: str = "tu1") -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


class FakeClaude:
    """Stands in for anthropic.Anthropic: returns queued answers, records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeTelegram:
    def __init__(self, chat_id="42"):
        self.chat_id = chat_id
        self.sent: list[str] = []

    def send(self, text, **_):
        self.sent.append(text)

    send_long = send

    def typing(self):
        pass


@unittest.skipIf(anthropic is None, "anthropic package not installed (only Jarvis needs it)")
class JarvisTestCase(unittest.TestCase):
    def setUp(self):
        from jarvis.config import Settings
        from jarvis.store import Store

        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir, TZ)
        self.settings = Settings(your_name="Sam", city="Hamburg")

    def tearDown(self):
        self.tmp.cleanup()

    def brain(self, *responses, settings=None):
        from jarvis.brain import Brain

        self.claude = FakeClaude(*responses)
        return Brain(self.claude, settings or self.settings, self.store, self.dir)


class BrainTests(JarvisTestCase):
    def test_plain_answer_with_time_and_cost(self):
        brain = self.brain(message([thinking(), text("Good evening, Sam.")]))
        reply = brain.ask("hi", NOW)
        self.assertEqual(reply.text, "Good evening, Sam.")
        request = self.claude.requests[0]
        self.assertEqual(request["model"], "claude-opus-5-5")
        self.assertEqual(request["output_config"], {"effort": "medium"})
        self.assertEqual(request["fallbacks"], "default")
        self.assertIn("[Sat 14 Nov 2026, 22:00]\nhi", request["messages"][0]["content"][0]["text"])
        self.assertIn("Sam", request["system"])
        # 1000 input tokens * $4/M + 100 output * $20/M = $0.006
        self.assertAlmostEqual(self.store.spent(NOW.date()), 0.006)
        # the hidden reasoning block is kept unchanged in the history
        self.assertEqual(brain.conversation["messages"][1]["content"][0]["signature"], "sig")

    def test_tool_call_sets_reminder_and_history_is_append_only(self):
        brain = self.brain(
            message([thinking(), tool_use("set_reminder", {"text": "Call mum", "when": "2026-11-15 18:00",
                                                            "repeat": "none"})], "tool_use"),
            message([text("Done: Sun 15 Nov at 18:00.")]),
            message([text("You're welcome.")]),
        )
        self.assertEqual(brain.ask("remind me to call mum tomorrow at 6pm", NOW).text, "Done: Sun 15 Nov at 18:00.")
        brain.ask("thanks", NOW + timedelta(minutes=1))

        [reminder] = self.store.reminders()
        self.assertEqual((reminder.text, reminder.due), ("Call mum", datetime(2026, 11, 15, 18, 0, tzinfo=TZ)))
        result = self.claude.requests[1]["messages"][2]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"]), ("tool_result", "tu1"))
        self.assertNotIn("is_error", result)
        # every request repeats the previous one unchanged, plus new messages at the end
        for before, after in zip(self.claude.requests, self.claude.requests[1:]):
            self.assertEqual(after["messages"][:len(before["messages"])], before["messages"])
            self.assertEqual(after["system"], before["system"])
            self.assertEqual(after["tools"], before["tools"])

    def test_bad_tool_input_is_reported_to_claude(self):
        brain = self.brain(
            message([tool_use("set_reminder", {"text": "x", "when": "2026-11-01 10:00", "repeat": "none"})],
                    "tool_use"),
            message([text("That time has passed - when instead?")]),
        )
        brain.ask("remind me on 1 Nov", NOW)
        result = self.claude.requests[1]["messages"][2]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("in the past", result["content"])
        self.assertEqual(self.store.reminders(), [])

    def test_failed_call_takes_message_back(self):
        request = SimpleNamespace(method="POST", url="https://api.anthropic.com")
        brain = self.brain(anthropic.APIConnectionError(request=request), message([text("Back online.")]))
        self.store.add_note("A reminder fired.")
        with self.assertRaises(anthropic.APIConnectionError):
            brain.ask("hello?", NOW)
        self.assertEqual(brain.conversation["messages"], [])
        brain.ask("hello?", NOW)
        self.assertEqual(len(self.claude.requests[1]["messages"]), 1)
        self.assertIn("A reminder fired.", self.claude.requests[1]["messages"][0]["content"][0]["text"])

    def test_rejected_conversation_starts_fresh(self):
        response = SimpleNamespace(status_code=400, headers={}, request=None)
        error = anthropic.BadRequestError("messages: invalid", response=response, body=None)
        brain = self.brain(message([text("one")]), error, message([text("fresh")]))
        brain.ask("first", NOW)
        with self.assertRaises(anthropic.BadRequestError):
            brain.ask("second", NOW)
        brain.ask("second", NOW)
        self.assertEqual(len(self.claude.requests[2]["messages"]), 1)

    def test_budget_brake(self):
        from jarvis.brain import BudgetReached

        self.store.add_spend(NOW.date(), 1.0)
        brain = self.brain()
        with self.assertRaises(BudgetReached):
            brain.ask("hi", NOW)
        self.assertEqual(self.claude.requests, [])

    def test_new_conversation_after_silence_keeps_memories_and_recap(self):
        brain = self.brain(message([text("Noted, Omar it is.")]), message([text("Morning!")]))
        brain.ask("my padel partner is Omar", NOW)
        self.store.remember("Padel partner is Omar.", NOW)
        brain.ask("good morning", NOW + timedelta(hours=10))
        second = self.claude.requests[1]
        self.assertEqual(len(second["messages"]), 1)  # fresh conversation
        self.assertIn("#1: Padel partner is Omar.", second["system"])
        self.assertIn("You: Noted, Omar it is.", second["system"])

    def test_web_search_and_fallbacks_follow_settings(self):
        from dataclasses import replace

        brain = self.brain(message([text("ok")]), settings=replace(self.settings, model="claude-haiku-5-5",
                                                                        web_search=False))
        brain.ask("hi", NOW)
        request = self.claude.requests[0]
        self.assertNotIn("fallbacks", request)
        self.assertNotIn("web_search", [t["name"] for t in request["tools"]])
        self.assertNotIn("Search the web", request["system"])

    def test_refusal_and_cut_off_answers(self):
        brain = self.brain(message([], "refusal"), message([text("Part one")], "max_tokens"))
        self.assertIn("not able to help", brain.ask("x", NOW).text)
        self.assertIn("cut off", brain.ask("y", NOW).text)


class StoreTests(JarvisTestCase):
    def test_repeating_reminders(self):
        friday = datetime(2026, 11, 13, 7, 30, tzinfo=TZ)
        self.store.add_reminder("Vitamins", friday, "weekdays")
        self.store.add_reminder("Once", friday, "none")
        fired = self.store.pop_due_reminders(friday + timedelta(minutes=1))
        self.assertEqual([r.text for r in fired], ["Vitamins", "Once"])
        [left] = self.store.reminders()
        self.assertEqual(left.due, datetime(2026, 11, 16, 7, 30, tzinfo=TZ))  # skips the weekend

    def test_daily_reminder_keeps_wall_clock_time_over_summer_time_change(self):
        from jarvis.store import next_occurrence

        before = datetime(2026, 3, 28, 8, 0, tzinfo=TZ)  # day before clocks go forward
        after = next_occurrence(before, "daily", TZ)
        self.assertEqual((after.hour, after.minute, after.day), (8, 0, 29))
        utc = timezone.utc  # (subtracting two times with the same zone ignores the clock change)
        self.assertEqual(after.astimezone(utc) - before.astimezone(utc), timedelta(hours=23))

    def test_missed_repeats_fire_once(self):
        self.store.add_reminder("Water plants", NOW - timedelta(days=3), "daily")
        self.assertEqual(len(self.store.pop_due_reminders(NOW)), 1)
        self.assertGreater(self.store.reminders()[0].due, NOW)

    def test_memories(self):
        self.assertEqual(self.store.remember("Likes  espresso", NOW), 1)
        self.assertEqual(self.store.remember("likes espresso", NOW), 1)  # no duplicates
        self.assertEqual(self.store.forget(1), "Likes espresso")
        self.assertIsNone(self.store.forget(1))

    def test_broken_file_does_not_stop_jarvis(self):
        (self.dir / "memories.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.memories(), [])
        self.assertTrue((self.dir / "memories.json.broken").exists())


class AppTests(JarvisTestCase):
    def jarvis(self, *responses):
        from jarvis.app import Jarvis

        self.telegram = FakeTelegram()
        return Jarvis(self.telegram, self.brain(*responses), self.store, TZ, clock=lambda: NOW)

    def update(self, text, chat="42", sent=NOW):
        return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "date": int(sent.timestamp()), "text": text}}

    def test_only_the_owner_is_answered(self):
        jarvis = self.jarvis()
        jarvis.handle_update(self.update("hi", chat="999"))
        self.assertEqual((self.telegram.sent, self.claude.requests), ([], []))

    def test_old_messages_are_skipped(self):
        jarvis = self.jarvis()
        jarvis.handle_update(self.update("hi", sent=NOW - timedelta(hours=2)))
        self.assertEqual(self.telegram.sent, [])

    def test_shortcuts_use_no_ai(self):
        jarvis = self.jarvis()
        self.store.add_reminder("Stretch", NOW + timedelta(hours=1))
        jarvis.handle_update(self.update("/reminders"))
        jarvis.handle_update(self.update("/cost"))
        self.assertIn("Stretch", self.telegram.sent[0])
        self.assertIn("today: $0.00", self.telegram.sent[1])
        self.assertEqual(self.claude.requests, [])

    def test_reminder_fires_and_jarvis_knows_about_it(self):
        jarvis = self.jarvis(message([text("Glad you called her.")]))
        self.store.add_reminder("Call mum", NOW - timedelta(minutes=1))
        jarvis.fire_reminders()
        self.assertEqual(self.telegram.sent, ["⏰ Call mum"])
        jarvis.handle_update(self.update("done"))
        sent_text = self.claude.requests[0]["messages"][0]["content"][0]["text"]
        self.assertIn("delivered reminder #1: 'Call mum'", sent_text)
        self.assertEqual(self.telegram.sent[-1], "Glad you called her.")

    def test_problems_are_explained_not_crashing(self):
        request = SimpleNamespace(method="POST", url="https://api.anthropic.com")
        jarvis = self.jarvis(anthropic.APIConnectionError(request=request))
        jarvis.handle_update(self.update("hi"))
        self.assertIn("can't reach Anthropic", self.telegram.sent[0])

    def test_long_answers_are_split(self):
        from jarvis.app import JarvisTelegram

        telegram = JarvisTelegram("token", "42")
        chunks = []
        telegram.send = lambda t, **_: chunks.append(t)
        telegram.send_long("\n".join(["x" * 100] * 100))
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 4096 for c in chunks))


@unittest.skipIf(anthropic is None, "anthropic package not installed")
class SettingsTests(unittest.TestCase):
    def test_shipped_settings_load(self):
        from jarvis.config import load_settings

        settings = load_settings(REPO / "jarvis.toml")
        self.assertEqual((settings.model, settings.effort, settings.web_search), ("claude-opus-5-5", "medium", True))

    def test_bad_effort_is_explained(self):
        from jarvis.config import ConfigError, load_settings

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "jarvis.toml"
            path.write_text('[jarvis]\neffort = "turbo"\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
