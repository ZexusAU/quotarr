"""End-to-end check: run fake_seerr.py and quotarr.py as real processes, then use the web API.

    python tests/e2e_check.py

Not part of `python -m unittest` (it starts processes and takes a few seconds).
Uses throwaway test values only.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEERR_PORT, QUOTARR_PORT = 15055, 18484
API_KEY, PASSWORD = "demo-key", "demo-password"
BASE = f"http://127.0.0.1:{QUOTARR_PORT}"


def call(method, path, body=None, cookie=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), e.headers


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main():
    data_dir = tempfile.mkdtemp(prefix="quotarr-e2e-")
    env = {**os.environ, "SEERR_URL": f"http://127.0.0.1:{SEERR_PORT}", "SEERR_API_KEY": API_KEY,
           "QUOTARR_PASSWORD": PASSWORD, "POLL_SECONDS": "5", "PORT": str(QUOTARR_PORT),
           "DATA_DIR": data_dir, "PYTHONUNBUFFERED": "1"}
    fake = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_seerr.py"), "--port", str(SEERR_PORT),
                             "--api-key", API_KEY], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    app = subprocess.Popen([sys.executable, str(ROOT / "quotarr.py")], env=env, cwd=str(ROOT),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ok = True
    try:
        assert wait_for(lambda: call("GET", "/healthz")[0] == 200), "quotarr did not start"
        print("healthz:", call("GET", "/healthz")[1])
        print("status without login:", call("GET", "/api/status")[0])
        print("wrong password:", call("POST", "/api/login", {"password": "nope"})[0])
        status, _, headers = call("POST", "/api/login", {"password": PASSWORD})
        cookie = headers["Set-Cookie"].split(";")[0]
        print("login:", status)

        assert wait_for(lambda: call("GET", "/api/status", cookie=cookie)[1].get("last_poll")), "no poll happened"
        st = call("GET", "/api/status", cookie=cookie)[1]
        print("status:", {k: st[k] for k in ("version", "seerr_reachable", "pending", "last_result", "last_error")})

        print("log:")
        for e in call("GET", "/api/log?limit=20", cookie=cookie)[1]["entries"]:
            print(f"  #{e['request_id']} {e['user_name']:<7} {e['title']:<20} {e['action']:<13} {e['reason']}")

        print("users:")
        for u in call("GET", "/api/users", cookie=cookie)[1]["users"]:
            if u["exempt"]:
                print(f"  {u['displayName']:<7} exempt")
            else:
                m, t = u["usage"]["movie"], u["usage"]["tv"]
                print(f"  {u['displayName']:<7} movies {m['used']}/{m['limit']} {m['window']}, "
                      f"tv {t['used']}/{t['limit']} {t['window']}")

        s, body, _ = call("PUT", "/api/users/2/limits", {"movie": {"count": 5}}, cookie=cookie)
        print("override Priya movies=5:", s, body)
        s, _, _ = call("POST", "/webhook", {"anything": True})
        print("webhook:", s)
        time.sleep(1.5)
        print("log after override + webhook:")
        for e in call("GET", "/api/log?limit=3", cookie=cookie)[1]["entries"]:
            print(f"  #{e['request_id']} {e['user_name']:<7} {e['title']:<20} {e['action']:<13} {e['reason']}")
        print("  (held requests stay held: auto_release_held is off by default)")
        s, body, _ = call("PUT", "/api/settings", {"auto_release_held": True}, cookie=cookie)
        print("turn on auto_release_held:", s)
        call("POST", "/webhook", {})
        time.sleep(1.5)
        top = call("GET", "/api/log?limit=1", cookie=cookie)[1]["entries"][0]
        print(f"  newest: #{top['request_id']} {top['title']} {top['action']} - {top['reason']}")
    except AssertionError as e:
        ok = False
        print("E2E FAILED:", e)
    finally:
        app.terminate()
        fake.terminate()
        out, _ = app.communicate(timeout=10)
        fake.communicate(timeout=10)
        print("---- quotarr process output ----")
        print(out.strip())
        if API_KEY in out or PASSWORD in out:
            ok = False
            print("E2E FAILED: a secret appeared in the log output")
    print("E2E", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
