"""Offline tests: sample Playtomic replies, no network. Run: py -m unittest -v"""

from __future__ import annotations

import logging

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from padel_bot.booking import AutoBooker
from padel_bot.config import AutoBookSettings, ConfigError, load_dotenv, load_settings
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
        many = [slot] * 200
        messages = build_messages(many, "Club", lambda d: "https://x")
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= 4096 for m in messages))


class ConfigTests(unittest.TestCase):
    def test_shipped_config_loads(self):
        s = load_settings(REPO / "config.toml", require_club=False)
        self.assertEqual(s.booking_url, "{club_url}?date={date}")
        self.assertTrue(s.api_times_utc)
        self.assertFalse(s.auto_book.enabled)
        self.assertTrue(s.auto_book.dry_run)
        self.assertEqual(s.days_ahead, 14)

    def test_missing_club_is_explained(self):
        with self.assertRaisesRegex(ConfigError, "START-HERE"):
            load_settings(REPO / "config.toml")

    def test_autobook_needs_max_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = (REPO / "config.toml").read_text(encoding="utf-8").replace("enabled = false", "enabled = true")
            path = Path(tmp) / "config.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "max_price_eur"):
                load_settings(path, require_club=False)

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


class BookingTests(unittest.TestCase):
    slot = Slot("c1", "Court 1", datetime(2026, 10, 27, 17, 0, tzinfo=TZ), 90, 48.0, "48 EUR")

    def test_disabled_by_default(self):
        booker = AutoBooker(AutoBookSettings(), confirm=lambda s: self.fail("must not ask"))
        self.assertEqual(booker.consider(self.slot).action, "skipped")

    def test_requires_max_price(self):
        with self.assertRaises(ValueError):
            AutoBooker(AutoBookSettings(enabled=True), confirm=lambda s: True)

    def test_real_booking_refused_without_backend(self):
        with self.assertRaises(ValueError):
            AutoBooker(AutoBookSettings(enabled=True, dry_run=False, max_price_eur=60), confirm=lambda s: True)

    def test_price_cap_and_dry_run(self):
        booker = AutoBooker(AutoBookSettings(enabled=True, max_price_eur=40), confirm=lambda s: self.fail("must not ask"))
        self.assertEqual(booker.consider(self.slot).action, "skipped")  # €48 > €40
        booker = AutoBooker(AutoBookSettings(enabled=True, max_price_eur=60), confirm=lambda s: self.fail("must not ask"))
        self.assertEqual(booker.consider(self.slot).action, "dry_run")

    def test_confirmation_required(self):
        class Backend:
            booked = []
            def book(self, slot):
                self.booked.append(slot)
                return "ref-1"
        backend = Backend()
        live = AutoBookSettings(enabled=True, dry_run=False, max_price_eur=60)
        self.assertEqual(AutoBooker(live, confirm=lambda s: False, backend=backend).consider(self.slot).action, "skipped")
        self.assertEqual(backend.booked, [])
        self.assertEqual(AutoBooker(live, confirm=lambda s: True, backend=backend).consider(self.slot).action, "booked")


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
        self.fail = False

    def send(self, text):
        if self.fail:
            raise NotifyError("offline")
        self.sent.append(text)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = settings(Path(self.tmp.name))
        self.now = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)  # Monday
        self.client = FakeClient()
        self.notifier = FakeNotifier()
        self.watcher = Watcher(self.s, self.client, CLUB, self.notifier, SeenState(self.s.state_file), TZ,
                               AutoBooker(self.s.auto_book, confirm=lambda s: False),
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
                        self.watcher.booker, clock=lambda: self.now, sleep=lambda s: None)
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
        ranged = parse_wish("mon-fri 16:00-20:30 prefer 16:30,17:00")
        self.assertEqual(describe(ranged), "Mon-Fri 16:00-20:30 (starts 16:00-19:00), 90 min, ⭐ 16:30 17:00")
        self.assertEqual(parse_wish("sat 10:00 120").durations, frozenset({120}))
        for bad in ("tue", "xyz 17:00", "tue 25:00", "tue 17:00-17:30", "tue 17:00 abc",
                    "mon 16:00-18:00 prefer 17:30", "sun 23:30"):
            with self.assertRaises(WishError, msg=bad):
                parse_wish(bad)

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
                               AutoBooker(self.s.auto_book, confirm=lambda s: False),
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
