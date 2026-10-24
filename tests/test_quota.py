import io
import logging
import os
import shutil
import socket
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import quotarr
from tests.fake_seerr import (APPROVED, COMPLETED, DECLINED, FAILED, PENDING, PERM_ADMIN,
                              PERM_MANAGE_REQUESTS, PERM_REQUEST, FakeSeerr)

API_KEY = "test-api-key-not-real"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)  # a Wednesday


def local_dt(y, mo, d, h=0, mi=0):
    """An aware UTC datetime for a wall-clock time in the machine's local zone."""
    ts = time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))
    return datetime.fromtimestamp(ts, timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quotarr-test-")
        self.fake = FakeSeerr(API_KEY)
        self.fake.add_user(1, "Admin", PERM_ADMIN)
        self.fake.add_user(2, "Priya", PERM_REQUEST)
        self.fake.add_user(3, "Sam", PERM_REQUEST)
        self.url = self.fake.start()
        self.store = quotarr.Store(os.path.join(self.tmp, "quotarr.db"))
        self.client = quotarr.SeerrClient(self.url, API_KEY, timeout=5)
        self.now = NOW
        self.engine = quotarr.Engine(self.store, self.client, now=lambda: self.now)

    def tearDown(self):
        self.fake.stop()
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settings(self, **patch):
        new = quotarr.validate_settings_patch(self.store.get_settings(), patch)
        self.store.save_settings(new)

    def limit(self, media="movie", count=1, days=7, mode="rolling", **extra):
        self.settings(**{media: {"count": count, "window_days": days, "mode": mode, **extra}})

    def actions(self, rid):
        return [e["action"] for e in self.store.recent_log(1000) if e["request_id"] == rid]

    def poll(self):
        res = self.engine.poll_once()
        self.assertTrue(res["ok"], res)
        return res


class QuotaTests(Base):
    def test_under_limit_is_approved(self):
        self.limit(count=2)
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=1))
        rid = self.fake.add_request(2, "movie", PENDING, NOW - timedelta(minutes=5), title="Dovetail")
        res = self.poll()
        self.assertEqual(res["approved"], 1)
        self.assertEqual(self.fake.approve_calls, [rid])
        self.assertEqual(self.fake.status_of(rid), APPROVED)
        entry = self.store.recent_log()[0]
        self.assertEqual((entry["action"], entry["title"], entry["user_name"]), ("auto_approved", "Dovetail", "Priya"))
        self.assertIn("2/2", entry["reason"])

    def test_at_limit_is_held_and_logged_once(self):
        self.limit(count=1)
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=1))
        rid = self.fake.add_request(2, "movie", PENDING, NOW - timedelta(minutes=5))
        for _ in range(3):
            self.poll()
        self.assertEqual(self.fake.approve_calls, [])
        self.assertEqual(self.fake.status_of(rid), PENDING)
        self.assertEqual(self.actions(rid), ["held"])

    def test_completed_counts(self):
        self.limit(count=1)
        self.fake.add_request(2, "movie", COMPLETED, NOW - timedelta(days=1))
        rid = self.fake.add_request(2, "movie", PENDING, NOW)
        self.poll()
        self.assertEqual(self.actions(rid), ["held"])

    def test_declined_and_failed_do_not_count(self):
        self.limit(count=1)
        self.fake.add_request(2, "movie", DECLINED, NOW - timedelta(days=1))
        self.fake.add_request(2, "movie", FAILED, NOW - timedelta(days=1))
        rid = self.fake.add_request(2, "movie", PENDING, NOW)
        self.poll()
        self.assertEqual(self.fake.approve_calls, [rid])

    def test_outside_rolling_window_does_not_count(self):
        self.limit(count=1, days=7)
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=8))
        rid = self.fake.add_request(2, "movie", PENDING, NOW)
        self.poll()
        self.assertEqual(self.fake.approve_calls, [rid])

    def test_inside_rolling_window_counts(self):
        self.limit(count=1, days=7)
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=6, hours=23))
        rid = self.fake.add_request(2, "movie", PENDING, NOW)
        self.poll()
        self.assertEqual(self.actions(rid), ["held"])

    def test_calendar_week_resets_on_monday(self):
        self.limit(count=1, mode="calendar_week")
        # 2026-10-18 is a Sunday, 2026-10-19 a Monday (local time)
        self.fake.add_request(2, "movie", APPROVED, local_dt(2026, 10, 18, 22, 0))
        r1 = self.fake.add_request(2, "movie", PENDING, local_dt(2026, 10, 18, 23, 0))
        self.now = local_dt(2026, 10, 18, 23, 30)  # still Sunday: same week, slot used
        self.poll()
        self.assertEqual(self.actions(r1), ["held"])
        r2 = self.fake.add_request(2, "movie", PENDING, local_dt(2026, 10, 19, 0, 5))
        self.now = local_dt(2026, 10, 19, 0, 10)  # Monday 00:10: new week
        self.poll()
        self.assertEqual(self.fake.approve_calls, [r2])

    def test_window_start_calendar_week_is_local_monday_midnight(self):
        lim = {"mode": "calendar_week", "window_days": 7}
        start = quotarr.window_start(lim, local_dt(2026, 10, 24, 15, 0))  # Saturday
        self.assertEqual(start, local_dt(2026, 10, 19, 0, 0))
        start = quotarr.window_start(lim, local_dt(2026, 10, 19, 0, 0))  # Monday midnight exactly
        self.assertEqual(start, local_dt(2026, 10, 19, 0, 0))

    def test_user_override_beats_default(self):
        self.limit(count=1)
        self.store.set_override(2, {"movie": {"count": 3}}, NOW)  # partial: inherits window and mode
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=1))
        self.fake.add_request(3, "movie", APPROVED, NOW - timedelta(days=1))
        priya = self.fake.add_request(2, "movie", PENDING, NOW)
        sam = self.fake.add_request(3, "movie", PENDING, NOW)
        self.poll()
        self.assertEqual(self.fake.approve_calls, [priya])
        self.assertEqual(self.actions(sam), ["held"])
        eff = quotarr.effective_limit(self.store.get_settings(), self.store.get_override(2), "movie")
        self.assertEqual((eff["count"], eff["window_days"], eff["mode"]), (3, 7, "rolling"))

    def test_admin_and_manager_requests_are_skipped(self):
        self.fake.add_user(4, "Jordan", PERM_MANAGE_REQUESTS | PERM_REQUEST)
        self.limit(count=0)
        a = self.fake.add_request(1, "movie", PENDING, NOW)
        m = self.fake.add_request(4, "movie", PENDING, NOW)
        res = self.poll()
        self.assertEqual(res["skipped"], 2)
        self.assertEqual(self.fake.approve_calls, [])
        self.assertEqual(self.actions(a) + self.actions(m), [])
        self.assertEqual(self.fake.status_of(a), PENDING)

    def test_two_pending_one_slot_only_oldest_approved(self):
        self.limit(count=2)
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=1))
        # the newer one is created first so it gets the lower id; order must follow createdAt
        newer = self.fake.add_request(2, "movie", PENDING, NOW - timedelta(minutes=1))
        older = self.fake.add_request(2, "movie", PENDING, NOW - timedelta(minutes=10))
        self.poll()
        self.assertEqual(self.fake.approve_calls, [older])
        self.assertEqual(self.actions(newer), ["held"])

    def test_movie_and_tv_limits_are_independent(self):
        self.limit("movie", count=1)
        self.limit("tv", count=1)
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=1))
        movie = self.fake.add_request(2, "movie", PENDING, NOW - timedelta(minutes=2))
        tv = self.fake.add_request(2, "tv", PENDING, NOW - timedelta(minutes=1), seasons=(1, 2, 3))
        self.poll()
        self.assertEqual(self.fake.approve_calls, [tv])
        self.assertEqual(self.actions(movie), ["held"])

    def test_tv_counts_one_per_request_not_per_season(self):
        self.limit("tv", count=2)
        self.fake.add_request(2, "tv", APPROVED, NOW - timedelta(days=1), seasons=(1, 2, 3, 4))
        rid = self.fake.add_request(2, "tv", PENDING, NOW)
        self.poll()
        self.assertEqual(self.fake.approve_calls, [rid])

    def test_auto_release_held_off_then_on(self):
        self.limit(count=1, days=7)
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=6))
        rid = self.fake.add_request(2, "movie", PENDING, NOW)
        self.poll()
        self.assertEqual(self.actions(rid), ["held"])
        self.now = NOW + timedelta(days=2)  # the old approval has left the window
        self.poll()
        self.assertEqual(self.fake.approve_calls, [], "auto_release_held is off by default")
        self.settings(auto_release_held=True)
        self.poll()
        self.assertEqual(self.fake.approve_calls, [rid])
        self.assertEqual(self.actions(rid), ["auto_approved", "held"])
        self.assertIn("released", self.store.recent_log()[0]["reason"])

    def test_auto_release_on_still_holds_while_full(self):
        self.settings(auto_release_held=True)
        self.limit(count=1)
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=1))
        rid = self.fake.add_request(2, "movie", PENDING, NOW)
        self.poll()
        self.poll()
        self.assertEqual(self.fake.approve_calls, [])
        self.assertEqual(self.actions(rid), ["held"])

    def test_count_manual_approvals_setting(self):
        self.limit(count=1)
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=2))
        held = self.fake.add_request(2, "movie", PENDING, NOW - timedelta(days=1))
        self.poll()
        self.assertEqual(self.actions(held), ["held"])
        self.fake.set_status(held, APPROVED)  # admin approves it by hand in Seerr
        # the first approval leaves the window; only the manual one is left in it
        self.now = NOW + timedelta(days=5, hours=1)
        new = self.fake.add_request(2, "movie", PENDING, self.now)
        self.poll()
        self.assertEqual(self.actions(new), ["held"], "manual approvals count by default")
        self.settings(count_manual_approvals=False)
        self.store.add_log(self.now, "error", request_id=None, reason="x")  # noise in the log is fine
        newer = self.fake.add_request(2, "movie", PENDING, self.now + timedelta(minutes=1))
        self.poll()
        self.assertEqual(self.fake.approve_calls, [newer])

    def test_4k_together_by_default_and_separate_when_set(self):
        self.limit(count=1)
        self.fake.add_request(2, "movie", APPROVED, NOW - timedelta(days=1), is4k=False)
        r4k = self.fake.add_request(2, "movie", PENDING, NOW, is4k=True)
        self.poll()
        self.assertEqual(self.actions(r4k), ["held"])
        self.settings(separate_4k=True)
        r4k2 = self.fake.add_request(2, "movie", PENDING, NOW + timedelta(minutes=1), is4k=True)
        self.poll()
        self.assertEqual(self.fake.approve_calls, [r4k2])

    def test_disabled_holds_everything_and_unlimited_approves(self):
        self.settings(movie={"enabled": False}, tv={"unlimited": True})
        m = self.fake.add_request(2, "movie", PENDING, NOW)
        self.fake.add_request(2, "tv", APPROVED, NOW - timedelta(days=1))
        self.fake.add_request(2, "tv", APPROVED, NOW - timedelta(days=1))
        t = self.fake.add_request(2, "tv", PENDING, NOW)
        self.poll()
        self.assertEqual(self.actions(m), ["held"])
        self.assertEqual(self.fake.approve_calls, [t])

    def test_override_can_turn_user_unlimited(self):
        self.limit(count=0)
        self.store.set_override(3, {"movie": {"unlimited": True}}, NOW)
        rid = self.fake.add_request(3, "movie", PENDING, NOW)
        self.poll()
        self.assertEqual(self.fake.approve_calls, [rid])

    def test_title_lookup_fails_soft(self):
        self.fake.title_fail = True
        rid = self.fake.add_request(2, "movie", PENDING, NOW, tmdb_id=4242)
        self.poll()
        self.assertEqual(self.fake.approve_calls, [rid])
        self.assertEqual(self.store.recent_log()[0]["title"], "TMDB 4242")

    def test_approve_conflict_is_skipped(self):
        rid = self.fake.add_request(2, "movie", PENDING, NOW)
        real = self.client.approve

        def race(request_id):
            self.fake.set_status(request_id, DECLINED)  # admin declined it a moment earlier
            return real(request_id)

        self.client.approve = race
        res = self.poll()
        self.assertEqual((res["approved"], res["errors"]), (0, 0))
        self.assertEqual(self.actions(rid), [])

    def test_log_prune_after_90_days(self):
        self.store.add_log(NOW - timedelta(days=91), "held", request_id=1, reason="old")
        self.store.add_log(NOW - timedelta(days=10), "held", request_id=2, reason="new")
        self.assertEqual(self.store.prune(NOW), 1)
        self.assertEqual([e["reason"] for e in self.store.recent_log()], ["new"])


