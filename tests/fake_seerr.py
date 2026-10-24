"""A small fake Seerr server for tests (standard library only).

It mimics the parts of the Seerr API that Quotarr uses, with the same
response shapes as seerr-team/seerr (develop): server/routes/request.ts,
server/routes/user/index.ts, server/entity/MediaRequest.ts.

Run on its own with demo data:  python tests/fake_seerr.py --port 15055 --api-key demo-key
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PENDING, APPROVED, DECLINED, FAILED, COMPLETED = 1, 2, 3, 4, 5
PERM_ADMIN, PERM_MANAGE_REQUESTS, PERM_REQUEST = 2, 16, 32


def iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class FakeSeerr:
    def __init__(self, api_key="test-api-key"):
        self.api_key = api_key
        self.lock = threading.Lock()
        self.users = {}
        self.requests = {}
        self.titles = {}
        self.approve_calls = []
        self.calls = []
        self.fail_status = None  # set to e.g. 500 to make every API call fail
        self.title_fail = False
        self._next_req = 1
        self._next_media = 1
        self.server = None
        self.thread = None

    # data setup
    def add_user(self, uid, name, permissions=PERM_REQUEST):
        now = iso_z(datetime.now(timezone.utc))
        self.users[uid] = {
            "permissions": permissions, "id": uid, "email": f"user{uid}@example.test",
            "plexUsername": None, "jellyfinUsername": name.lower(), "username": None,
            "recoveryLinkExpirationDate": None, "userType": 3, "plexId": None,
            "jellyfinUserId": f"fake{uid}", "avatar": "/avatarproxy/fake", "avatarETag": None,
            "avatarVersion": None, "movieQuotaLimit": None, "movieQuotaDays": None,
            "tvQuotaLimit": None, "tvQuotaDays": None, "createdAt": now, "updatedAt": now,
            "requestCount": 0, "displayName": name,
        }
        return self.users[uid]

    def add_request(self, user_id, media_type="movie", status=PENDING, created=None, is4k=False,
                    tmdb_id=None, title=None, seasons=(1,)):
        with self.lock:
            rid = self._next_req
            self._next_req += 1
            mid = self._next_media
            self._next_media += 1
        created = created or datetime.now(timezone.utc)
        tmdb_id = tmdb_id or 1000 + rid
        if title:
            self.titles[(media_type, tmdb_id)] = title
        user = self.users[user_id]
        ts = iso_z(created)
        req = {
            "id": rid, "status": status, "createdAt": ts, "updatedAt": ts, "type": media_type,
            "is4k": is4k, "serverId": 0, "profileId": 1, "rootFolder": "/media",
            "languageProfileId": None, "tags": [], "isAutoRequest": False, "ignoreQuota": False,
            "media": {
                "downloadStatus": [], "downloadStatus4k": [], "id": mid, "mediaType": media_type,
                "tmdbId": tmdb_id, "tvdbId": 70000 + rid if media_type == "tv" else None,
                "imdbId": None, "status": 2, "status4k": 1, "createdAt": ts, "updatedAt": ts,
                "lastSeasonChange": ts, "mediaAddedAt": None, "serviceId": None, "serviceId4k": None,
                "externalServiceId": None, "externalServiceId4k": None, "externalServiceSlug": None,
                "externalServiceSlug4k": None, "ratingKey": None, "ratingKey4k": None,
                "jellyfinMediaId": None, "jellyfinMediaId4k": None,
            },
            "seasons": ([{"id": rid * 100 + n, "seasonNumber": n, "status": status,
                          "createdAt": ts, "updatedAt": ts} for n in seasons] if media_type == "tv" else []),
            "modifiedBy": None,
            "requestedBy": user,
            "seasonCount": len(seasons) if media_type == "tv" else 0,
        }
        user["requestCount"] += 1
        self.requests[rid] = req
        return rid

    def set_status(self, rid, status, by_admin=True):
        r = self.requests[rid]
        r["status"] = status
        r["updatedAt"] = iso_z(datetime.now(timezone.utc))
        if by_admin:
            r["modifiedBy"] = self.users.get(1)

    def status_of(self, rid):
        return self.requests[rid]["status"]

    # server
    def start(self, port=0, host="127.0.0.1"):
        fake = self

        class H(FakeHandler):
            app = fake

        self.server = ThreadingHTTPServer((host, port), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return f"http://{host}:{self.server.server_address[1]}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def page(items, take, skip):
    total = len(items)
    return {"pageInfo": {"pages": -(-total // take) if take else 0, "pageSize": take,
                         "results": total, "page": (-(-skip // take) if take else 0) + 1},
            "results": items[skip:skip + take]}


class FakeHandler(BaseHTTPRequestHandler):
    app: FakeSeerr = None

    def log_message(self, *args):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        app = self.app
        parts = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        path = parts.path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        app.calls.append((method, path, q))

        if self.headers.get("X-Api-Key") != app.api_key:
            return self._json(403, {"message": "You do not have permission to access this endpoint."})
        if app.fail_status:
            return self._json(app.fail_status, {"message": "Simulated failure"})

        take = int(q.get("take", 10))
        skip = int(q.get("skip", 0))

        with app.lock:
            if method == "GET" and path == "/api/v1/status":
                return self._json(200, {"version": "fake", "commitTag": "local"})

            if method == "GET" and path == "/api/v1/request":
                flt = q.get("filter")
                statuses = {"pending": [PENDING], "approved": [APPROVED], "processing": [APPROVED],
                            "failed": [FAILED], "completed": [COMPLETED]}.get(flt, [1, 2, 3, 4, 5])
                items = [r for r in app.requests.values() if r["status"] in statuses]
                key = (lambda r: r["createdAt"]) if q.get("sort") == "added" else (lambda r: r["id"])
                items.sort(key=lambda r: (key(r), r["id"]), reverse=q.get("sortDirection") != "asc")
                data = page(items, take, skip)
                data["serviceErrors"] = {"radarr": [], "sonarr": []}
                return self._json(200, data)

            if method == "GET" and path == "/api/v1/user":
                items = sorted(app.users.values(), key=lambda u: u["id"])
                return self._json(200, page(items, take, skip))

            parts_ = path.strip("/").split("/")
            # /api/v1/user/{id}/requests
            if method == "GET" and len(parts_) == 5 and parts_[2] == "user" and parts_[4] == "requests":
                uid = int(parts_[3])
                if uid not in app.users:
                    return self._json(404, {"message": "User not found."})
                items = sorted((r for r in app.requests.values() if r["requestedBy"]["id"] == uid),
                               key=lambda r: r["id"], reverse=True)
                return self._json(200, page(items, take, skip))

            # /api/v1/request/{id}/{approve|decline}
            if method == "POST" and len(parts_) == 5 and parts_[2] == "request":
                rid = int(parts_[3])
                action = parts_[4]
                r = app.requests.get(rid)
                if r is None:
                    return self._json(404, {"message": "Request not found."})
                if action not in ("approve", "decline"):
                    return self._json(400, {"message": "Status must be approve or decline."})
                if r["status"] != PENDING:
                    return self._json(409, {"message": "Only pending requests can be approved or declined."})
                r["status"] = APPROVED if action == "approve" else DECLINED
                r["modifiedBy"] = app.users.get(1)
                if action == "approve":
                    app.approve_calls.append(rid)
                return self._json(200, r)

            # /api/v1/movie/{tmdbId}  /api/v1/tv/{tmdbId}
            if method == "GET" and len(parts_) == 4 and parts_[2] in ("movie", "tv"):
                if app.title_fail:
                    return self._json(500, {"message": "TMDB unavailable"})
                tmdb = int(parts_[3])
                title = app.titles.get((parts_[2], tmdb), f"Fake {parts_[2]} {tmdb}")
                key = "title" if parts_[2] == "movie" else "name"
                return self._json(200, {"id": tmdb, key: title})

        return self._json(404, {"message": "Not found"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def seed_demo(fake):
    now = datetime.now(timezone.utc)
    fake.add_user(1, "Admin", PERM_ADMIN)
    fake.add_user(2, "Priya", PERM_REQUEST)
    fake.add_user(3, "Sam", PERM_REQUEST)
    fake.add_user(4, "Jordan", PERM_MANAGE_REQUESTS | PERM_REQUEST)
    fake.add_request(2, "movie", APPROVED, now - timedelta(days=2), title="The Ironbark Saw")
    fake.add_request(2, "movie", COMPLETED, now - timedelta(days=3), title="Dovetail Nights")
    fake.add_request(2, "movie", PENDING, now - timedelta(hours=2), title="Sawdust and Silence")
    fake.add_request(2, "movie", PENDING, now - timedelta(hours=1), title="Grain of Truth")
    fake.add_request(3, "tv", PENDING, now - timedelta(minutes=30), title="Workshop Tales", seasons=(1, 2))
    fake.add_request(3, "movie", DECLINED, now - timedelta(days=1), title="Splinter")
    fake.add_request(4, "movie", PENDING, now - timedelta(minutes=10), title="Manager's Pick")


def main():
    ap = argparse.ArgumentParser(description="Fake Seerr server for Quotarr tests")
    ap.add_argument("--port", type=int, default=15055)
    ap.add_argument("--api-key", default="demo-key")
    args = ap.parse_args()
    fake = FakeSeerr(args.api_key)
    seed_demo(fake)
    print(f"Fake Seerr on {fake.start(args.port)}", flush=True)
    try:
        fake.thread.join()
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
