#!/usr/bin/env python3
"""Quotarr: per-user auto-approval quotas for Seerr.

Seerr's own quota refuses over-quota requests. Quotarr instead lets every
request land as PENDING (users have no Auto-Approve permission), then approves
the ones that are under the user's limit and leaves the rest pending for an
admin to approve or decline in Seerr.

Standard library only. Python 3.12+.
"""

from __future__ import annotations

import copy
import hmac
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0.0"

# server/constants/media.ts (MediaRequestStatus)
PENDING, APPROVED, DECLINED, FAILED, COMPLETED = 1, 2, 3, 4, 5
# Requests that use up a slot. FAILED and DECLINED do not (see README).
COUNTED_STATUSES = (APPROVED, COMPLETED)

# server/lib/permissions.ts
PERM_ADMIN = 2
PERM_MANAGE_REQUESTS = 16
EXEMPT_PERMS = PERM_ADMIN | PERM_MANAGE_REQUESTS

MEDIA_TYPES = ("movie", "tv")
MODES = ("rolling", "calendar_week")
LOG_KEEP_DAYS = 90
MAX_WINDOW_DAYS = 90  # held/approved history lives in the log, which is kept 90 days
MAX_COUNT = 10000
SESSION_SECONDS = 7 * 24 * 3600
MAX_BODY = 64 * 1024

DEFAULT_SETTINGS = {
    "movie": {"enabled": True, "unlimited": False, "count": 3, "window_days": 7, "mode": "rolling"},
    "tv": {"enabled": True, "unlimited": False, "count": 2, "window_days": 7, "mode": "rolling"},
    "count_manual_approvals": True,
    "separate_4k": False,
    "auto_release_held": False,
}
BOOL_SETTINGS = ("count_manual_approvals", "separate_4k", "auto_release_held")
LIMIT_FIELDS = ("enabled", "unlimited", "count", "window_days", "mode")

log = logging.getLogger("quotarr")


# ---------------------------------------------------------------- config


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, seerr_url, api_key, password, poll_seconds=30, port=8484,
                 data_dir="/data", webhook_token=None, static_dir=None):
        self.seerr_url = seerr_url.rstrip("/")
        self.api_key = api_key
        self.password = password or None
        self.poll_seconds = poll_seconds
        self.port = port
        self.data_dir = data_dir
        self.webhook_token = webhook_token or None
        self.static_dir = static_dir or str(Path(__file__).resolve().parent / "static")

    def __repr__(self):  # never show secrets
        return (f"Config(seerr_url={self.seerr_url!r}, poll_seconds={self.poll_seconds}, "
                f"port={self.port}, data_dir={self.data_dir!r})")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        key = (env.get("SEERR_API_KEY") or "").strip()
        key_file = (env.get("SEERR_API_KEY_FILE") or "").strip()
        if not key and key_file:
            try:
                key = Path(key_file).read_text(encoding="utf-8").strip()
            except OSError as e:
                raise ConfigError(f"Cannot read SEERR_API_KEY_FILE ({key_file}): {e.strerror}")
        if not key:
            raise ConfigError("SEERR_API_KEY (or SEERR_API_KEY_FILE) is required. "
                              "Find it in Seerr under Settings > General > API Key.")

        def as_int(name, default, low, high):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                val = int(raw)
            except ValueError:
                raise ConfigError(f"{name} must be a whole number, got {raw!r}")
            if not low <= val <= high:
                raise ConfigError(f"{name} must be between {low} and {high}")
            return val

        return cls(
            seerr_url=(env.get("SEERR_URL") or "http://seerr:5055").strip(),
            api_key=key,
            password=env.get("QUOTARR_PASSWORD") or None,
            poll_seconds=as_int("POLL_SECONDS", 30, 5, 86400),
            port=as_int("PORT", 8484, 1, 65535),
            data_dir=(env.get("DATA_DIR") or "/data").strip(),
            webhook_token=env.get("WEBHOOK_TOKEN") or None,
        )