class FailureTests(Base):
    def free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_seerr_down_does_not_crash_and_sets_status(self):
        client = quotarr.SeerrClient(f"http://127.0.0.1:{self.free_port()}", API_KEY, timeout=2)
        engine = quotarr.Engine(self.store, client, now=lambda: NOW)
        res = engine.poll_once()
        self.assertFalse(res["ok"])
        self.assertFalse(engine.status["seerr_reachable"])
        self.assertIn("Cannot reach Seerr", engine.status["last_error"])

    def test_seerr_http_500_sets_error(self):
        self.fake.fail_status = 500
        res = self.engine.poll_once()
        self.assertFalse(res["ok"])
        self.assertIn("HTTP 500", self.engine.status["last_error"])
        self.fake.fail_status = None
        self.assertTrue(self.engine.poll_once()["ok"])
        self.assertIsNone(self.engine.status["last_error"])

    def test_poller_thread_survives_failures(self):
        self.fake.fail_status = 503
        engine = quotarr.Engine(self.store, self.client)
        poller = quotarr.Poller(engine, interval=0.05)
        poller.MAX_BACKOFF = 0.1
        poller.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and poller.failures < 3:
                time.sleep(0.05)
            self.assertTrue(poller.is_alive())
            self.assertGreaterEqual(poller.failures, 3)
            # Seerr comes back: the loop recovers on its own
            self.fake.fail_status = None
            poller.trigger()
            deadline = time.time() + 5
            while time.time() < deadline and poller.failures != 0:
                time.sleep(0.05)
            self.assertTrue(engine.status["seerr_reachable"])
            self.assertEqual(poller.failures, 0)
        finally:
            poller.stop()
            poller.join(2)

    def test_bad_api_key_is_reported(self):
        client = quotarr.SeerrClient(self.url, "wrong-key", timeout=2)
        engine = quotarr.Engine(self.store, client, now=lambda: NOW)
        res = engine.poll_once()
        self.assertFalse(res["ok"])
        self.assertIn("HTTP 403", engine.status["last_error"])

    def test_api_key_never_logged(self):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        logger = logging.getLogger("quotarr")
        old = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            self.fake.add_request(2, "movie", PENDING, NOW)
            self.engine.poll_once()
            self.fake.fail_status = 500
            self.engine.poll_once()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old)
        self.assertTrue(buf.getvalue())
        self.assertNotIn(API_KEY, buf.getvalue())
        self.assertNotIn(API_KEY, repr(self.client))


