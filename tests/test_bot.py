"""Offline tests: sample Playtomic replies, no network. Run: py -m unittest -v"""

from __future__ import annotations

import logging
from dataclasses import replace as _replace

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from padel_bot.config import ConfigError, load_dotenv, load_settings
from padel_bot.notify import NotifyError, build_messages, format_slot
from padel_bot.playtomic import (Club, Court, PlaytomicError, load_or_fetch_club,
                                 normalize_club_url, parse_club_page, load_club)
from padel_bot.rules import Slot, find_matches, parse_availability, parse_price
from padel_bot.state import SeenState
from padel_bot.timeutil import CentralEuropeanTime, local_day_bounds_utc
from padel_bot.watcher import Watcher, in_quiet_hours

logging.disable(logging.CRITICAL)  # keep test output readable

REPO = Path(__file__).resolve().parent.parent
TZ = CentralEuropeanTime()

COURTS = {
    "c1": Court("c1", "Court 1", indoor=True, size="double"),
    "c2": Court("c2", "Court 2", indoor=True, size="double"),
    "single": Court("single", "Single 1", indoor=True, size="single"),
    "out": Court("out", "Outdoor 5", indoor=False, size="double"),
}


CLUB = Club("T1", "padel amigos", "Halstenbek", tuple(COURTS.values()), "https://playtomic.com/clubs/padel-amigos")


def settings(tmp: Path, extra: str = ""):
    text = (REPO / "config.toml").read_text(encoding="utf-8").replace('url = ""', f'url = "{CLUB.url}"')
    text = text.replace("min_notice_hours = 48", "min_notice_hours = 0")  # tests use near-term slots
    path = tmp / "config.toml"
    path.write_text(text + extra, encoding="utf-8")
    return load_settings(path)


def avail(resource_id: str, day: str, *slots: tuple[str, int, str]) -> dict:
    return {"resource_id": resource_id, "start_date": day,
            "slots": [{"start_time": t, "duration": d, "price": p} for t, d, p in slots]}


class TimeTests(unittest.TestCase):
    def test_fallback_timezone_matches_berlin_rules(self):
        summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).astimezone(TZ)
        winter = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc).astimezone(TZ)
        self.assertEqual(summer.hour, 14)
        self.assertEqual(winter.hour, 13)
        # DST ends Sunday 25 Oct 2026 at 01:00 UTC.
        self.assertEqual(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).astimezone(TZ).hour, 2)
        self.assertEqual(datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc).astimezone(TZ).hour, 2)
        self.assertEqual(datetime(2026, 10, 25, 2, 30, tzinfo=timezone.utc).astimezone(TZ).hour, 3)

    def test_fallback_agrees_with_zoneinfo_when_available(self):
        try:
            from zoneinfo import ZoneInfo
            berlin = ZoneInfo("Europe/Berlin")
        except Exception:
            self.skipTest("no system tz database")
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for _ in range(0, 365 * 24 * 2, 7):
            self.assertEqual(t.astimezone(TZ).replace(tzinfo=None), t.astimezone(berlin).replace(tzinfo=None), t)
            t += timedelta(minutes=30 * 7)

    def test_day_bounds_in_utc(self):
        start, end = local_day_bounds_utc(date(2026, 11, 3), TZ)  # winter: UTC+1
        self.assertEqual(start, datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 3, 22, 59, 59, tzinfo=timezone.utc))


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = settings(Path(self.tmp.name))
        self.now = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)  # Monday morning

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_price(self):
        self.assertEqual(parse_price("48 EUR"), 48.0)
        self.assertEqual(parse_price("48,50 EUR"), 48.5)
        self.assertIsNone(parse_price(""))

    def test_weekday_window_courts_and_preferred(self):
        # Tue 3 Nov 2026 is winter time: local = UTC + 1.
        raw = [
            avail("c1", "2026-11-03",
                  ("15:00:00", 90, "48 EUR"),   # 16:00-17:30 ok
                  ("15:30:00", 90, "48 EUR"),   # 16:30 preferred
                  ("15:30:00", 60, "32 EUR"),   # wrong duration
                  ("17:30:00", 90, "48 EUR"),   # 18:30-20:00 ok
                  ("18:00:00", 90, "48 EUR"),   # 19:00-20:30 ok (last start)
                  ("18:30:00", 90, "48 EUR"),   # 19:30-21:00 too late
                  ("14:30:00", 90, "48 EUR")),  # 15:30 too early
            avail("out", "2026-11-03", ("16:00:00", 90, "40 EUR")),     # outdoor
            avail("single", "2026-11-03", ("16:00:00", 90, "30 EUR")),  # single court
            avail("unknown", "2026-11-03", ("16:00:00", 90, "30 EUR")), # not in court list
        ]
        slots = parse_availability(raw, COURTS, TZ)
        matches = find_matches(slots, COURTS, self.s, self.now, target_day=date(2026, 11, 3))
        got = [(m.start.strftime("%H:%M"), m.court_name, m.preferred) for m in matches]
        self.assertEqual(got, [("16:30", "Court 1", True), ("16:00", "Court 1", False), ("18:30", "Court 1", False),
                               ("19:00", "Court 1", False)])

    def test_local_time_mode(self):
        raw = [avail("c1", "2026-11-03", ("16:30:00", 90, "48 EUR"))]
        utc_slot = parse_availability(raw, COURTS, TZ)[0]
        local_slot = parse_availability(raw, COURTS, TZ, times_are_utc=False)[0]
        self.assertEqual(utc_slot.start.strftime("%H:%M"), "17:30")
        self.assertEqual(local_slot.start.strftime("%H:%M"), "16:30")
        self.assertEqual(local_slot.key, "c1|2026-11-03T15:30|90")

    def test_weekend_window(self):
        raw = [avail("c2", "2026-10-31",  # Saturday, winter time
                     ("08:00:00", 90, "50 EUR"),   # 09:00 too early
                     ("08:30:00", 90, "50 EUR"),   # 09:30-11:00 ok
                     ("10:00:00", 90, "50 EUR"),   # 11:00-12:30 ok
                     ("10:30:00", 90, "50 EUR"))]  # 11:30-13:00 too late
        matches = find_matches(parse_availability(raw, COURTS, TZ), COURTS, self.s, self.now)
        self.assertEqual([m.start.strftime("%H:%M") for m in matches], ["09:30", "11:00"])

    def test_past_slots_and_other_days_ignored(self):
        raw = [avail("c1", "2026-10-26", ("15:30:00", 90, "48 EUR"))]  # Mon 16:30
        slots = parse_availability(raw, COURTS, TZ)
        late = datetime(2026, 10, 26, 17, 0, tzinfo=TZ)
        self.assertEqual(find_matches(slots, COURTS, self.s, late), [])
        self.assertEqual(find_matches(slots, COURTS, self.s, self.now, target_day=date(2026, 10, 27)), [])
        self.assertEqual(len(find_matches(slots, COURTS, self.s, self.now)), 1)

    def test_notification_text(self):
        slot = Slot("c2", "Court 2", datetime(2026, 10, 27, 17, 0, tzinfo=TZ), 90, 48.0, "48 EUR", preferred=True)
        self.assertEqual(format_slot(slot, "https://x"),
                         "⭐ Free: Tue 27 Oct, 17:00-18:30, Court 2 indoor, €48. Book: https://x")
        many = [_replace(slot, start=slot.start + timedelta(minutes=30 * i)) for i in range(200)]
        messages = build_messages(many, "Club", lambda d: "https://x")
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= 4096 for m in messages))