# ---------------------------------------------------------------- helpers


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value):
    """Parse a Seerr timestamp (ISO 8601, usually with Z). Naive means UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace(" ", "T", 1))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def request_type(req):
    t = req.get("type") or (req.get("media") or {}).get("mediaType")
    return t if t in MEDIA_TYPES else None


def window_start(limit, now):
    """Start of the counting window as an aware UTC datetime."""
    if limit["mode"] == "calendar_week":
        local = now.astimezone()  # honours TZ
        monday = local.date() - timedelta(days=local.weekday())
        # mktime with isdst=-1 gets local midnight right across DST changes
        ts = time.mktime((monday.year, monday.month, monday.day, 0, 0, 0, 0, 0, -1))
        return datetime.fromtimestamp(ts, timezone.utc)
    return now - timedelta(days=int(limit["window_days"]))


def window_label(limit):
    if limit["mode"] == "calendar_week":
        return "this week"
    d = int(limit["window_days"])
    return "in the last day" if d == 1 else f"in the last {d} days"


def effective_limit(settings, override, media):
    eff = dict(settings[media])
    for k, v in ((override or {}).get(media) or {}).items():
        if k in LIMIT_FIELDS and v is not None:
            eff[k] = v
    return eff


class ValidationError(Exception):
    pass


def _check_limit_field(key, val):
    if key in ("enabled", "unlimited"):
        if not isinstance(val, bool):
            raise ValidationError(f"{key} must be true or false")
    elif key == "count":
        if isinstance(val, bool) or not isinstance(val, int) or not 0 <= val <= MAX_COUNT:
            raise ValidationError(f"count must be a whole number from 0 to {MAX_COUNT}")
    elif key == "window_days":
        if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= MAX_WINDOW_DAYS:
            raise ValidationError(f"window_days must be a whole number from 1 to {MAX_WINDOW_DAYS}")
    elif key == "mode":
        if val not in MODES:
            raise ValidationError("mode must be 'rolling' or 'calendar_week'")
    else:
        raise ValidationError(f"unknown field {key!r}")


def validate_settings_patch(current, patch):
    """Merge a partial settings patch into the current settings. Returns the new full settings."""
    if not isinstance(patch, dict):
        raise ValidationError("settings must be a JSON object")
    new = copy.deepcopy(current)
    for key, val in patch.items():
        if key in MEDIA_TYPES:
            if not isinstance(val, dict):
                raise ValidationError(f"{key} must be an object")
            for f, v in val.items():
                if v is None:
                    raise ValidationError(f"{key}.{f} cannot be empty in the defaults")
                _check_limit_field(f, v)
                new[key][f] = v
        elif key in BOOL_SETTINGS:
            if not isinstance(val, bool):
                raise ValidationError(f"{key} must be true or false")
            new[key] = val
        else:
            raise ValidationError(f"unknown setting {key!r}")
    return new


def validate_override(data):
    """A user override: {"movie": {...}, "tv": {...}}, any field may be null or absent (= inherit)."""
    if not isinstance(data, dict):
        raise ValidationError("limits must be a JSON object")
    out = {}
    for key, val in data.items():
        if key not in MEDIA_TYPES:
            raise ValidationError(f"unknown media type {key!r}")
        if val is None:
            continue
        if not isinstance(val, dict):
            raise ValidationError(f"{key} must be an object")
        clean = {}
        for f, v in val.items():
            if v is None:
                continue
            _check_limit_field(f, v)
            clean[f] = v
        if clean:
            out[key] = clean
    return out


# ---------------------------------------------------------------- storage


class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_limits (
                    user_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    enabled INTEGER,
                    unlimited INTEGER,
                    count INTEGER,
                    window_days INTEGER,
                    mode TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, media_type)
                );
                CREATE TABLE IF NOT EXISTS log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL,
                    request_id INTEGER,
                    user_id INTEGER,
                    user_name TEXT,
                    title TEXT,
                    media_type TEXT,
                    is4k INTEGER NOT NULL DEFAULT 0,
                    action TEXT NOT NULL CHECK (action IN ('auto_approved','held','error')),
                    reason TEXT
                );
                CREATE INDEX IF NOT EXISTS log_request ON log (request_id, action);
                CREATE INDEX IF NOT EXISTS log_time ON log (time);
            """)

    def close(self):
        with self.lock:
            self.db.close()

    # settings
    def get_settings(self):
        s = copy.deepcopy(DEFAULT_SETTINGS)
        with self.lock:
            rows = self.db.execute("SELECT key, value FROM settings").fetchall()
        for r in rows:
            try:
                val = json.loads(r["value"])
            except ValueError:
                continue
            if r["key"] in MEDIA_TYPES and isinstance(val, dict):
                s[r["key"]].update({k: v for k, v in val.items() if k in LIMIT_FIELDS})
            elif r["key"] in BOOL_SETTINGS:
                s[r["key"]] = bool(val)
        return s

    def save_settings(self, settings):
        with self.lock:
            self.db.execute("BEGIN")
            for key in (*MEDIA_TYPES, *BOOL_SETTINGS):
                self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                (key, json.dumps(settings[key])))
            self.db.execute("COMMIT")

    # per-user overrides
    @staticmethod
    def _row_to_limit(r):
        out = {}
        for f in LIMIT_FIELDS:
            v = r[f]
            if v is None:
                continue
            out[f] = bool(v) if f in ("enabled", "unlimited") else v
        return out

    def all_overrides(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM user_limits").fetchall()
        out = {}
        for r in rows:
            lim = self._row_to_limit(r)
            if lim:
                out.setdefault(r["user_id"], {})[r["media_type"]] = lim
        return out

    def get_override(self, user_id):
        return self.all_overrides().get(user_id, {})

    def set_override(self, user_id, override, now):
        with self.lock:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM user_limits WHERE user_id = ?", (user_id,))
            for media, lim in override.items():
                self.db.execute(
                    "INSERT INTO user_limits (user_id, media_type, enabled, unlimited, count, "
                    "window_days, mode, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, media,
                     None if lim.get("enabled") is None else int(lim["enabled"]),
                     None if lim.get("unlimited") is None else int(lim["unlimited"]),
                     lim.get("count"), lim.get("window_days"), lim.get("mode"), iso(now)))
            self.db.execute("COMMIT")

    def delete_override(self, user_id):
        with self.lock:
            cur = self.db.execute("DELETE FROM user_limits WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0

    # log
    def add_log(self, now, action, request_id=None, user_id=None, user_name=None,
                title=None, media_type=None, is4k=False, reason=None):
        with self.lock:
            self.db.execute(
                "INSERT INTO log (time, request_id, user_id, user_name, title, media_type, is4k, "
                "action, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (iso(now), request_id, user_id, user_name, title, media_type, int(bool(is4k)),
                 action, reason))

    def ids_with_action(self, action):
        with self.lock:
            rows = self.db.execute("SELECT DISTINCT request_id FROM log WHERE action = ? "
                                   "AND request_id IS NOT NULL", (action,)).fetchall()
        return {r[0] for r in rows}

    def manual_ids(self):
        """Requests Quotarr held that it never auto-approved itself.
        If such a request is now APPROVED or COMPLETED, an admin approved it by hand."""
        return self.ids_with_action("held") - self.ids_with_action("auto_approved")

    def recent_log(self, limit=200):
        with self.lock:
            rows = self.db.execute("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "is4k": bool(r["is4k"])} for r in rows]

    def prune(self, now, keep_days=LOG_KEEP_DAYS):
        cutoff = iso(now - timedelta(days=keep_days))
        with self.lock:
            cur = self.db.execute("DELETE FROM log WHERE time < ?", (cutoff,))
        return cur.rowcount


# ---------------------------------------------------------------- Seerr client


class SeerrError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status  # None means a network-level failure


class SeerrClient:
    PAGE = 100
    MAX_PAGES = 200  # safety net against a misbehaving server

    def __init__(self, base_url, api_key, timeout=15):
        self.base = base_url.rstrip("/")
        self._key = api_key
        self.timeout = timeout

    def __repr__(self):
        return f"SeerrClient({self.base!r})"

    def _req(self, method, path, params=None):
        url = f"{self.base}/api/v1{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None,
                                     headers={"X-Api-Key": self._key, "Accept": "application/json",
                                              "User-Agent": f"Quotarr/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                body = json.loads(e.read() or b"{}")
                msg = body.get("message") or body.get("error") or ""
            except (ValueError, AttributeError, OSError):
                pass
            hint = " (check SEERR_API_KEY)" if e.code in (401, 403) and path != "/request" else ""
            raise SeerrError(f"Seerr returned HTTP {e.code} for {method} {path}"
                             f"{': ' + str(msg)[:200] if msg else ''}{hint}", e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", None) or e
            raise SeerrError(f"Cannot reach Seerr at {self.base}: {reason}") from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise SeerrError(f"Seerr returned something that is not JSON for {method} {path}") from None

    def _paged(self, path, params=None, stop=None):
        skip = 0
        out = []
        for _ in range(self.MAX_PAGES):
            data = self._req("GET", path, {**(params or {}), "take": self.PAGE, "skip": skip})
            results = data.get("results") or []
            out.extend(results)
            total = (data.get("pageInfo") or {}).get("results")
            skip += len(results)
            if not results or len(results) < self.PAGE or (total is not None and skip >= total):
                break
            if stop and stop(results):
                break
        return out

    def pending_requests(self):
        reqs = self._paged("/request", {"filter": "pending", "sort": "added", "sortDirection": "asc"})
        # oldest first; sort here too so we never rely on server ordering
        reqs = [r for r in reqs if r.get("status") == PENDING]
        reqs.sort(key=lambda r: (parse_time(r.get("createdAt")) or datetime.max.replace(tzinfo=timezone.utc),
                                 r.get("id") or 0))
        return reqs

    def users(self):
        return self._paged("/user", {"sort": "displayname"})

    def user_requests(self, user_id, since):
        """A user's requests, newest first, fetched until the page is older than `since`.
        Seerr orders this list by request id descending, which tracks createdAt."""
        def older(page):
            last = parse_time(page[-1].get("createdAt"))
            return last is not None and last < since
        return self._paged(f"/user/{int(user_id)}/requests", stop=older)

    def approve(self, request_id):
        return self._req("POST", f"/request/{int(request_id)}/approve")

    def title(self, media_type, tmdb_id):
        data = self._req("GET", f"/{'movie' if media_type == 'movie' else 'tv'}/{int(tmdb_id)}")
        return data.get("title") or data.get("name") or data.get("originalTitle") or data.get("originalName")


# ---------------------------------------------------------------- engine


class Engine:
    def __init__(self, store, client, now=None):
        self.store = store
        self.client = client
        self.now = now or utcnow
        self.lock = threading.Lock()  # one poll at a time
        self._titles = {}
        self._errors_logged = set()
        self._last_prune = None
        self.status = {"version": VERSION, "last_poll": None, "last_success": None,
                       "last_error": None, "last_error_time": None, "seerr_reachable": None,
                       "pending": None, "last_result": None}

    # titles
    def title_for(self, req):
        media = req.get("media") or {}
        mtype = request_type(req)
        tmdb = media.get("tmdbId")
        if not mtype or not tmdb:
            return f"Request {req.get('id')}"
        key = (mtype, tmdb)
        if key not in self._titles:
            try:
                self._titles[key] = self.client.title(mtype, tmdb) or f"TMDB {tmdb}"
            except SeerrError as e:
                log.warning("Could not fetch title for %s %s: %s", mtype, tmdb, e)
                return f"TMDB {tmdb}"  # not cached, try again next time
        return self._titles[key]

    # counting
    def count_used(self, history, media, is4k, start, settings, manual_ids, exclude_id=None):
        n = 0
        for r in history:
            if r.get("id") == exclude_id or request_type(r) != media:
                continue
            if r.get("status") not in COUNTED_STATUSES:
                continue
            created = parse_time(r.get("createdAt"))
            if created is None or created < start:
                continue
            if settings["separate_4k"] and bool(r.get("is4k")) != bool(is4k):
                continue
            if not settings["count_manual_approvals"] and r.get("id") in manual_ids:
                continue
            n += 1
        return n

    def _log_error_once(self, now, req, reason, user=None):
        key = (req.get("id") if req else None, reason)
        if key in self._errors_logged:
            return
        self._errors_logged.add(key)
        if len(self._errors_logged) > 5000:
            self._errors_logged.clear()
        user = user or (req or {}).get("requestedBy") or {}
        self.store.add_log(now, "error", request_id=(req or {}).get("id"), user_id=user.get("id"),
                           user_name=user.get("displayName"),
                           title=self.title_for(req) if req else None,
                           media_type=request_type(req) if req else None,
                           is4k=bool((req or {}).get("is4k")), reason=reason[:500])

    def _set_error(self, now, msg, reachable):
        self.status.update(last_error=msg, last_error_time=iso(now), seerr_reachable=reachable)

    def poll_once(self):
        """One pass over the pending requests. Never raises for Seerr problems."""
        with self.lock:
            now = self.now()
            result = {"ok": True, "approved": 0, "held": 0, "skipped": 0, "errors": 0, "error": None}
            self.status["last_poll"] = iso(now)
            try:
                self._maybe_prune(now)
                self._poll(now, result)
            except SeerrError as e:
                result.update(ok=False, error=str(e))
                self._set_error(now, str(e), reachable=e.status is not None)
                log.warning("Poll failed: %s", e)
            except sqlite3.Error as e:
                result.update(ok=False, error=f"Database error: {e}")
                self._set_error(now, result["error"], reachable=self.status["seerr_reachable"])
                log.error("Database error during poll: %s", e)
            if result["ok"]:
                self.status.update(last_success=iso(now), seerr_reachable=True)
                if result["errors"] == 0:
                    self.status["last_error"] = None
            self.status["last_result"] = {k: result[k] for k in ("approved", "held", "skipped", "errors")}
            return result

    def _maybe_prune(self, now):
        if self._last_prune is None or now - self._last_prune > timedelta(hours=1):
            removed = self.store.prune(now)
            if removed:
                log.info("Pruned %d log entries older than %d days", removed, LOG_KEEP_DAYS)
            self._last_prune = now

    def _poll(self, now, result):
        pending = self.client.pending_requests()
        self.status["pending"] = len(pending)
        if not pending:
            return
        settings = self.store.get_settings()
        overrides = self.store.all_overrides()
        held_ids = self.store.ids_with_action("held")
        manual_ids = self.store.manual_ids() if not settings["count_manual_approvals"] else set()

        for req in pending:
            rid = req.get("id")
            user = req.get("requestedBy") or {}
            uid = user.get("id")
            media = request_type(req)
            if rid is None or uid is None or media is None:
                result["skipped"] += 1
                continue
            if int(user.get("permissions") or 0) & EXEMPT_PERMS:
                result["skipped"] += 1  # admins and request managers approve their own
                continue
            already_held = rid in held_ids
            if already_held and not settings["auto_release_held"]:
                result["skipped"] += 1
                continue

            lim = effective_limit(settings, overrides.get(uid), media)
            is4k = bool(req.get("is4k"))
            label = ("4K " if is4k and settings["separate_4k"] else "") + ("movies" if media == "movie" else "TV")

            try:
                if not lim["enabled"]:
                    decision, reason = "hold", f"{label} limit is off, so every request needs an admin"
                elif lim["unlimited"]:
                    decision, reason = "approve", f"unlimited {label}"
                else:
                    start = window_start(lim, now)
                    history = self.client.user_requests(uid, start)
                    used = self.count_used(history, media, is4k, start, settings, manual_ids, exclude_id=rid)
                    wl = window_label(lim)
                    if used < lim["count"]:
                        decision, reason = "approve", f"{used + 1}/{lim['count']} {label} {wl}"
                    else:
                        decision, reason = "hold", f"limit reached: {used}/{lim['count']} {label} {wl}"

                if decision == "hold":
                    if not already_held:
                        self.store.add_log(now, "held", request_id=rid, user_id=uid,
                                           user_name=user.get("displayName"), title=self.title_for(req),
                                           media_type=media, is4k=is4k, reason=reason)
                        held_ids.add(rid)
                        result["held"] += 1
                        log.info("Held request %s from %s: %s", rid, user.get("displayName"), reason)
                    else:
                        result["skipped"] += 1
                    continue

                try:
                    self.client.approve(rid)
                except SeerrError as e:
                    if e.status == 409:  # no longer pending: an admin got there first
                        result["skipped"] += 1
                        continue
                    raise
                if already_held:
                    reason = "released after a slot freed up: " + reason
                self.store.add_log(now, "auto_approved", request_id=rid, user_id=uid,
                                   user_name=user.get("displayName"), title=self.title_for(req),
                                   media_type=media, is4k=is4k, reason=reason)
                result["approved"] += 1
                log.info("Approved request %s from %s: %s", rid, user.get("displayName"), reason)
            except SeerrError as e:
                if e.status is None:
                    raise  # Seerr went away mid-poll; stop and back off
                result["errors"] += 1
                self._set_error(now, str(e), reachable=True)
                self._log_error_once(now, req, str(e))
                log.warning("Error on request %s: %s", rid, e)

    # overview for the web page
    def users_overview(self):
        now = self.now()
        settings = self.store.get_settings()
        overrides = self.store.all_overrides()
        manual_ids = self.store.manual_ids() if not settings["count_manual_approvals"] else set()
        users = self.client.users()
        buckets = [("movie", False), ("tv", False)]
        if settings["separate_4k"]:
            buckets += [("movie", True), ("tv", True)]
        out = []
        for u in users:
            uid = u.get("id")
            perms = int(u.get("permissions") or 0)
            exempt = bool(perms & EXEMPT_PERMS)
            ov = overrides.get(uid, {})
            row = {"id": uid, "displayName": u.get("displayName") or u.get("username") or f"User {uid}",
                   "avatar": u.get("avatar"), "exempt": exempt,
                   "admin": bool(perms & PERM_ADMIN), "overrides": ov, "usage": {}}
            if not exempt:
                lims = {m: effective_limit(settings, ov, m) for m in MEDIA_TYPES}
                starts = {m: window_start(lims[m], now) for m in MEDIA_TYPES}
                history = None
                for media, is4k in buckets:
                    lim = lims[media]
                    key = media + ("4k" if is4k else "")
                    entry = {"enabled": lim["enabled"], "unlimited": lim["unlimited"],
                             "limit": lim["count"], "mode": lim["mode"],
                             "window_days": lim["window_days"], "window": window_label(lim), "used": None}
                    if history is None:
                        history = self.client.user_requests(uid, min(starts.values()))
                    entry["used"] = self.count_used(history, media, is4k, starts[media], settings, manual_ids)
                    row["usage"][key] = entry
            out.append(row)
        return out


class Poller(threading.Thread):
    MAX_BACKOFF = 300

    def __init__(self, engine, interval):
        super().__init__(name="quotarr-poller", daemon=True)
        self.engine = engine
        self.interval = interval
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.failures = 0

    def trigger(self):
        self.wake.set()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def run(self):
        while not self.stopping.is_set():
            ok = False
            try:
                ok = self.engine.poll_once()["ok"]
            except Exception as e:  # never let the loop die
                log.exception("Unexpected error in poll loop")
                self.engine.status.update(last_error=f"Unexpected error: {e}", last_error_time=iso(utcnow()))
            self.failures = 0 if ok else self.failures + 1
            delay = self.interval if ok else min(self.interval * 2 ** min(self.failures, 6), self.MAX_BACKOFF)
            self.engine.status["next_poll_seconds"] = delay
            self.wake.wait(delay)
            self.wake.clear()


# ---------------------------------------------------------------- web


class Web:
    COOKIE = "quotarr_session"

    def __init__(self, cfg, store, engine, poller=None):
        self.cfg = cfg
        self.store = store
        self.engine = engine
        self.poller = poller
        self.sessions = {}
        self.slock = threading.Lock()
        self.failed = {}  # ip -> deque of failure times
        self.failed_all = deque()
        self.flock = threading.Lock()
        self.max_fail_ip = 5
        self.max_fail_all = 30
        self.fail_window = 300
        try:
            self.index_html = (Path(cfg.static_dir) / "index.html").read_bytes()
        except OSError:
            self.index_html = b"<!doctype html><title>Quotarr</title><p>static/index.html is missing.</p>"

    # sessions
    def new_session(self):
        token = secrets.token_urlsafe(32)
        with self.slock:
            now = time.time()
            for t in [t for t, exp in self.sessions.items() if exp < now]:
                del self.sessions[t]
            self.sessions[token] = now + SESSION_SECONDS
        return token

    def valid_session(self, token):
        if not token:
            return False
        with self.slock:
            exp = self.sessions.get(token)
            if exp is None:
                return False
            if exp < time.time():
                del self.sessions[token]
                return False
            return True

    def end_session(self, token):
        with self.slock:
            self.sessions.pop(token, None)

    # login rate limit
    def _trim(self, dq, now):
        while dq and dq[0] < now - self.fail_window:
            dq.popleft()

    def login_blocked(self, ip):
        now = time.time()
        with self.flock:
            dq = self.failed.get(ip, deque())
            self._trim(dq, now)
            self._trim(self.failed_all, now)
            return len(dq) >= self.max_fail_ip or len(self.failed_all) >= self.max_fail_all

    def record_failure(self, ip):
        now = time.time()
        with self.flock:
            self.failed.setdefault(ip, deque()).append(now)
            self.failed_all.append(now)
            if len(self.failed) > 10000:
                self.failed.clear()

    def clear_failures(self, ip):
        with self.flock:
            self.failed.pop(ip, None)

    def make_server(self, host="0.0.0.0", port=None):
        web = self

        class Handler(QuotarrHandler):
            app = web

        srv = ThreadingHTTPServer((host, self.cfg.port if port is None else port), Handler)
        srv.daemon_threads = True
        return srv


_QUERY_RE = re.compile(r"\?\S*")


class QuotarrHandler(BaseHTTPRequestHandler):
    app: Web = None
    server_version = "Quotarr"
    sys_version = ""

    def log_message(self, fmt, *args):  # strip query strings (webhook token)
        log.debug("%s %s", self.address_string(), _QUERY_RE.sub("", fmt % args))

    def log_request(self, code="-", size="-"):
        log.debug("%s %s %s", self.command, urllib.parse.urlsplit(self.path).path, code)

    # plumbing
    def _send(self, status, body=b"", ctype="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "img-src 'self' data: https:; connect-src 'self'; form-action 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj, headers=None):
        self._send(status, json.dumps(obj).encode(), headers=headers)

    def _token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = cookies.SimpleCookie(raw)
        except cookies.CookieError:
            return None
        m = c.get(Web.COOKIE)
        return m.value if m else None

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise ValidationError("request body too large")
        return self.rfile.read(length) if length > 0 else b""

    def _json_body(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValidationError("Content-Type must be application/json")
        raw = self._body()
        try:
            return json.loads(raw or b"null")
        except ValueError:
            raise ValidationError("body is not valid JSON")

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        try:
            self._route(method)
        except ValidationError as e:
            self._json(400, {"error": str(e)})
        except SeerrError as e:
            self._json(502, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("Unhandled error serving %s %s", method, urllib.parse.urlsplit(self.path).path)
            try:
                self._json(500, {"error": "internal error"})
            except OSError:
                pass

    def _route(self, method):
        app = self.app
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parts.query)

        if path == "/healthz" and method == "GET":
            return self._json(200, {"ok": True, "version": VERSION})

        if path == "/webhook":
            if method != "POST":
                return self._json(405, {"error": "use POST"})
            self._body()  # accept and ignore any payload
            if app.cfg.webhook_token:
                given = (query.get("token") or [""])[0]
                if not hmac.compare_digest(given.encode(), app.cfg.webhook_token.encode()):
                    return self._json(401, {"error": "bad token"})
            if app.poller:
                app.poller.trigger()
            return self._json(202, {"ok": True})

        if not app.cfg.password:
            if path in ("/", "/index.html") or path.startswith("/api"):
                return self._send(503, b"Quotarr web UI is disabled: set QUOTARR_PASSWORD to turn it on.\n",
                                  "text/plain; charset=utf-8")
            return self._json(404, {"error": "not found"})

        if path in ("/", "/index.html") and method == "GET":
            return self._send(200, app.index_html, "text/html; charset=utf-8")

        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})

        if path == "/api/login":
            if method != "POST":
                return self._json(405, {"error": "use POST"})
            ip = self.client_address[0]
            if app.login_blocked(ip):
                return self._json(429, {"error": "Too many failed logins. Try again in a few minutes."},
                                  headers={"Retry-After": str(app.fail_window)})
            body = self._json_body()
            given = body.get("password") if isinstance(body, dict) else None
            if not isinstance(given, str) or not hmac.compare_digest(given.encode(), app.cfg.password.encode()):
                app.record_failure(ip)
                time.sleep(0.25)
                return self._json(401, {"error": "Wrong password."})
            app.clear_failures(ip)
            token = app.new_session()
            secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
            return self._json(200, {"ok": True}, headers={
                "Set-Cookie": f"{Web.COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; "
                              f"Max-Age={SESSION_SECONDS}{secure}"})

        token = self._token()
        if not app.valid_session(token):
            return self._json(401, {"error": "login required"})

        if path == "/api/logout" and method == "POST":
            app.end_session(token)
            return self._json(200, {"ok": True}, headers={
                "Set-Cookie": f"{Web.COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})

        if path == "/api/status" and method == "GET":
            st = dict(app.engine.status)
            st.update(poll_seconds=app.cfg.poll_seconds, seerr_url=app.cfg.seerr_url,
                      webhook_token_set=bool(app.cfg.webhook_token), now=iso(utcnow()))
            return self._json(200, st)

        if path == "/api/poll" and method == "POST":
            if app.poller:
                app.poller.trigger()
                return self._json(202, {"ok": True})
            return self._json(200, app.engine.poll_once())

        if path == "/api/settings":
            if method == "GET":
                return self._json(200, app.store.get_settings())
            if method == "PUT":
                body = self._json_body()
                new = validate_settings_patch(app.store.get_settings(), body)
                app.store.save_settings(new)
                log.info("Default limits updated")
                return self._json(200, new)
            return self._json(405, {"error": "use GET or PUT"})

        if path == "/api/users" and method == "GET":
            return self._json(200, {"users": app.engine.users_overview(),
                                    "settings": app.store.get_settings()})

        m = re.fullmatch(r"/api/users/(\d+)/limits", path)
        if m:
            uid = int(m.group(1))
            if method == "GET":
                return self._json(200, app.store.get_override(uid))
            if method == "PUT":
                override = validate_override(self._json_body())
                if override:
                    app.store.set_override(uid, override, utcnow())
                else:
                    app.store.delete_override(uid)
                log.info("Limits updated for user %s", uid)
                return self._json(200, override)
            if method == "DELETE":
                app.store.delete_override(uid)
                log.info("Limits reset to defaults for user %s", uid)
                return self._json(200, {})
            return self._json(405, {"error": "use GET, PUT or DELETE"})

        if path == "/api/log" and method == "GET":
            try:
                limit = int((query.get("limit") or ["200"])[0])
            except ValueError:
                raise ValidationError("limit must be a number")
            limit = max(1, min(limit, 1000))
            return self._json(200, {"entries": app.store.recent_log(limit)})

        return self._json(404, {"error": "not found"})


# ---------------------------------------------------------------- main


def setup_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)


def main(argv=None):
    setup_logging()
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        log.error("Config error: %s", e)
        return 2
    try:
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        store = Store(str(Path(cfg.data_dir) / "quotarr.db"))
    except (OSError, sqlite3.Error) as e:
        log.error("Cannot open the database in DATA_DIR=%s: %s", cfg.data_dir, e)
        return 2

    client = SeerrClient(cfg.seerr_url, cfg.api_key)
    engine = Engine(store, client)
    poller = Poller(engine, cfg.poll_seconds)
    web = Web(cfg, store, engine, poller)

    log.info("Quotarr %s starting: Seerr at %s, polling every %ss, local time zone %s",
             VERSION, cfg.seerr_url, cfg.poll_seconds, time.strftime("%Z"))
    if not cfg.password:
        log.warning("QUOTARR_PASSWORD is not set, so the web UI is OFF. Approvals still run. "
                    "Set QUOTARR_PASSWORD and restart to use the web page.")
    if cfg.webhook_token is None:
        log.info("WEBHOOK_TOKEN is not set: POST /webhook is open to anyone who can reach this port.")

    try:
        server = web.make_server()
    except OSError as e:
        log.error("Cannot listen on port %s: %s", cfg.port, e)
        return 2

    stop = threading.Event()

    def shutdown(*_):
        if not stop.is_set():
            stop.set()
            log.info("Shutting down")
            poller.stop()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    poller.start()
    log.info("Listening on port %s", cfg.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
