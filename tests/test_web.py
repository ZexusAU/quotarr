import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import quotarr
from tests.fake_seerr import APPROVED, PENDING, PERM_ADMIN, PERM_REQUEST, FakeSeerr

API_KEY = "test-api-key-not-real"
PASSWORD = "test-password-not-real"


class WebBase(unittest.TestCase):
    password = PASSWORD
    webhook_token = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quotarr-web-")
        self.fake = FakeSeerr(API_KEY)
        self.fake.add_user(1, "Admin", PERM_ADMIN)
        self.fake.add_user(2, "Priya", PERM_REQUEST)
        now = datetime.now(timezone.utc)
        self.fake.add_request(2, "movie", APPROVED, now - timedelta(days=1))
        self.fake.add_request(2, "movie", APPROVED, now - timedelta(days=2))
        seerr_url = self.fake.start()
        self.cfg = quotarr.Config(seerr_url, API_KEY, self.password, poll_seconds=30, port=0,
                                  data_dir=self.tmp, webhook_token=self.webhook_token)
        self.store = quotarr.Store(os.path.join(self.tmp, "quotarr.db"))
        self.engine = quotarr.Engine(self.store, quotarr.SeerrClient(seerr_url, API_KEY, timeout=5))
        self.triggered = threading.Event()
        poller = type("P", (), {"trigger": lambda _self: self.triggered.set()})()
        self.web = quotarr.Web(self.cfg, self.store, self.engine, poller)
        self.server = self.web.make_server("127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookie = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fake.stop()
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method, path, body=None, cookie=True, ctype="application/json"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None and ctype:
            req.add_header("Content-Type", ctype)
        if cookie and self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw, status, headers = r.read(), r.status, r.headers
        except urllib.error.HTTPError as e:
            raw, status, headers = e.read(), e.code, e.headers
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw.decode(errors="replace")
        return status, payload, headers

    def login(self):
        status, _, headers = self.call("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        return headers["Set-Cookie"]


class WebTests(WebBase):
    def test_api_requires_login(self):
        for method, path, body in (("GET", "/api/settings", None), ("GET", "/api/users", None),
                                   ("GET", "/api/log", None), ("GET", "/api/status", None),
                                   ("PUT", "/api/settings", {"movie": {"count": 9}}),
                                   ("PUT", "/api/users/2/limits", {"movie": {"count": 9}}),
                                   ("DELETE", "/api/users/2/limits", None)):
            status, _, _ = self.call(method, path, body)
            self.assertEqual(status, 401, f"{method} {path}")
        self.assertEqual(self.store.get_settings()["movie"]["count"], 3)

    def test_wrong_password_rejected(self):
        status, payload, headers = self.call("POST", "/api/login", {"password": "nope"})
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("Set-Cookie"))
        self.cookie = "quotarr_session=made-up-token"
        self.assertEqual(self.call("GET", "/api/settings")[0], 401)

    def test_failed_logins_are_rate_limited(self):
        for _ in range(5):
            self.assertEqual(self.call("POST", "/api/login", {"password": "nope"})[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"password": PASSWORD})[0], 429)

    def test_login_cookie_flags_and_logout(self):
        set_cookie = self.login()
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertEqual(self.call("GET", "/api/settings")[0], 200)
        self.assertEqual(self.call("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.call("GET", "/api/settings")[0], 401)

    def test_index_page_served(self):
        status, body, headers = self.call("GET", "/", cookie=False)
        self.assertEqual(status, 200)
        self.assertIn("Quotarr", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_settings_roundtrip_and_validation(self):
        self.login()
        status, s, _ = self.call("PUT", "/api/settings", {"movie": {"count": 5, "mode": "calendar_week"},
                                                          "auto_release_held": True})
        self.assertEqual(status, 200)
        self.assertEqual((s["movie"]["count"], s["movie"]["mode"], s["auto_release_held"]),
                         (5, "calendar_week", True))
        self.assertEqual(self.call("GET", "/api/settings")[1]["movie"]["count"], 5)
        self.assertEqual(self.call("PUT", "/api/settings", {"movie": {"count": "lots"}})[0], 400)
        self.assertEqual(self.call("PUT", "/api/settings", {"movie": {"count": 1}}, ctype="text/plain")[0], 400)

    def test_users_usage_and_limits(self):
        self.login()
        status, data, _ = self.call("GET", "/api/users")
        self.assertEqual(status, 200)
        users = {u["id"]: u for u in data["users"]}
        self.assertTrue(users[1]["exempt"])
        self.assertEqual(users[2]["usage"]["movie"]["used"], 2)
        self.assertEqual(users[2]["usage"]["movie"]["limit"], 3)
        self.assertEqual(users[2]["usage"]["tv"]["used"], 0)

        status, ov, _ = self.call("PUT", "/api/users/2/limits", {"movie": {"count": 10, "mode": None}, "tv": None})
        self.assertEqual((status, ov), (200, {"movie": {"count": 10}}))
        users = {u["id"]: u for u in self.call("GET", "/api/users")[1]["users"]}
        self.assertEqual(users[2]["usage"]["movie"]["limit"], 10)
        self.assertEqual(users[2]["overrides"], {"movie": {"count": 10}})
        self.assertEqual(self.call("PUT", "/api/users/2/limits", {"movie": {"window_days": 500}})[0], 400)
        self.assertEqual(self.call("DELETE", "/api/users/2/limits")[0], 200)
        self.assertEqual(self.store.get_override(2), {})

    def test_log_and_status(self):
        self.login()
        self.fake.add_request(2, "movie", PENDING, datetime.now(timezone.utc), title="Grain of Truth")
        self.engine.poll_once()
        status, data, _ = self.call("GET", "/api/log?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(data["entries"][0]["action"], "auto_approved")
        self.assertEqual(data["entries"][0]["title"], "Grain of Truth")
        status, st, _ = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(st["seerr_reachable"])
        self.assertEqual(st["version"], quotarr.VERSION)
        self.assertNotIn(API_KEY, json.dumps(st))

    def test_users_when_seerr_down_gives_502(self):
        self.login()
        self.fake.fail_status = 500
        status, data, _ = self.call("GET", "/api/users")
        self.assertEqual(status, 502)
        self.assertIn("HTTP 500", data["error"])

    def test_webhook_without_token_triggers_poll(self):
        req = urllib.request.Request(self.base + "/webhook", data=b"anything at all", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 202)
        self.assertTrue(self.triggered.wait(2))

    def test_healthz_is_public(self):
        self.assertEqual(self.call("GET", "/healthz", cookie=False)[0], 200)


class WebhookTokenTests(WebBase):
    webhook_token = "test-webhook-token"

    def test_webhook_token_checked(self):
        self.assertEqual(self.call("POST", "/webhook?token=wrong", {})[0], 401)
        self.assertFalse(self.triggered.is_set())
        self.assertEqual(self.call("POST", "/webhook", {})[0], 401)
        self.assertEqual(self.call("POST", "/webhook?token=test-webhook-token", {"x": 1})[0], 202)
        self.assertTrue(self.triggered.is_set())


class NoPasswordTests(WebBase):
    password = None

    def test_web_ui_refused_without_password(self):
        status, body, _ = self.call("GET", "/", cookie=False)
        self.assertEqual(status, 503)
        self.assertIn("QUOTARR_PASSWORD", body)
        self.assertEqual(self.call("POST", "/api/login", {"password": ""})[0], 503)
        self.assertEqual(self.call("GET", "/healthz")[0], 200)


if __name__ == "__main__":
    unittest.main()