class ConfigTests(unittest.TestCase):
    def test_shipped_config_loads(self):
        s = load_settings(REPO / "config.toml", require_club=False)
        self.assertEqual(s.booking_url, "{club_url}?date={date}")
        self.assertTrue(s.api_times_utc)
        self.assertEqual((s.booking.max_price_eur, s.booking.players, s.booking.max_per_day), (70, 4, 1))
        self.assertEqual(s.booking.payment_methods, ("MERCHANT_WALLET", "CREDIT_CARD"))
        self.assertEqual(s.days_ahead, 14)

    def test_missing_club_is_explained(self):
        with self.assertRaisesRegex(ConfigError, "START-HERE"):
            load_settings(REPO / "config.toml")


    def test_polling_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = (REPO / "config.toml").read_text(encoding="utf-8").replace("near_interval_minutes = 5", "near_interval_minutes = 0.5")
            path = Path(tmp) / "config.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "at least 2 minutes"):
                load_settings(path, require_club=False)

    def test_dotenv(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('# comment\nPADEL_TEST_A="hello"\nexport PADEL_TEST_B=world\n', encoding="utf-8")
            os.environ["PADEL_TEST_B"] = "already-set"
            try:
                load_dotenv(env)
                self.assertEqual(os.environ["PADEL_TEST_A"], "hello")
                self.assertEqual(os.environ["PADEL_TEST_B"], "already-set")
            finally:
                os.environ.pop("PADEL_TEST_A", None)
                os.environ.pop("PADEL_TEST_B", None)

    def test_gitignore_excludes_env(self):
        self.assertIn(".env", (REPO / ".gitignore").read_text().splitlines())


class FakeClient:
    def __init__(self):
        self.replies: dict[str, list[dict] | Exception] = {}
        self.calls: list[str] = []

    def get_availability(self, tenant_id, day):
        assert tenant_id == "T1"
        day = day.isoformat()
        self.calls.append(day)
        reply = self.replies.get(day, [])
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []
        self.outbox: list[dict] = []
        self.fail = False

    def send(self, text, buttons=None, keyboard=None, chat_id=None, silent=False):
        if self.fail:
            raise NotifyError("offline")
        self.sent.append(text)
        self.outbox.append({"text": text, "buttons": buttons, "keyboard": keyboard, "chat_id": chat_id,
                            "silent": silent})
        return len(self.outbox)

    def edit(self, message_id, text, buttons=None, chat_id=None):
        self.edits = getattr(self, "edits", [])
        self.edits.append({"id": message_id, "text": text, "buttons": buttons})

    def answer_callback(self, callback_id, text=""):
        self.toasts = getattr(self, "toasts", [])
        self.toasts.append(text)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = settings(Path(self.tmp.name))
        self.now = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)  # Monday
        self.client = FakeClient()
        self.notifier = FakeNotifier()
        self.watcher = Watcher(self.s, self.client, CLUB, self.notifier, SeenState(self.s.state_file), TZ,
                               None,
                               clock=lambda: self.now, sleep=lambda s: None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_alerts_once_then_again_after_rebooking_frees_it(self):
        self.client.replies["2026-10-27"] = [avail("c2", "2026-10-27", ("16:00:00", 90, "48 EUR"))]
        self.assertEqual(len(self.watcher.run_round()), 1)
        self.assertIn("Court 2", self.notifier.sent[0])

        self.now += timedelta(minutes=5)
        self.assertEqual(self.watcher.run_round(), [])          # still free: no repeat
        self.client.replies["2026-10-27"] = []
        self.now += timedelta(minutes=5)
        self.watcher.run_round()                                 # booked by someone
        self.client.replies["2026-10-27"] = [avail("c2", "2026-10-27", ("16:00:00", 90, "48 EUR"))]
        self.now += timedelta(minutes=5)
        self.assertEqual(len(self.watcher.run_round()), 1)       # cancelled -> new alert
        self.assertEqual(len(self.notifier.sent), 2)

    def test_state_survives_restart(self):
        self.client.replies["2026-10-27"] = [avail("c2", "2026-10-27", ("16:00:00", 90, "48 EUR"))]
        self.watcher.run_round()
        again = Watcher(self.s, self.client, CLUB, self.notifier, SeenState(self.s.state_file), TZ,
                        None, clock=lambda: self.now, sleep=lambda s: None)
        self.assertEqual(again.run_round(), [])

    def test_failed_telegram_send_retries_next_round(self):
        self.client.replies["2026-10-27"] = [avail("c2", "2026-10-27", ("16:00:00", 90, "48 EUR"))]
        self.notifier.fail = True
        self.watcher.run_round()
        self.notifier.fail = False
        self.now += timedelta(minutes=5)
        self.assertEqual(len(self.watcher.run_round()), 1)

    def test_polling_schedule(self):
        self.watcher.run_round()
        self.assertEqual(len(self.client.calls), 15)            # today + 14 days
        self.client.calls.clear()
        self.now += timedelta(minutes=5)
        self.watcher.run_round()
        # Days 0-3 plus the newest day (14) are re-checked; days 4-13 wait 20 min.
        self.assertEqual(len(self.client.calls), 5)
        self.assertIn("2026-11-09", self.client.calls)
        for _ in range(3):
            self.client.calls.clear()
            self.now += timedelta(minutes=5)
            self.watcher.run_round()
        self.assertEqual(len(self.client.calls), 15)

    def test_errors_back_off_and_alert(self):
        for day in range(15):
            self.client.replies[(date(2026, 10, 26) + timedelta(days=day)).isoformat()] = PlaytomicError("HTTP 503", status=503)
        for _ in range(self.s.alert_after_failed_rounds):
            self.watcher.run_round()
            self.now += timedelta(minutes=5)
        self.assertGreater(self.watcher.backoff_s, 0)
        self.assertEqual(sum("⚠️" in m for m in self.notifier.sent), 1)
        self.client.replies.clear()
        self.watcher.run_round()
        self.assertEqual(self.watcher.backoff_s, 0)
        self.assertIn("✅", self.notifier.sent[-1])

    def test_rate_limit_stops_round(self):
        self.client.replies["2026-10-26"] = PlaytomicError("HTTP 429", status=429)
        self.watcher.run_round()
        self.assertEqual(len(self.client.calls), 1)

    def test_quiet_hours(self):
        q = (23 * 60, 7 * 60)
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 26, 23, 30, tzinfo=TZ), q))
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 26, 6, 59, tzinfo=TZ), q))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 26, 7, 0, tzinfo=TZ), q))


class ParseClubTests(unittest.TestCase):
    PAGE = """<html><head></head><body><div id="__next"></div>
<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"tenant": {
 "tenant_id": "t1", "tenant_name": "padel amigos Hamburg-Halstenbek", "slug": "padel-amigos",
 "address": {"street": "Str 1", "postal_code": "25469", "city": "Halstenbek", "timezone": "Europe/Berlin"},
 "resources": [
  {"resourceId": "r1", "name": "Court 1", "features": ["indoor", "double", "panoramic"], "sport": "PADEL"},
  {"resourceId": "r5", "name": "Court 5", "features": ["outdoor", "double"], "sport": "PADEL"},
  {"resourceId": "r9", "name": "Single", "features": ["indoor", "single"], "sport": "PADEL"},
  {"resourceId": "t1", "name": "Tennis 1", "features": ["outdoor"], "sport": "TENNIS"}]}}}}</script>
</body></html>"""

    def test_parse_club_page(self):
        club = parse_club_page(self.PAGE, "https://playtomic.com/clubs/padel-amigos")
        self.assertEqual(club.tenant_id, "t1")
        self.assertEqual(club.address, "Str 1, 25469, Halstenbek")
        self.assertEqual(club.courts, (Court("r1", "Court 1", True, "double"),
                                       Court("r5", "Court 5", False, "double"),
                                       Court("r9", "Single", True, "single")))

    def test_parse_app_router_page(self):
        # Newer Next.js pages stream their data as escaped JavaScript strings.
        import json as _json
        tenant = {"slug": "padel-amigos", "address": {"city": "Halstenbek"}, "tenant_id": "t2",
                  "tenant_name": "padel amigos", "resources": [
                      {"properties": {"resource_type": "indoor", "resource_size": "double"},
                       "resource_id": "r1", "name": "Court 1", "sport_id": "PADEL"}]}
        flight = '5:["$","div",null,{"children":' + _json.dumps({"tenant": tenant}) + '}]\n'
        half = len(flight) // 2
        html = ("<html><body><script>self.__next_f.push([1," + _json.dumps(flight[:half]) + "])</script>"
                "<script>self.__next_f.push([1," + _json.dumps(flight[half:]) + "])</script></body></html>")
        club = parse_club_page(html, "u")
        self.assertEqual(club.tenant_id, "t2")
        self.assertEqual(club.courts, (Court("r1", "Court 1", True, "double"),))

    def test_failure_describes_page(self):
        with self.assertRaisesRegex(PlaytomicError, "title 'Padel Amigos'.*flight_chunks=0"):
            parse_club_page("<html><title>Padel Amigos</title>tenant_id</html>", "u")

    def test_page_without_data(self):
        with self.assertRaises(PlaytomicError):
            parse_club_page("<html>blocked</html>", "https://playtomic.com/clubs/x")

    def test_normalize_club_url(self):
        self.assertEqual(normalize_club_url(" playtomic.com/clubs/padel-amigos?date=2026-10-27#top "),
                         "https://playtomic.com/clubs/padel-amigos")
        for good in ("https://playtomic.com/de/clubs/padel-amigos-hamburg-halstenbek",
                     "https://www.playtomic.com/en-gb/clubs/padel-amigos"):
            self.assertEqual(normalize_club_url(good), good)
        for bad in ("https://example.com/clubs/x", "https://playtomic.com/", "hello"):
            with self.assertRaises(ValueError):
                normalize_club_url(bad)

    def test_club_is_cached(self):
        class Client:
            calls = 0
            def get_club_page(self, url):
                Client.calls += 1
                return parse_club_page(ParseClubTests.PAGE, url)
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "club.json"
            url = "https://playtomic.com/clubs/padel-amigos"
            first = load_or_fetch_club(Client(), url, cache)
            second = load_or_fetch_club(Client(), url, cache)
            self.assertEqual(first, second)
            self.assertEqual(Client.calls, 1)
            load_or_fetch_club(Client(), url + "-other", cache)  # different club: fetch again
            self.assertEqual(Client.calls, 2)


if __name__ == "__main__":
    unittest.main()


class SetupWizardTests(unittest.TestCase):
    def test_write_env_values_keeps_other_lines(self):
        from padel_bot.setup_wizard import write_env_values
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# comment\nTELEGRAM_BOT_TOKEN=\nOTHER=1\n", encoding="utf-8")
            write_env_values(env, {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"})
            self.assertEqual(env.read_text(encoding="utf-8"),
                             "# comment\nTELEGRAM_BOT_TOKEN=123:abc\nOTHER=1\nTELEGRAM_CHAT_ID=42\n")

    def test_set_club_url(self):
        from padel_bot.setup_wizard import set_club_url
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text((REPO / "config.toml").read_text(encoding="utf-8"), encoding="utf-8")
            set_club_url(path, "https://playtomic.com/clubs/abc")
            self.assertEqual(load_settings(path).club_url, "https://playtomic.com/clubs/abc")

    def test_set_booking_url(self):
        from padel_bot.setup_wizard import set_booking_url
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text((REPO / "config.toml").read_text(encoding="utf-8"), encoding="utf-8")
            set_booking_url(path, "https://playtomic.com/clubs/padel-amigos?x=1&y=\\2")
            self.assertEqual(load_settings(path, require_club=False).booking_url,
                             "https://playtomic.com/clubs/padel-amigos?x=1&y=\\2")

    def test_full_wizard_flow(self):
        from unittest import mock
        from padel_bot import setup_wizard
        from padel_bot.playtomic import Club

        token = "123456789:" + "A" * 35
        telegram_calls = []

        def fake_call(self, method, params):
            telegram_calls.append((method, params))
            if method == "getMe":
                return {"ok": True, "result": {"username": "padel_test_bot"}}
            if method == "getUpdates":
                return {"ok": True, "result": [{"message": {"chat": {"id": 42, "first_name": "Me"}}}]}
            return {"ok": True}

        def fake_page(self, url):
            return Club("T1", "padel amigos Hamburg-Halstenbek", "Halstenbek",
                        tuple(COURTS.values()) + (Court("c3", "Court 3", True, "double"),
                                                  Court("c4", "Court 4", True, "double")), url)
        answers = iter(["not a token", token, "", "https://example.com/x",
                        "https://playtomic.com/clubs/padel-amigos?date=2026-10-25", "y", "y"])
        checked = []
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text((REPO / "config.toml").read_text(encoding="utf-8"), encoding="utf-8")
            with mock.patch("builtins.input", lambda prompt="": next(answers)), \
                 mock.patch("builtins.print"), \
                 mock.patch.object(setup_wizard.TelegramNotifier, "_call", fake_call), \
                 mock.patch.object(setup_wizard.PlaytomicClient, "get_club_page", fake_page), \
                 mock.patch.dict("os.environ", {}, clear=False):
                self.assertEqual(setup_wizard.run_setup(config, lambda days: checked.append(days)), 0)
            env = (Path(tmp) / ".env").read_text(encoding="utf-8")
            self.assertIn(f"TELEGRAM_BOT_TOKEN={token}", env)
            self.assertIn("TELEGRAM_CHAT_ID=42", env)
            self.assertEqual(load_settings(config).club_url, "https://playtomic.com/clubs/padel-amigos")
            self.assertEqual(load_club(Path(tmp) / "club.json").tenant_id, "T1")
        self.assertEqual(checked, [1])
        sent = [p for m, p in telegram_calls if m == "sendMessage"]
        self.assertEqual(sent[0]["chat_id"], "42")
        self.assertIn("https://playtomic.com/clubs/padel-amigos?date=", sent[0]["text"])

    def test_rerun_keeps_working_telegram(self):
        from unittest import mock
        from padel_bot import setup_wizard
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "42"}), \
             mock.patch.object(setup_wizard.TelegramNotifier, "bot_username", lambda self: "b"), \
             mock.patch("builtins.input", lambda prompt="": ""), mock.patch("builtins.print"):
            notifier = setup_wizard.setup_telegram(Path("unused.env"))
        self.assertEqual(notifier.chat_id, "42")