class ConfigTests(unittest.TestCase):
    def test_requires_api_key(self):
        with self.assertRaises(quotarr.ConfigError):
            quotarr.Config.from_env({})

    def test_api_key_file_and_defaults(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "key")
            with open(p, "w") as f:
                f.write("from-file-key\n")
            cfg = quotarr.Config.from_env({"SEERR_API_KEY_FILE": p})
            self.assertEqual(cfg.api_key, "from-file-key")
            self.assertEqual((cfg.seerr_url, cfg.poll_seconds, cfg.port, cfg.data_dir),
                             ("http://seerr:5055", 30, 8484, "/data"))
            self.assertIsNone(cfg.password)
            self.assertNotIn("from-file-key", repr(cfg))
        finally:
            shutil.rmtree(d)

    def test_bad_numbers_rejected(self):
        with self.assertRaises(quotarr.ConfigError):
            quotarr.Config.from_env({"SEERR_API_KEY": "k", "POLL_SECONDS": "fast"})

    def test_settings_validation(self):
        cur = quotarr.DEFAULT_SETTINGS
        for bad in ({"movie": {"count": -1}}, {"movie": {"mode": "monthly"}}, {"tv": {"window_days": 0}},
                    {"auto_release_held": "yes"}, {"nope": True}, {"movie": {"count": None}}):
            with self.assertRaises(quotarr.ValidationError, msg=bad):
                quotarr.validate_settings_patch(cur, bad)
        new = quotarr.validate_settings_patch(cur, {"movie": {"count": 5}})
        self.assertEqual(new["movie"]["count"], 5)
        self.assertEqual(new["movie"]["window_days"], 7)
        self.assertEqual(cur["movie"]["count"], 3, "defaults must not be mutated")


if __name__ == "__main__":
    unittest.main()