class WishTests(unittest.TestCase):
    def test_parse_days(self):
        from padel_bot.wishes import parse_days
        self.assertEqual(parse_days("weekdays"), frozenset(["mon", "tue", "wed", "thu", "fri"]))
        self.assertEqual(parse_days("sat,sun"), frozenset(["sat", "sun"]))
        self.assertEqual(parse_days("fri-mon"), frozenset(["fri", "sat", "sun", "mon"]))
        self.assertEqual(parse_days("Di"), frozenset(["tue"]))  # German
        self.assertEqual(parse_days("tuesday"), frozenset(["tue"]))

    def test_parse_wish(self):
        from padel_bot.wishes import WishError, describe, parse_wish
        exact = parse_wish("tue 17:00")
        self.assertEqual((exact.start_min, exact.end_min, exact.durations), (17 * 60, 18 * 60 + 30, frozenset({90})))
        self.assertEqual(exact.preferred_starts, frozenset({17 * 60}))
        self.assertEqual(describe(exact), "Tue 17:00-18:30, 90 min")
        ranged = parse_wish("mon-fri 16:00-19:00 prefer 16:30,17:00")
        self.assertEqual(describe(ranged), "Mon-Fri 16:00-20:30 (starts 16:00-19:00), 90 min, ⭐ 16:30 17:00")
        self.assertEqual(parse_wish("sat 10:00 120").durations, frozenset({120}))
        for bad in ("tue", "xyz 17:00", "tue 25:00", "tue 19:00-17:00", "tue 17:00 abc",
                    "mon 16:00-17:00 prefer 17:30", "sun 23:30", "31.02.2026 16:00", "01.01.2020 16:00"):
            with self.assertRaises(WishError, msg=bad):
                parse_wish(bad)

    def test_date_wish(self):
        from padel_bot.wishes import describe, looks_like_date_wish, parse_wish
        today = date(2026, 10, 27)
        for text in ("10.11.2026 16:00-19:30", "10.11.2026 between 16:00 - 19:30", "10.11. 16:00 bis 19:30",
                     "2026-11-10 16:00-19:30"):
            w = parse_wish(text, today)
            self.assertEqual((w.on_date, w.start_min, w.end_min), (date(2026, 11, 10), 16 * 60, 21 * 60), text)
        self.assertEqual(describe(parse_wish("10.11.2026 16:00-19:30", today)),
                         "Tue 10 Nov (once) 16:00-21:00 (starts 16:00-19:30), 90 min")
        self.assertEqual(parse_wish("05.01 10:00", today).on_date, date(2027, 1, 5))   # next January
        self.assertTrue(looks_like_date_wish("10.11.2026 16:00-19:30"))
        self.assertFalse(looks_like_date_wish("hello 10.11"))

    def test_wish_store_round_trip(self):
        from padel_bot.wishes import WishStore, parse_wish
        with tempfile.TemporaryDirectory() as tmp:
            defaults = (parse_wish("weekend 09:30-12:30"),)
            store = WishStore(Path(tmp) / "wishes.json", defaults)
            self.assertEqual(store.load(), defaults)
            mine = (parse_wish("tue 17:00"), parse_wish("mon-fri 16:00-20:30 prefer 16:30"))
            store.save(mine)
            self.assertEqual(WishStore(Path(tmp) / "wishes.json", defaults).load(), mine)
            self.assertEqual(store.reset(), defaults)
            self.assertEqual(store.load(), defaults)

    def test_shipped_config_as_wishes(self):
        from padel_bot.wishes import describe
        s = load_settings(REPO / "config.toml", require_club=False)
        self.assertEqual([describe(w) for w in s.windows],
                         ["Mon-Fri 16:00-20:30 (starts 16:00-19:00), 90 min, ⭐ 16:30 17:00",
                          "Sat-Sun 09:30-12:30 (starts 09:30-11:00), 90 min"])


class FakeTelegram(FakeNotifier):
    chat_id = "42"

    def __init__(self, batches=()):
        super().__init__()
        self.batches = list(batches)
        self.offsets = []

    def get_updates(self, offset, timeout=25):
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []


def message(update_id, text, chat_id=42):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


class CommandTests(unittest.TestCase):
    def setUp(self):
        from padel_bot.commands import CommandCenter
        from padel_bot.wishes import WishStore
        self.tmp = tempfile.TemporaryDirectory()
        self.s = settings(Path(self.tmp.name))
        self.now = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)
        self.client = FakeClient()
        self.telegram = FakeTelegram()
        self.watcher = Watcher(self.s, self.client, CLUB, self.telegram, SeenState(self.s.state_file), TZ,
                               None,
                               clock=lambda: self.now, sleep=lambda s: None)
        self.store = WishStore(Path(self.tmp.name) / "wishes.json", self.s.windows)
        self.cc = CommandCenter(self.watcher, self.telegram, self.store, sleep=lambda s: None)
        self.watcher.commands = self.cc

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_remove_wish(self):
        reply = self.cc.handle("/wish tue 17:00")
        self.assertIn("Added: Tue 17:00-18:30", reply)
        self.assertEqual(len(self.watcher.settings.windows), 3)
        self.assertTrue(self.watcher.check_requested)
        self.assertTrue((Path(self.tmp.name) / "wishes.json").exists())
        self.assertIn("3. Tue 17:00-18:30", self.cc.handle("/wishes"))
        self.assertIn("Removed: Sat-Sun", self.cc.handle("/remove 2"))
        self.assertEqual(len(self.store.load()), 2)
        self.assertIn("Which one?", self.cc.handle("/remove 9"))
        self.assertIn("🤔", self.cc.handle("/wish someday 17:00"))
        self.assertIn("Back to the wishes", self.cc.handle("/reset"))
        self.assertEqual(len(self.watcher.settings.windows), 2)

    def test_new_wish_finds_slots(self):
        # 19:30 local on a Monday is outside the default wishes...
        self.client.replies["2026-10-26"] = [avail("c1", "2026-10-26", ("18:30:00", 90, "48 EUR"))]
        self.assertEqual(self.watcher.run_round(), [])
        # ...until you wish for it.
        self.cc.handle("/wish mon 19:30")
        self.assertEqual(len(self.watcher.run_round()), 1)
        self.assertIn("19:30-21:00", self.cc.handle("/now"))

    def test_plain_date_message_adds_one_time_wish(self):
        reply = self.cc.handle("28.10.2026 between 18:00 - 19:30")
        self.assertIn("Added: Wed 28 Oct (once) 18:00-21:00 (starts 18:00-19:30), 90 min", reply)

    def test_pause_resume_status(self):
        self.assertIn("Paused", self.cc.handle("/pause"))
        self.assertTrue(self.watcher.paused)
        self.assertIn("paused", self.cc.handle("/status"))
        self.assertIn("Resumed", self.cc.handle("/resume"))
        self.assertFalse(self.watcher.paused)
        self.assertIn("running", self.cc.handle("/status"))
        self.assertIn("/help", self.cc.handle("hello"))
        self.assertIn("/wish", self.cc.handle("/help"))
        self.assertIn("I'm watching", self.cc.handle("/wishes@padel_bot"))

    def test_only_own_chat_is_obeyed(self):
        self.cc.process([message(5, "/pause", chat_id=666), message(6, "/wishes")])
        self.assertFalse(self.watcher.paused)
        self.assertEqual(len(self.telegram.sent), 1)          # only our chat got an answer
        self.assertIn("I'm watching", self.telegram.sent[0])
        self.assertEqual(self.cc.offset, 7)

    def test_listen_returns_early_after_new_wish(self):
        clock = [0.0]
        self.cc.monotonic = lambda: clock[0]
        self.telegram.batches = [[], [message(1, "/wish sat 10:00")], []]
        def advance(*a, **k):
            clock[0] += 25
            return FakeTelegram.get_updates(self.telegram, *a, **k)
        self.telegram.get_updates = advance
        self.cc.listen(300)
        self.assertEqual(clock[0], 50)                       # stopped after the wish, not at 300 s
        self.assertEqual(self.telegram.offsets, [None, None])


class ExploreTests(unittest.TestCase):
    def test_find_events_in_json_and_html(self):
        import json as _json
        from padel_bot.explore import find_events
        event = {"tournament_id": "e1", "name": "Newcomer Club mit Coach David",
                 "start_date": "2026-10-31T10:30:00", "available_places": 2}
        self.assertEqual(find_events(_json.dumps({"content": [event]})), [event])
        flight = _json.dumps({"tournament": event})
        html = '<script>self.__next_f.push([1,' + _json.dumps(flight) + '])</script>'
        self.assertEqual(find_events(html), [event])
        self.assertEqual(find_events("<html>nothing</html>"), [])

    def test_analyze_page(self):
        import json as _json
        from padel_bot.explore import analyze
        event = {"id": "e1", "title": "Signup! Newcomer Club mit Coach David", "level": "0-3",
                 "players": {"max": 4, "registered": 3}}
        flight = _json.dumps({"activity": event})
        html = ('<a href="/tournaments/e1">x</a><a href="/about">y</a>'
                '<script>fetch("/api/v1/activities/e1")</script>'
                '<script>self.__next_f.push([1,' + _json.dumps(flight) + '])</script>')
        found = analyze(html, "newcomer")
        self.assertEqual(found["objects"][0], event)
        self.assertEqual(found["links"], ["/tournaments/e1"])
        self.assertEqual(found["apis"], ["/api/v1/activities/e1"])
        plain = analyze("<p>Join the Newcomer club</p>", "Newcomer")
        self.assertEqual(plain["objects"], [])
        self.assertIn("Newcomer club", plain["snippets"][0])

    def test_visible_text(self):
        from padel_bot.explore import visible_text
        html = ("<html><head><style>.a{}</style><script>var x='hidden'</script></head><body>"
                "<h1>Signup! Newcomer Club</h1><p>Sa., 31. Okt. &middot; 11:30</p><p>Level 0-3</p>"
                "<p>Level 0-3</p><span>3/4</span></body></html>")
        self.assertEqual(visible_text(html), ["Signup! Newcomer Club", "Sa., 31. Okt. · 11:30", "Level 0-3", "3/4"])


CLASS_PAGE_LINES = ("Leveling Session | padel amigos Hamburg-Halstenbek | Für Spieler | Clubs | / | padel amigos Hamburg-Halstenbek | "
    "Leveling Session | Padel | Gruppenstunde | Datum | Freitag, 30. Oktober | 17:30h | Dauer | 60 Min. | Niveau | 0 – 7 | "
    "Anmeldung endet | Donnerstag, 29. Oktober | 17:30h | Beschreibung | Remember that also your reliability index will be updated. | "
    "Trainer | DA | Dado | Spieler (1/3) | SK | S | Verfügbar | 1 | 3 | Wo gespielt wird | Am Bahndamm 88, 25469, Halstenbek | "
    "30 € | / Anmeldung | Freitag, 30. Oktober | 17:30 | · | 1/3 Plätze · 2 übrig | Gruppenstunde buchen · 30 € | 2 Plätze übrig | Buchen")
OPEN_PLAY_LINES = ("Playtomic | Continue in the app | Everything you need is in the Playtomic app | Open Play | "
    "Sat, Oct 31 · 11:30 AM | Newcomer Club mit Coach David | Padel | 0 – 3 | Unrestricted | 0 of 10 spots | "
    "Registration | EUR | €17.00 | Get the app to register")


def as_html(lines: str) -> str:
    return "<html><body>" + "".join(f"<p>{line}</p>" for line in lines.split(" | ")) + "<script>x=1</script></body></html>"


class EventParsingTests(unittest.TestCase):
    today = date(2026, 10, 25)

    def test_class_page(self):
        from padel_bot.events import parse_class_page
        url = "https://playtomic.com/de/activities/public-classes/1f63e06a-ced1-4551-9d5e-400caf2342ef"
        e = parse_class_page(as_html(CLASS_PAGE_LINES), url, TZ, self.today)
        self.assertEqual(e.event_id, "1f63e06a-ced1-4551-9d5e-400caf2342ef")
        self.assertEqual((e.name, e.kind, e.coach, e.level, e.price), ("Leveling Session", "Group class", "Dado", "0 – 7", "€30"))
        self.assertEqual(e.start, datetime(2026, 10, 30, 17, 30, tzinfo=TZ))
        self.assertEqual((e.duration, e.registered, e.capacity, e.spots_left), (60, 1, 3, 2))

    def test_open_play_page(self):
        from padel_bot.events import describe_event, parse_open_play_page
        url = "https://app.playtomic.com/tournaments/30edfbd8-8f5f-4b74-8075-500e4caa7c78"
        e = parse_open_play_page(as_html(OPEN_PLAY_LINES), url, TZ, self.today)
        self.assertEqual((e.kind, e.name, e.coach, e.level, e.price), ("Open Play", "Newcomer Club mit Coach David", "David", "0 – 3", "€17"))
        self.assertEqual(e.start, datetime(2026, 10, 31, 11, 30, tzinfo=TZ))
        self.assertEqual((e.registered, e.capacity, e.spots_left), (0, 10, 10))
        self.assertEqual(describe_event(e).splitlines()[0],
                         "Sat 31 Oct 11:30 · Newcomer Club mit Coach David · level 0 – 3 · €17 · 10 spots left (of 10)")

    def test_year_rollover(self):
        from padel_bot.events import parse_day_month
        self.assertEqual(parse_day_month("Dienstag, 05. Januar", date(2026, 12, 20)), date(2027, 1, 5))
        self.assertEqual(parse_day_month("Tue, Dec 15", self.today), date(2026, 12, 15))

    def test_tournament_from_api(self):
        from padel_bot.events import tournament_event
        e = tournament_event({"tournament_id": "t9", "name": "Afterwork intermediate", "start_date": "2026-12-03T18:00:00",
                              "available_places": 0, "type": "OPEN_PLAY"}, TZ, True)
        self.assertEqual((e.event_id, e.kind, e.spots_left), ("t9", "Open Play", 0))
        self.assertEqual(e.start, datetime(2026, 12, 3, 19, 0, tzinfo=TZ))  # UTC -> CET
        self.assertIn("t9", e.url)


class FakeWeb:
    def __init__(self):
        self.pages: dict[str, str] = {}
        self.fetched: list[str] = []

    def fetch_page(self, url):
        self.fetched.append(url)
        for key, html in self.pages.items():
            if key in url:
                if isinstance(html, Exception):
                    raise html
                return html
        raise PlaytomicError("HTTP 404", status=404)


class FakeAccount:
    logged_in = True

    def __init__(self):
        self.events: list[dict] | Exception = []

    def list_tournaments(self, tenant_id, from_utc):
        if isinstance(self.events, Exception):
            raise self.events
        return self.events


def open_play_html(name, when, registered, capacity):
    return as_html(f"Open Play | {when} | {name} | Padel | 2 – 4 | {registered} of {capacity} spots | €17.00")


class EventWatcherTests(unittest.TestCase):
    def setUp(self):
        from padel_bot.events import EventWatcher, EventWords
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = settings(tmp)
        self.now = datetime(2026, 10, 25, 12, 0, tzinfo=TZ)  # Sunday
        self.web, self.account, self.telegram = FakeWeb(), FakeAccount(), FakeNotifier()
        self.web.pages["academy/public-classes"] = '<a href="/de/activities/public-classes/1f63e06a-ced1-4551-9d5e-400caf2342ef">x</a>'
        self.web.pages["1f63e06a"] = as_html(CLASS_PAGE_LINES)
        self.words = EventWords(tmp / "event_words.json", ["Newcomer", "Afterwork"])
        self.make = lambda: EventWatcher(self.s, self.web, self.account, CLUB, TZ, tmp / "events_state.json",
                                         self.words, self.telegram, clock=lambda: self.now, sleep=lambda s: None)
        self.ew = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def add_event(self, eid, name, start_utc, places, page_when, registered, capacity):
        self.account.events = (self.account.events if isinstance(self.account.events, list) else []) + [
            {"tournament_id": eid, "name": name, "start_date": start_utc, "available_places": places}]
        self.web.pages[eid] = open_play_html(name, page_when, registered, capacity)

    def test_first_check_lists_then_new_and_freed_alerts(self):
        self.add_event("e1", "Newcomer Club mit Coach David", "2026-11-07T10:30:00", 0, "Sat, Nov 7 · 11:30 AM", 10, 10)
        sent = self.ew.check()
        self.assertEqual(len(sent), 1)
        self.assertIn("Now watching", sent[0])
        self.assertIn("Newcomer Club mit Coach David · level 2 – 4 · €17 · FULL", sent[0])
        self.assertNotIn("Leveling", sent[0])            # class not watched by name
        # December dates get published:
        self.now += timedelta(minutes=30)
        self.add_event("e2", "Afterwork intermediate", "2026-12-03T17:00:00", 3, "Thu, Dec 3 · 6:00 PM", 1, 4)
        sent = self.ew.check()
        self.assertEqual(sent, ["🆕 New date: Thu 3 Dec 18:00 · Afterwork intermediate · level 2 – 4 · €17 · 3 spots left (of 4)\n"
                                "https://app.playtomic.com/tournaments/e2"])
        # Someone cancels in the full November event:
        self.now += timedelta(minutes=30)
        self.account.events[0]["available_places"] = 1
        fetched_before = len(self.web.fetched)
        sent = self.ew.check()
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("🟢 Spot free again: Sat 7 Nov 11:30 · Newcomer"))
        self.assertIn("1 spot left", sent[0])
        # known event pages aren't re-read; only the class list was fetched
        self.assertEqual([u for u in self.web.fetched[fetched_before:] if "tournaments" in u], [])
        self.assertEqual(self.ew.check(), [])            # nothing changed: silence

    def test_state_survives_restart(self):
        self.add_event("e1", "Newcomer Club mit Coach David", "2026-11-07T10:30:00", 0, "Sat, Nov 7 · 11:30 AM", 10, 10)
        self.ew.check()
        self.assertEqual(self.make().check(), [])

    def test_failed_source_does_not_cause_fake_new_alerts(self):
        self.add_event("e1", "Newcomer Club mit Coach David", "2026-11-07T10:30:00", 2, "Sat, Nov 7 · 11:30 AM", 8, 10)
        self.ew.check()
        saved = self.account.events
        self.account.events = PlaytomicError("HTTP 503", status=503)
        self.assertEqual(self.ew.check(), [])
        self.assertEqual(len(self.ew.upcoming), 1)       # still shown from memory
        self.account.events = saved
        self.assertEqual(self.ew.check(), [])            # back: not "new"

    def test_not_logged_in_warns_once_and_classes_still_work(self):
        from padel_bot.account import AuthError
        self.words.save(["Leveling"])
        self.account.events = AuthError("Not logged in to Playtomic - double-click playtomic-login.bat.")
        self.ew.check()
        self.now += timedelta(minutes=30)
        self.ew.check()
        warnings = [m for m in self.telegram.sent if "playtomic-login.bat" in m]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(any("Leveling Session · coach Dado · level 0 – 7 · €30 · 2 spots left (of 3)" in m
                            for m in self.telegram.sent))

    def test_weekly_overview_sunday_evening_once(self):
        self.add_event("e1", "Newcomer Club mit Coach David", "2026-11-07T10:30:00", 0, "Sat, Nov 7 · 11:30 AM", 10, 10)
        self.ew.tick()                                    # Sunday 12:00: check, no overview yet
        self.assertFalse(any("Upcoming events" in m for m in self.telegram.sent))
        self.now = datetime(2026, 10, 25, 18, 5, tzinfo=TZ)
        self.ew.tick()
        self.ew.tick()
        overviews = [m for m in self.telegram.sent if m.startswith("📅 Upcoming events")]
        self.assertEqual(len(overviews), 1)
        self.assertIn("1 date(s), 0 with free spots", overviews[0])

    def test_watch_commands(self):
        from padel_bot.commands import CommandCenter
        from padel_bot.wishes import WishStore
        watcher = Watcher(self.s, FakeClient(), CLUB, self.telegram, SeenState(self.s.state_file), TZ,
                          None, clock=lambda: self.now, sleep=lambda s: None)
        watcher.events = self.ew
        cc = CommandCenter(watcher, self.telegram, WishStore(Path(self.tmp.name) / "w.json", self.s.windows))
        self.assertIn("still to come", cc.handle("/events"))
        self.assertIn("Watching events named 'Leveling'", cc.handle("/watch Leveling"))
        self.assertEqual(self.words.load(), ["Newcomer", "Afterwork", "Leveling"])
        self.ew.check()
        self.assertIn("Leveling Session", cc.handle("/events"))
        self.assertIn("Stopped watching 'Afterwork'", cc.handle("/unwatch 2"))
        self.assertIn("Which one?", cc.handle("/unwatch 7"))


class AccountTests(unittest.TestCase):
    def test_login_refresh_and_retry(self):
        from padel_bot.account import AuthError, PlaytomicAccount
        with tempfile.TemporaryDirectory() as tmp:
            acc = PlaytomicAccount(Path(tmp) / "playtomic_token.json", "ua")
            calls = []
            def fake(method, path, *, query=None, body=None, bearer=None):
                calls.append((method, path, bearer, body))
                if path == "/v3/auth/login":
                    return {"access_token": "A1", "refresh_token": "R1", "access_token_expiration": "2099-01-01T00:00:00"}
                if path == "/v3/auth/token":
                    assert body["refresh_token"] == "R1"
                    return {"access_token": "A2", "refresh_token": "R2", "access_token_expiration": "2099-01-01T00:00:00"}
                if bearer == "A1":
                    raise PlaytomicError("HTTP 401", status=401)
                return [{"tournament_id": "x"}]
            acc._request = fake
            with self.assertRaises(AuthError):
                acc.get("/v2/tournaments")                # not logged in yet
            acc.login("me@example.com", "secret")
            saved = (Path(tmp) / "playtomic_token.json").read_text()
            self.assertNotIn("secret", saved)
            self.assertNotIn("me@example.com", saved)
            self.assertEqual(acc.list_tournaments("T1", datetime(2026, 10, 25, tzinfo=timezone.utc)), [{"tournament_id": "x"}])
            self.assertEqual([c[1] for c in calls], ["/v3/auth/login", "/v2/tournaments", "/v3/auth/token", "/v2/tournaments"])
            self.assertEqual(PlaytomicAccount(Path(tmp) / "playtomic_token.json", "ua").token["access_token"], "A2")

    def test_wrong_password_message(self):
        from padel_bot.account import AuthError, PlaytomicAccount
        with tempfile.TemporaryDirectory() as tmp:
            acc = PlaytomicAccount(Path(tmp) / "t.json", "ua")
            def fake(*a, **k):
                raise PlaytomicError("HTTP 401", status=401)
            acc._request = fake
            with self.assertRaisesRegex(AuthError, "Google or Apple"):
                acc.login("me@example.com", "wrong")


class TelegramLoginTests(unittest.TestCase):
    def setUp(self):
        from padel_bot.commands import CommandCenter
        from padel_bot.wishes import WishStore
        self.tmp = tempfile.TemporaryDirectory()
        self.s = settings(Path(self.tmp.name))
        self.now = datetime(2026, 10, 25, 12, 0, tzinfo=TZ)
        self.telegram = FakeTelegram()
        self.telegram.deleted = []
        self.telegram.delete_message = lambda mid: self.telegram.deleted.append(mid)
        self.watcher = Watcher(self.s, FakeClient(), CLUB, self.telegram, SeenState(self.s.state_file), TZ,
                               None,
                               clock=lambda: self.now, sleep=lambda s: None)

        class Account:
            logged_in = False
            attempts = []
            def login(acc, email, password):
                acc.attempts.append((email, password))
                if password != "right":
                    from padel_bot.account import AuthError
                    raise AuthError("Playtomic didn't accept that email/password.")
                acc.logged_in = True
            def logout(acc):
                acc.logged_in = False

        class Events:
            account = Account()
            open_play_problem = "not logged in"
            last_check = self.now

        self.watcher.events = Events()
        self.clock = [0.0]
        self.cc = CommandCenter(self.watcher, self.telegram, WishStore(Path(self.tmp.name) / "w.json", self.s.windows),
                                monotonic=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def msg(self, update_id, text):
        return {"update_id": update_id, "message": {"message_id": 100 + update_id, "chat": {"id": 42}, "text": text}}

    def test_login_deletes_password_and_never_logs_it(self):
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("padel_bot", level="INFO") as logs:
                self.cc.process([self.msg(1, "/login"), self.msg(2, "me@example.com"), self.msg(3, "right")])
        finally:
            logging.disable(logging.CRITICAL)
        self.assertIn("Step 1/2", self.telegram.sent[0])
        self.assertIn("Step 2/2", self.telegram.sent[1])
        self.assertIn("✅ Logged in", self.telegram.sent[2])
        self.assertEqual(self.telegram.deleted, [103])            # the password message
        self.assertTrue(self.watcher.events.account.logged_in)
        self.assertIsNone(self.watcher.events.last_check)          # events re-checked right away
        self.assertTrue(self.watcher.check_requested)
        self.assertFalse(any("right" in line or "me@example.com" in line for line in logs.output))
        self.assertIsNone(self.cc.login_step)

    def test_wrong_password_cancel_and_timeout(self):
        self.cc.process([self.msg(1, "/login"), self.msg(2, "not an email"), self.msg(3, "me@example.com"),
                         self.msg(4, "wrong")])
        self.assertIn("doesn't look like an email", self.telegram.sent[1])
        self.assertIn("❌ Login failed", self.telegram.sent[-1])
        self.assertEqual(self.telegram.deleted, [104])
        self.assertIn("Login cancelled", self.cc.handle("/login") and self.cc.handle("/cancel"))
        self.cc.process([self.msg(5, "/login")])
        self.clock[0] += 301
        self.cc.process([self.msg(6, "me@example.com")])
        self.assertIn("timed out", self.telegram.sent[-1])
        self.assertEqual(self.telegram.deleted[-1], 106)
        # outside a login, normal text isn't treated as a password
        self.cc.process([self.msg(7, "right")])
        self.assertIn("/help", self.telegram.sent[-1])
        self.assertEqual(self.watcher.events.account.attempts, [("me@example.com", "wrong")])

    def test_logout(self):
        self.watcher.events.account.logged_in = True
        self.assertIn("Logged out", self.cc.handle("/logout"))
        self.assertFalse(self.watcher.events.account.logged_in)


class MenuBase(unittest.TestCase):
    def setUp(self):
        from padel_bot.commands import CommandCenter
        from padel_bot.events import EventWords
        from padel_bot.friends import Broadcaster, Friends
        from padel_bot.menu import RuntimeSettings
        from padel_bot.wishes import WishStore
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = settings(tmp)
        self.now = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)  # Monday
        self.telegram = FakeTelegram()
        self.telegram.bot_username = lambda: "padel_test_bot"
        self.friends = Friends(tmp / "friends.json")
        self.bc = Broadcaster(self.telegram, self.friends)
        self.client = FakeClient()
        self.watcher = Watcher(self.s, self.client, CLUB, self.bc, SeenState(self.s.state_file), TZ,
                               None,
                               clock=lambda: self.now, sleep=lambda s: None)

        class Events:
            words = EventWords(tmp / "event_words.json", ["Newcomer"])
            account = None
            last_check = None
            open_play_problem = None
            restarted = 0
            def restart_watching(ev):
                ev.restarted += 1
        self.watcher.events = Events()
        self.runtime = RuntimeSettings(tmp / "bot_settings.json")
        self.store = WishStore(tmp / "wishes.json", self.s.windows)
        self.cc = CommandCenter(self.watcher, self.bc, self.store, friends=self.friends, runtime=self.runtime)
        self.menu = self.cc.menu

    def tearDown(self):
        self.tmp.cleanup()

    def tap(self, data, chat_id=42):
        self.cc.process([{"update_id": 1, "callback_query": {
            "id": "q", "data": data, "message": {"message_id": 7, "chat": {"id": chat_id}}}}])
        return self.telegram.toasts[-1]

    def last_screen(self):
        return self.telegram.edits[-1]

    def labels(self):
        return [label for row in self.last_screen()["buttons"] for label, _ in row]


class MenuTests(MenuBase):
    def test_menu_command_and_button(self):
        self.cc.process([message(1, "/start")])
        self.assertEqual(self.telegram.outbox[0]["keyboard"], [["🎾 Menu"]])
        self.assertIn("➕ Court wish", [b for row in self.telegram.outbox[1]["buttons"] for b, _ in row])
        self.cc.process([message(2, "🎾 Menu")])
        self.assertEqual(len(self.telegram.outbox), 4)

    def test_recurring_wish_by_buttons(self):
        self.tap("cw")
        self.tap("d:ok")
        self.assertEqual(self.telegram.toasts[-1], "Pick at least one day")
        self.tap("d:tue")
        self.tap("d:thu")
        self.assertIn("✅ Tue", self.labels())
        self.tap("d:ok")
        self.tap(f"s:{17 * 60}")
        self.assertIn("Only 17:00", self.labels())
        self.tap(f"l:{18 * 60}")
        self.tap("u:90")
        self.assertIn("Tue, Thu 17:00-19:30 (starts 17:00-18:00), 90 min", self.last_screen()["text"])
        self.assertEqual(self.tap("ok"), "Added")
        self.assertEqual(len(self.watcher.settings.windows), 3)
        self.assertEqual(len(self.store.load()), 3)          # saved

    def test_one_time_wish_matches_only_that_date(self):
        self.tap("ow")
        self.assertIn("Today", self.labels())
        self.tap("dt:2026-10-29")                            # Thursday
        self.tap(f"s:{20 * 60}")
        self.tap(f"l:{20 * 60}")                             # exactly 20:00
        self.tap("u:60")
        self.assertIn("Thu 29 Oct (once) 20:00-21:00, 60 min", self.last_screen()["text"])
        self.tap("ok")
        # a free 20:00 hour on Thursday matches; the same on Wednesday doesn't
        self.client.replies["2026-10-29"] = [avail("c1", "2026-10-29", ("19:00:00", 60, "30 EUR"))]
        self.client.replies["2026-10-28"] = [avail("c1", "2026-10-28", ("19:00:00", 60, "30 EUR"))]
        found = self.watcher.run_round()
        self.assertEqual([(s.start.date(), s.start.hour) for s in found], [(date(2026, 10, 29), 20)])
        # after the date it disappears from the list
        self.now = datetime(2026, 10, 30, 9, 0, tzinfo=TZ)
        self.tap("mw")
        self.assertNotIn("once", self.last_screen()["text"])
        self.assertEqual(len(self.store.load(today=date(2026, 10, 30))), 2)

    def test_delete_wish_and_event_buttons(self):
        self.tap("mw")
        delete = [d for row in self.last_screen()["buttons"] for _, d in row if d.startswith("dw:0:")][0]
        self.assertTrue(self.tap(delete).startswith("Deleted: Mon-Fri"))
        self.assertEqual(len(self.watcher.settings.windows), 1)
        self.assertIn("outdated", self.tap(delete))          # same button again: safe
        word = [d for row in self.last_screen()["buttons"] for _, d in row if d.startswith("de:0:")][0]
        self.assertIn("Stopped watching 'Newcomer'", self.tap(word))
        self.assertEqual(self.watcher.events.words.load(), [])

    def test_add_event_name_by_typing(self):
        self.tap("ev")
        self.tap("ea")
        self.cc.process([message(3, "Afterwork")])
        self.assertIn("Watching events named 'Afterwork'", self.telegram.sent[-1])
        self.assertEqual(self.watcher.events.words.load(), ["Newcomer", "Afterwork"])

    def test_settings_persist(self):
        from padel_bot.menu import RuntimeSettings
        self.tap("st")
        self.assertEqual(self.tap("sp"), "Paused")
        self.assertEqual(self.tap("so"), "Only ⭐ times")
        self.assertIn("00:00-07:00", self.tap("sq"))
        self.assertIn("01:00-06:00", self.tap("sq"))
        self.assertIn("off", self.tap("sq").lower())  # 23-07 -> 00-07 -> 01-06 -> off
        fresh = Watcher(self.s, self.client, CLUB, self.bc, SeenState(self.s.state_file), TZ,
                        None, clock=lambda: self.now)
        RuntimeSettings(Path(self.tmp.name) / "bot_settings.json").apply(fresh)
        self.assertTrue(fresh.paused)
        self.assertTrue(fresh.settings.only_preferred)
        self.assertIsNone(fresh.settings.quiet_hours)

    def test_buttons_from_strangers_are_refused(self):
        self.assertEqual(self.tap("sp", chat_id=666), "Not allowed")
        self.assertFalse(self.watcher.paused)

    def test_friend_request_approve_alerts_and_leave(self):
        friend = {"update_id": 5, "message": {"message_id": 1, "chat": {"id": 777},
                                              "from": {"first_name": "Ali", "username": "ali_p"}, "text": "/start"}}
        self.cc.process([friend])
        ask = self.telegram.outbox[-1]
        self.assertEqual(ask["chat_id"], "777")              # friend told to wait
        request = self.telegram.outbox[-2]
        self.assertIn("Ali (@ali_p) wants to get your padel alerts", request["text"])
        self.assertEqual(request["buttons"][0][0][1], "fa:777")
        self.cc.process([friend])
        self.assertIn("Still waiting", self.telegram.outbox[-1]["text"])
        self.assertEqual(self.tap("fa:777"), "Approved Ali (@ali_p)")
        # alerts now reach both of you
        self.client.replies["2026-10-27"] = [avail("c2", "2026-10-27", ("16:00:00", 90, "48 EUR"))]
        self.watcher.run_round()
        alert_targets = [m["chat_id"] for m in self.telegram.outbox if "Free:" in m["text"]]
        self.assertEqual(alert_targets, [None, "777"])
        # but the friend can't control anything
        self.cc.process([{"update_id": 6, "message": {"chat": {"id": 777}, "from": {"first_name": "Ali"}, "text": "/pause"}}])
        self.assertFalse(self.watcher.paused)
        self.cc.process([{"update_id": 7, "message": {"chat": {"id": 777}, "from": {"first_name": "Ali"}, "text": "/stop"}}])
        self.assertEqual(self.friends.approved, {})

    def test_declined_stranger_is_ignored(self):
        stranger = {"update_id": 5, "message": {"chat": {"id": 888}, "from": {"first_name": "X"}, "text": "hi"}}
        self.cc.process([stranger])
        self.tap("fd:888")
        before = len(self.telegram.outbox)
        self.cc.process([stranger])
        self.assertEqual(len(self.telegram.outbox), before)

    def test_friends_screen_shows_invite_link(self):
        self.tap("fr")
        self.assertIn("https://t.me/padel_test_bot", self.last_screen()["text"])


class NoticeTests(unittest.TestCase):
    def test_48h_buffer(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = load_settings(REPO / "config.toml", require_club=False)
            self.assertEqual((s.min_notice_min, s.free_cancel_min), (0, 24 * 60))  # default: alert for all
            from dataclasses import replace as _replace
            s = _replace(s, min_notice_min=48 * 60)
            now = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)                   # Monday 08:00
            raw = [avail("c1", "2026-10-27", ("15:30:00", 90, "48 EUR")),   # Tue 16:30: 32.5h ahead
                   avail("c1", "2026-10-28", ("15:30:00", 90, "48 EUR"))]   # Wed 16:30: 56.5h ahead
            slots = parse_availability(raw, COURTS, TZ)
            self.assertEqual([m.start.day for m in find_matches(slots, COURTS, s, now)], [28])


class AlertExtrasTests(MenuBase):
    """Who's in, I booked it, mute, <24h warning, daily check-in (reuses the menu set-up)."""

    def setUp(self):
        super().setUp()
        from padel_bot.polls import Polls
        from padel_bot.reminders import Reminders
        tmp = Path(self.tmp.name)
        self.watcher.polls = Polls(tmp / "polls.json", self.bc, self.friends)
        self.watcher.reminders = Reminders(tmp / "reminders.json", self.bc, self.s.free_cancel_min)
        self.telegram.chat_first_name = lambda chat_id: "Mo"
        self.friends.pending["777"] = "Ali (@ali_p)"
        self.friends.approve("777")

    def court(self, day, utc_time, minutes=90):
        self.client.replies[day] = [avail("c2", day, (utc_time, minutes, "45 EUR"))]

    def alerts(self):
        return [m for m in self.telegram.outbox if "Free court" in m["text"]]

    def test_list_alert_grouped_with_buttons(self):
        from padel_bot.booking import BookingManager, PlaytomicBooker
        tmp = Path(self.tmp.name)
        self.watcher.bookings = BookingManager(lambda: self.watcher.settings,
                                               PlaytomicBooker(FakeBookingAccount(), "T1", self.s.booking),
                                               tmp / "bs.json", tmp / "bl.jsonl", self.bc, TZ, self.watcher.clock)
        self.watcher.commands = self.cc
        for day in ("2026-10-27", "2026-10-28"):
            self.client.replies[day] = [avail(c, day, ("15:00:00", 90, "48 EUR"), ("16:00:00", 90, "48 EUR"))
                                        for c in ("c1", "c2")]
        self.watcher.run_round()
        owner = [m for m in self.telegram.outbox if "new free court slot(s)" in m["text"] and m["chat_id"] is None][0]
        friend = [m for m in self.telegram.outbox if "new free court slot(s)" in m["text"] and m["chat_id"] == "777"][0]
        self.assertIn("Free: Tue 27 Oct, 16:00-17:30: Court 1, Court 2 · €48.", owner["text"])
        self.assertEqual(owner["text"].count("Free:"), 4)           # 8 slots, 4 times
        labels = [label for row in owner["buttons"] for label, _ in row]
        self.assertEqual(labels, ["🧪 27.10 17:00", "🧪 27.10 16:00", "🧪 28.10 17:00", "🧪 28.10 16:00"])  # ⭐ 17:00 first
        self.assertIsNone(friend["buttons"])
        data = owner["buttons"][0][0][1]
        self.assertTrue(self.tap(data).startswith("Test done"))
        self.assertIn("🧪 Test booking", self.telegram.sent[-1])
        # /now offers the same buttons any time
        self.cc.process([message(9, "/now")])
        self.assertIn("Tap a time to book it.", self.telegram.outbox[-1]["text"])
        self.assertTrue(self.telegram.outbox[-1]["buttons"][0][0][0].startswith("💳"))

    def test_poll_alert_votes_and_silence(self):
        self.court("2026-10-28", "15:30:00")                 # Wed 16:30 = preferred
        self.court("2026-10-29", "17:00:00")                 # Thu 18:00 = not preferred
        self.watcher.run_round()
        alerts = self.alerts()
        self.assertEqual([(a["chat_id"], a["silent"]) for a in alerts],
                         [(None, False), ("777", False), (None, True), ("777", True)])  # only ⭐ rings
        owner_buttons = [d for row in alerts[0]["buttons"] for _, d in row]
        friend_buttons = [d for row in alerts[1]["buttons"] for _, d in row]
        self.assertTrue(any(d.startswith("md:") for d in owner_buttons))
        self.assertFalse(any(d.startswith("md:") for d in friend_buttons))   # friends can't mute
        vote = [d for d in owner_buttons if d.startswith("p:in:")][0]
        self.assertEqual(self.tap(vote), "You're in (1/4)")
        self.assertEqual(self.tap(vote, chat_id=777), "You're in (2/4)")
        self.assertEqual(self.tap(vote, chat_id=999), "Not allowed")
        edited = self.telegram.edits[-2:]
        self.assertTrue(all("👥 In (2/4): Mo, Ali" in e["text"] for e in edited))
        self.assertEqual(self.tap(vote.replace("p:in", "p:out"), chat_id=777), "Noted - not this time")
        self.assertIn("🙅 Not: Ali", self.telegram.edits[-1]["text"])

    def test_many_new_slots_are_grouped(self):
        for i, day in enumerate(["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]):
            self.client.replies[day] = [avail("c1", day, ("15:00:00", 90, "45 EUR")),
                                        avail("c2", day, ("16:00:00", 90, "45 EUR"))]
        self.watcher.run_round()
        self.assertEqual(self.alerts(), [])
        grouped = [m for m in self.telegram.outbox if "new free court slot(s)" in m["text"]]
        self.assertEqual([m["chat_id"] for m in grouped], [None, "777"])

    def test_under_24h_warning(self):
        self.court("2026-10-26", "18:00:00")                 # today 19:00, 11h ahead
        self.court("2026-10-28", "18:00:00")
        self.watcher.run_round()
        texts = [a["text"] for a in self.alerts() if a["chat_id"] is None]
        self.assertIn("⚠️ <24h: no free cancellation", texts[0])
        self.assertNotIn("⚠️", texts[1])

    def test_mute_day_and_unmute(self):
        self.assertIn("No more court alerts for Wed 28 Oct", self.tap("md:2026-10-28"))
        self.court("2026-10-28", "15:30:00")
        self.watcher.run_round()
        self.assertEqual(self.alerts(), [])
        self.tap("mw")
        self.assertIn("🔕 Muted days: Wed 28 Oct", self.last_screen()["text"])
        self.assertIn("back", self.tap("um:2026-10-28"))
        self.watcher.run_round()
        self.assertEqual(len(self.alerts()), 2)

    def test_booked_it_reminder_48h_before(self):
        self.court("2026-11-05", "16:00:00")                 # Thu 5 Nov 17:00
        self.watcher.run_round()
        booked = [d for row in self.alerts()[0]["buttons"] for _, d in row if d.startswith("bk:")][0]
        toast = self.tap(booked)
        self.assertIn("remind you Tue 3 Nov 17:00", toast)
        self.assertEqual(self.tap(booked), "Already noted 📌")
        self.assertIn("remind you", self.tap(booked, chat_id=777))   # a friend can note it too
        self.tap("mw")
        self.assertIn("📌 Thu 5 Nov 17:00-18:30, Indoor 2", self.last_screen()["text"].replace("Court 2", "Indoor 2"))
        r = self.watcher.reminders
        self.assertEqual(r.due(datetime(2026, 11, 3, 16, 0, tzinfo=TZ)), 0)
        self.assertEqual(r.due(datetime(2026, 11, 3, 17, 1, tzinfo=TZ)), 2)
        reminder = [m for m in self.telegram.outbox if m["text"].startswith("⏰")]
        self.assertEqual([m["chat_id"] for m in reminder], ["42", "777"])
        self.assertIn("Free cancellation ends Wed 4 Nov 17:00 (in about 23h)", reminder[0]["text"])
        self.assertEqual(r.due(datetime(2026, 11, 3, 18, 0, tzinfo=TZ)), 0)   # once only
        r.due(datetime(2026, 11, 5, 19, 0, tzinfo=TZ))
        self.assertEqual(r.items, [])                                          # game over: dropped

    def test_booked_late(self):
        from padel_bot.reminders import Reminders
        r = Reminders(Path(self.tmp.name) / "r2.json", self.bc, 24 * 60)
        now = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)
        self.assertIn("free cancellation ends Mon 26 Oct 20:00",
                      r.add("42", "a", "x", datetime(2026, 10, 27, 20, 0, tzinfo=TZ), now))   # 36h ahead
        self.assertIn("already ended", r.add("42", "b", "y", datetime(2026, 10, 26, 20, 0, tzinfo=TZ), now))
        self.assertEqual(r.due(now), 1)                       # the 36h one is reminded right away

    def test_event_alert_has_booked_button(self):
        from padel_bot.events import EventWatcher, EventWords
        tmp = Path(self.tmp.name)
        web, account = FakeWeb(), FakeAccount()
        web.pages["academy/public-classes"] = ""
        ew = EventWatcher(self.watcher.settings, web, account, CLUB, TZ, tmp / "ev.json",
                          EventWords(tmp / "ew.json", ["Afterwork"]), self.bc, clock=lambda: self.now, sleep=lambda s: None)
        self.watcher.events = ew
        ew.check()
        account.events = [{"tournament_id": "e7", "name": "Afterwork intermediate", "start_date": "2026-12-03T17:00:00",
                           "available_places": 2}]
        web.pages["e7"] = open_play_html("Afterwork intermediate", "Thu, Dec 3 · 6:00 PM", 2, 4)
        ew.check()
        alert = [m for m in self.telegram.outbox if m["text"].startswith("🆕") and m["chat_id"] is None][0]
        self.assertEqual(alert["buttons"], [[("📌 I booked it", "bk:e:e7")]])
        self.assertIn("remind you Tue 1 Dec 18:00", self.tap("bk:e:e7"))

    def test_daily_report_once_a_day(self):
        self.now = datetime(2026, 10, 27, 8, 5, tzinfo=TZ)
        self.assertTrue(self.watcher.daily_report_due(self.now))
        self.watcher._maybe_daily_report()
        self.watcher._maybe_daily_report()
        reports = [m for m in self.telegram.sent if m.startswith("☀️")]
        self.assertEqual(len(reports), 1)
        self.assertIn("court checks", reports[0])
        late_start = Watcher(self.s, self.client, CLUB, self.bc, SeenState(self.s.state_file), TZ,
                             None,
                             clock=lambda: datetime(2026, 10, 27, 9, 0, tzinfo=TZ))
        self.assertFalse(late_start.daily_report_due(datetime(2026, 10, 27, 9, 0, tzinfo=TZ)))  # tomorrow


class FakeBookingAccount:
    """Scripted Playtomic payment API."""
    logged_in = True
    user_id = "u1"

    def __init__(self):
        self.calls = []
        self.price = 45
        self.methods = [{"method_type": "MERCHANT_WALLET", "payment_method_id": "w-1", "balance": {"amount": 20.0}},
                        {"method_type": "CREDIT_CARD", "payment_method_id": "c-9"}]
        self.refuse = set()            # method ids whose PATCH or confirmation is refused
        self.confirm_reply = {"match_id": "m-77", "status": "CONFIRMED"}
        self.confirm_error = None
        self.matches = []

    def send(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/v1/payment_intents":
            return {"payment_intent_id": "pi-1", "price": f"{self.price} EUR", "available_payment_methods": self.methods}
        if method == "PATCH":
            self.selected = body["selected_payment_method"]
            return {}
        if path.endswith("/confirmation"):
            if self.confirm_error:
                raise self.confirm_error
            if self.selected in self.refuse:
                raise PlaytomicError("HTTP 402 for confirmation: insufficient funds", status=402)
            return self.confirm_reply
        raise AssertionError(path)

    def get(self, path, query=None):
        assert path == "/v1/matches"
        return self.matches


class BookingTests(unittest.TestCase):
    def setUp(self):
        from padel_bot.booking import BookingManager, PlaytomicBooker
        from padel_bot.reminders import Reminders
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = settings(tmp)
        self.now = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)   # Monday
        self.acc = FakeBookingAccount()
        self.telegram = FakeNotifier()
        self.telegram.chat_id = "42"
        self.free = True
        self.reminders = Reminders(tmp / "rem.json", self.telegram, self.s.free_cancel_min)
        self.make = lambda: BookingManager(lambda: self.s, PlaytomicBooker(self.acc, "T1", self.s.booking),
                                           tmp / "bookings_state.json", tmp / "log.jsonl", self.telegram, TZ,
                                           lambda: self.now, slot_still_free=lambda slot: self.free,
                                           reminders=self.reminders)
        self.bm = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def slot(self, day=28, hour=17, price=45.0, preferred=False, court="c2"):
        return Slot(court, "Indoor 2", datetime(2026, 10, day, hour, 0, tzinfo=TZ), 90, price, f"{price:g} EUR", preferred)

    def paths(self):
        return [(m, p) for m, p, _ in self.acc.calls]

    def test_payload(self):
        from padel_bot.booking import PlaytomicBooker
        data = PlaytomicBooker(self.acc, "T1", self.s.booking).payload(self.slot())["cart"]["requested_item"]["cart_item_data"]
        self.assertEqual((data["start"], data["duration"], data["number_of_players"], data["supports_split_payment"]),
                         ("2026-10-28T16:00:00", 90, 4, True))     # 17:00 CET -> 16:00 UTC
        self.assertEqual((data["tenant_id"], data["resource_id"]), ("T1", "c2"))

    def test_stage1_test_never_confirms(self):
        text = self.bm.test(self.slot())
        self.assertIn("NOT confirmed, nothing paid", text)
        self.assertIn("I would pay with: club wallet, card", text)
        self.assertEqual(self.paths(), [("POST", "/v1/payment_intents")])
        self.assertEqual(self.bm.stage, "tested")
        self.assertIn("🔒", self.bm.set_auto(True))

    def test_stage2_tap_booking_with_wallet(self):
        self.bm.test(self.slot())
        out = self.bm.book(self.slot(), "tap")
        self.assertTrue(out.booked, out.text)
        self.assertIn("✅ BOOKED: Indoor 2 Wed 28 Oct 17:00-18:30 (90 min), your share €11.25 (court €45) "
                      "via club wallet. Wallet left €8.75. Invite your fellow players", out.text)
        self.assertIn("Free cancellation until Tue 27 Oct 17:00", out.text)
        self.assertIn("https://app.playtomic.com/matches/m-77", out.text)
        self.assertEqual([b for m, _, b in self.acc.calls if m == "PATCH"], [{"selected_payment_method": "w-1"}])
        self.assertEqual(self.bm.stage, "verified")
        self.assertEqual(len(self.reminders.items), 1)
        self.assertEqual(self.make().stage, "verified")          # survives a restart
        again = self.bm.book(self.slot(), "tap")
        self.assertIn("already booked", again.text)

    def test_wallet_balance_and_active_limit(self):
        self.assertIn("Club wallet balance: €20.00", self.bm.test(self.slot()))
        self.assertEqual(self.bm.state["wallet"]["balance"], 20.0)
        self.bm.state.update(stage="verified", auto=True)
        # No own cap: the bot tries; Playtomic's refusal is reported once, then quietly.
        self.acc.confirm_error = PlaytomicError("HTTP 409: You have exceeded the maximum number of 6 active bookings", status=409)
        out = self.bm.book(self.slot(day=29), "tap")
        self.assertIn("❌ Didn't get it: Thu 29 Oct 17:00-18:30, Indoor 2 (HTTP 409: You have exceeded", out.text)
        self.assertEqual(self.bm.book(self.slot(day=30), "auto").text, "")
        self.acc.confirm_error = None
        self.assertTrue(self.bm.book(self.slot(day=31), "auto").booked)
        self.assertFalse(self.bm.state["limit_notified"])           # next refusal will be reported again
        # An own cap can still be set in config.toml (max_active):
        self.s = _replace(self.s, booking=_replace(self.s.booking, max_active=2))
        self.acc.matches = [{"start_date": "2026-11-05T17:00:00"}, {"start_date": "2026-11-06T17:00:00"}]
        self.assertIn("Not booked (2/2 active)", self.bm.book(self.slot(day=28), "auto").text)
        self.acc.matches = self.acc.matches[:1]
        self.bm.prepare()
        self.assertIn("You're back at 1 active court booking(s)", self.telegram.sent[-1])

    def test_wallet_refused_then_card(self):
        self.acc.refuse = {"w-1"}
        out = self.bm.book(self.slot(), "tap")
        self.assertTrue(out.booked, out.text)
        self.assertIn("via card", out.text)

    def test_bank_confirmation_needed(self):
        self.acc.confirm_reply = {"status": "REQUIRES_ACTION", "next_action": {"redirect_url": "https://bank"}}
        out = self.bm.book(self.slot(), "tap")
        self.assertFalse(out.booked)
        self.assertIn("Open the Playtomic app", out.text)

    def test_unclear_outcome_never_retries_and_pauses_auto(self):
        self.bm.state.update(stage="verified", auto=True)
        self.acc.confirm_error = PlaytomicError("network error", status=None)
        out = self.bm.book(self.slot(), "auto")
        self.assertIn("check your bookings in the Playtomic app", out.text)
        self.assertEqual(sum(1 for _, p in self.paths() if p.endswith("/confirmation")), 1)
        self.assertFalse(self.bm.auto_on)

    def test_limits(self):
        self.assertIn("above your limit of €70", self.bm.book(self.slot(price=75), "tap").text)
        self.acc.price = 80                                       # Playtomic's own price is higher
        self.assertIn("above your limit", self.bm.book(self.slot(), "tap").text)
        self.assertNotIn(("POST", "/v1/payment_intents/pi-1/confirmation"), self.paths())
        self.acc.price = 45
        self.free = False
        self.assertIn("Someone else just booked it", self.bm.book(self.slot(), "tap").text)
        self.free = True
        self.assertTrue(self.bm.book(self.slot(hour=17), "tap").booked)
        self.assertIn("limit 1/day", self.bm.book(self.slot(hour=19, court="c1"), "tap").text)

    def test_automatic_rules(self):
        self.assertEqual(self.bm.auto_book([self.slot()]), [])    # locked
        self.bm.book(self.slot(day=30), "tap")                    # first real booking: Fri 30
        self.assertIn("ON", self.bm.set_auto(True))
        self.acc.matches = [{"start_date": "2026-10-29T09:00:00"}]   # you already play Thu 29 (10:00)
        new = [self.slot(day=27, hour=17),                        # Tue: only 33h ahead? no - 57h, ok
               self.slot(day=26, hour=19),                        # today: <24h, never automatic
               self.slot(day=29, hour=18),                        # Thu: you already play
               self.slot(day=30, hour=18),                        # Fri: bot already booked that day
               self.slot(day=31, hour=10, preferred=True),        # Sat: fine
               self.slot(day=31, hour=11)]                        # Sat: second one that day: no
        results = self.bm.auto_book(new)
        booked_days = sorted(b["day"] for b in self.bm.state["bookings"])
        self.assertEqual(booked_days, ["2026-10-27", "2026-10-30", "2026-10-31"])
        self.assertEqual(len(results), 2)                          # Tue + Sat; the rest skipped quietly
        self.assertTrue(all(r.booked for r in results))
        self.assertTrue(all("🤖 BOOKED" in m for m in self.telegram.sent))

    def test_cannot_check_existing_bookings_means_no_auto(self):
        self.bm.state.update(stage="verified", auto=True)
        def broken(path, query=None):
            raise PlaytomicError("HTTP 404", status=404)
        self.acc.get = broken
        self.assertIn("Couldn't check your existing Playtomic bookings", self.bm.book(self.slot(), "auto").text)
        self.assertTrue(self.bm.book(self.slot(), "tap").booked)   # with your tap it still works


class BookingMenuTests(MenuBase):
    def setUp(self):
        super().setUp()
        from padel_bot.booking import BookingManager, PlaytomicBooker
        from padel_bot.polls import Polls
        tmp = Path(self.tmp.name)
        self.telegram.chat_first_name = lambda chat_id: "Mo"
        self.watcher.polls = Polls(tmp / "polls.json", self.bc, self.friends)
        self.watcher.polls.owner_extra = self.menu.booking_button
        self.acc = FakeBookingAccount()
        self.watcher.bookings = BookingManager(lambda: self.watcher.settings, PlaytomicBooker(self.acc, "T1", self.s.booking),
                                               tmp / "bs.json", tmp / "bl.jsonl", self.bc, TZ, self.watcher.clock,
                                               slot_still_free=lambda slot: True)
        self.friends.pending["777"] = "Ali (@ali_p)"
        self.friends.approve("777")

    def test_alert_buttons_test_then_book(self):
        self.client.replies["2026-10-28"] = [avail("c2", "2026-10-28", ("15:30:00", 90, "45 EUR"))]
        self.watcher.run_round()
        owner, friend = [m for m in self.telegram.outbox if "Free court" in m["text"]]
        owner_data = [d for row in owner["buttons"] for _, d in row]
        self.assertTrue(any(d.startswith("bt:") for d in owner_data))
        self.assertFalse(any(d.startswith(("bt:", "bb:")) for row in friend["buttons"] for _, d in row))
        pid = [d for d in owner_data if d.startswith("bt:")][0][3:]
        self.tap(f"p:in:{pid}", chat_id=777)                       # Ali is in
        self.tap(f"bt:{pid}")
        self.assertIn("🧪 Test booking", self.telegram.sent[-1])
        self.assertIn("💳 Book €45", [label for row in self.telegram.edits[-1]["buttons"] for label, _ in row] +
                      [label for label, _ in self.menu.booking_button(pid, self.watcher.polls.polls[pid])])
        self.assertEqual(self.tap(f"bb:{pid}", chat_id=777), "Not allowed")   # friends can't book
        self.tap(f"bb:{pid}")
        ask = self.telegram.outbox[-1]
        self.assertIn("💳 Book Wed 28 Oct 16:30-18:00, Court 2 for €45?", ask["text"])
        self.assertEqual(ask["buttons"][0][0], ("✅ Yes, book €45", f"by:{pid}"))
        self.assertEqual(self.tap(f"by:{pid}"), "Booked ✅")
        self.assertIn("✅ BOOKED", self.telegram.edits[-1]["text"])
        to_ali = [m for m in self.telegram.outbox if m["chat_id"] == "777" and "Join the match" in m["text"]]
        self.assertEqual(len(to_ali), 1)
        self.tap("st")
        self.assertIn("🤖 Automatic booking: off", self.labels())
        self.assertIn("ON", self.tap("sa"))


class OpeningTests(unittest.TestCase):
    def setUp(self):
        from padel_bot.booking import BookingManager, PlaytomicBooker
        from padel_bot.opening import OpeningSniper
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = settings(tmp)
        self.now = datetime(2026, 10, 27, 19, 29, 20, tzinfo=TZ)   # Tuesday, 40 s before 19:30
        self.client = FakeClient()
        self.telegram = FakeNotifier()
        self.telegram.chat_id = "42"
        self.watcher = Watcher(self.s, self.client, CLUB, self.telegram, SeenState(self.s.state_file), TZ,
                               clock=lambda: self.now, sleep=lambda s: None)
        self.acc = FakeBookingAccount()
        self.bm = BookingManager(lambda: self.watcher.settings, PlaytomicBooker(self.acc, "T1", self.s.booking),
                                 tmp / "bs.json", tmp / "bl.jsonl", self.telegram, TZ, lambda: self.now,
                                 slot_still_free=lambda slot: self.fail("fast path must not re-check"))
        self.bm.state.update(stage="verified", auto=True)
        self.slept = []

        def sleep(seconds):
            self.slept.append(round(seconds, 1))
            self.now += timedelta(seconds=seconds)
        self.sn = OpeningSniper(self.watcher, self.bm, days_ahead=14, sleep=sleep)
        self.sn.enabled = True
        self.watcher.sniper = self.sn
        from padel_bot.wishes import parse_wish
        self.watcher.settings = _replace(self.s, windows=(parse_wish("mon-fri 16:00-19:30 prefer 19:30", date(2026, 10, 27)),
                                                           parse_wish("weekend 09:30-11:00", date(2026, 10, 27))))

    def tearDown(self):
        self.tmp.cleanup()

    def test_openings_follow_wishes(self):
        ops = self.sn.openings(datetime(2026, 10, 27, 8, 0, tzinfo=TZ), hours=48)
        got = [(o.at.strftime("%a %H:%M"), o.day.isoformat(), o.start_min) for o in ops]
        self.assertEqual(got[0], ("Tue 19:30", "2026-11-10", 19 * 60 + 30))      # only the ⭐ time on weekdays
        self.assertIn(("Wed 19:30", "2026-11-11", 19 * 60 + 30), got)
        # Saturdays/Sundays 14 days ahead open on Saturdays/Sundays: none within these 48 h (Tue-Thu)
        self.assertFalse(any(o.day.weekday() >= 5 for o in ops))
        weekend = self.sn.openings(datetime(2026, 10, 31, 8, 0, tzinfo=TZ), hours=4)
        self.assertEqual([(o.at.strftime("%H:%M"), o.day.isoformat()) for o in weekend],
                         [("09:30", "2026-11-14"), ("10:00", "2026-11-14"), ("10:30", "2026-11-14"), ("11:00", "2026-11-14")])

    def test_grabs_slot_when_it_appears(self):
        calls = {"n": 0}
        real = self.client.get_availability
        def appears_on_second_try(tenant, day):
            calls["n"] += 1
            if calls["n"] < 2:
                return []
            return [avail("c2", "2026-11-10", ("18:30:00", 90, "45 EUR"))]    # 19:30 local
        self.client.get_availability = appears_on_second_try
        self.assertTrue(self.sn.maybe_fire())
        self.assertEqual(self.slept, [40.5, 1.0])                     # 19:30:00.5, then 19:30:01.5
        self.assertEqual(self.bm.state["bookings"][0]["day"], "2026-11-10")
        self.assertIn("⚡ Grabbed at opening (+1.5s)", self.telegram.sent[-1])
        self.assertFalse(self.sn.maybe_fire())                        # done for this opening

    def test_gives_up_after_30s(self):
        self.assertTrue(self.sn.maybe_fire())
        self.assertEqual(self.slept, [40.5, 1.0, 1.5, 3.0, 4.0, 10.0, 10.0])   # +0.5 ... +30 s
        self.assertIn("👀 Watched Tue 10 Nov 19:30", self.telegram.sent[-1])
        self.assertIn("never seen free", self.telegram.sent[-1])
        self.assertEqual(self.bm.state["bookings"], [])

    def test_clock_correction(self):
        self.watcher.client.clock_offset_s = 2.0                       # Playtomic is 2 s ahead of the laptop
        self.sn.maybe_fire()
        self.assertEqual(self.slept[0], 38.5)                          # fires at laptop 19:29:58.5 = server 19:30:00.5

    def test_wallet_warning_before_opening(self):
        self.bm.state["wallet"] = {"balance": 11.84, "at": "2026-10-27T14:02:00+01:00"}
        self.now = datetime(2026, 10, 27, 17, 0, tzinfo=TZ)
        self.sn.wallet_check()
        self.assertIn("💰 Wallet padel amigos: €11.84 (as of Tue 14:02). About €17.50 is needed for Tue 10 Nov 19:30 "
                      "(opens Tue 19:30)", self.telegram.sent[-1])                  # €70 limit / 4 players
        self.sn.wallet_check()
        self.assertEqual(sum("💰" in m for m in self.telegram.sent), 1)              # once a day
        self.bm.state["wallet"]["balance"] = 30
        self.sn.wallet_warned = None
        self.sn.wallet_check()
        self.assertEqual(sum("💰" in m for m in self.telegram.sent), 1)              # enough: silent

    def test_needs_automatic_booking(self):
        self.bm.state["auto"] = False
        self.assertIsNone(self.sn.next_opening(self.now))
        self.assertFalse(self.sn.maybe_fire())

    def test_wakes_up_in_time(self):
        early = datetime(2026, 10, 27, 19, 0, tzinfo=TZ)
        self.assertEqual(self.sn.seconds_until_prepare(early), 30 * 60 - 40)
