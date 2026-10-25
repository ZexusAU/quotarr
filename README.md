# Quotarr

Per-user auto-approval quotas for [Seerr](https://github.com/seerr-team/seerr) (the merged Overseerr/Jellyseerr).

Each user gets a number of auto-approved requests per time window. Anything beyond that isn't refused. It waits in Seerr for an admin to approve or decline as normal.

> **Status: v1.** Tested against a fake Seerr server that copies the real API's response shapes. It has **not yet been tested against a live Seerr instance.** Try it on a test server first, and keep an eye on the activity log for the first few days.

Quotarr is not affiliated with or endorsed by the Seerr project.

## Why Seerr's own quota doesn't do this

Seerr's built-in quota is a hard stop. When a user is over their limit, Seerr refuses the request (HTTP 403, `QuotaRestrictedError`) and the user can't request at all until the window frees up.

Quotarr gives you a soft limit instead:

- under the limit: approved straight away, as if the user had Auto-Approve
- over the limit: the request is still made, but stays **pending** until you approve or decline it in Seerr

## How it works

1. You take Auto-Approve away from normal users, so every request they make lands as pending.
2. Quotarr checks Seerr's pending requests every 30 seconds (oldest first).
3. For each one, it counts that user's recent approved requests. If they're under their limit, Quotarr approves the request through the Seerr API. If not, it leaves it pending and writes "held" in its activity log.

Requests from users with the **Admin** or **Manage Requests** permission are never touched. They approve their own.

## Set up Seerr (2 changes)

1. **Remove Auto-Approve from normal users.** In Seerr go to **Users**, then **Edit** on a user, then **Permissions**, and untick **Auto-Approve** (and Auto-Approve Movies, Auto-Approve Series, and the 4K versions if they're ticked). To change the default for new users, untick it under **Settings → Users → Default Permissions** as well.
2. **Leave Seerr's own quotas unlimited.** Under **Settings → Users**, keep the global Movie and Series request limits on unlimited, and don't set per-user quota overrides in Seerr. Seerr counts pending requests against its own quota, so if it's left on, Seerr will start refusing requests before Quotarr gets a say.

You also need Seerr's API key: **Settings → General → API Key**.

## Install (Docker Compose)

1. Add Quotarr to the `docker-compose.yml` that runs Seerr, under `services:`:

```yaml
  quotarr:
    image: ghcr.io/zexusau/quotarr:latest
    container_name: quotarr
    restart: unless-stopped
    environment:
      SEERR_URL: http://seerr:5055
      SEERR_API_KEY: ${SEERR_API_KEY}
      QUOTARR_PASSWORD: ${QUOTARR_PASSWORD}
      TZ: Australia/Sydney
    volumes:
      - ./quotarr-data:/data
    ports:
      - "8484:8484"
```

   `seerr` in `SEERR_URL` is the name of your Seerr service. Change it if yours is called something else (for example `jellyseerr`). If Seerr is in a different compose file or on its own Docker network, add Quotarr to that network too, or use `http://<server-ip>:5055`.

2. Create a file called `.env` in the **same folder as `docker-compose.yml`** (or add to it if you already have one). Compose reads it automatically and fills in the `${...}` values:

```sh
SEERR_API_KEY=paste-your-seerr-api-key-here
QUOTARR_PASSWORD=pick-a-long-password
```

   This keeps the key and password out of your compose file. Don't commit `.env` to git.

3. Run `docker compose up -d quotarr`, then open `http://your-server:8484` and log in with `QUOTARR_PASSWORD`. Your Seerr users should be listed, with admins marked **exempt**. If it can't reach Seerr, `docker logs quotarr` says why.

`docker-compose.example.yml` also shows how to use a Docker secret for the API key.

### Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `SEERR_URL` | `http://seerr:5055` | Where Quotarr reaches Seerr. |
| `SEERR_API_KEY` | (required) | Seerr's API key. |
| `SEERR_API_KEY_FILE` | | Read the API key from a file instead (Docker secrets). Used only if `SEERR_API_KEY` is empty. |
| `QUOTARR_PASSWORD` | (none) | Password for the web page. Without it the web page is turned off, but approvals still run. |
| `POLL_SECONDS` | `30` | How often to check for pending requests (5 or more). |
| `PORT` | `8484` | Web port. |
| `DATA_DIR` | `/data` | Where `quotarr.db` (SQLite) is kept. |
| `TZ` | container default (UTC) | Time zone. Used for the calendar-week reset (Monday 00:00 local time). |
| `WEBHOOK_TOKEN` | (none) | Optional. If set, `/webhook` needs `?token=` with this value. |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail. |

Quotarr never logs the API key or the password.

## Limits

You set limits on the web page. Movies and TV have separate limits. Each has:

- **Limit**: *Limited*, *Unlimited* (approve everything), or *Off* (hold everything for an admin).
- **Requests per window**: how many get auto-approved.
- **Window**: *Rolling* (the last N days) or *Calendar week* (resets Monday 00:00 in your `TZ`; the days setting is ignored).

These are the defaults for everyone. You can override any part of them for a single user (for example, just the count). Anything you leave on "Default" follows the defaults, including later changes to them.

Users with Admin or Manage Requests show as **exempt**.

Three more options:

| Option | Default | What it means |
| --- | --- | --- |
| Count requests an admin approved by hand | On | If a request was held and you approved it yourself, does it use up one of the user's slots? |
| Count 4K requests separately | Off | Off: 4K and normal requests share one limit. On: each gets the full limit. |
| Auto-approve held requests when a slot frees up | Off | Off: once a request is held, it waits for you, even after the window frees up. On: Quotarr re-checks held requests every poll and approves them when there's room. |

## How counting works

For a pending request, Quotarr counts the same user's requests that:

- are the same type (movie or TV),
- have status **Approved** or **Completed** (Completed means it's now available),
- were **created** inside the window,
- aren't the request being decided.

It doesn't count:

- **Declined** requests.
- **Failed** requests. Seerr marks a request Failed when it was approved but sending it to Radarr or Sonarr failed. That's a server problem, not the user's doing. If you press Retry in Seerr, it goes back to Approved and counts again.
- **Pending** requests, including ones Quotarr is holding.

Pending requests are handled oldest first, one at a time, and the count is redone after every approval. So if a user has one slot left and makes two requests, only the older one is approved.

The window is based on when the request was **made** (`createdAt`), not when it was approved.

How Quotarr knows a request was approved by hand: if Quotarr logged it as held and never approved it itself, but it's now approved, an admin approved it. This uses Quotarr's own log, so it only knows about requests Quotarr has seen. The log keeps 90 days, which is why a rolling window can be at most 90 days.

## Optional: instant approvals with a webhook

Polling every 30 seconds is fine for most people. If you want approvals to happen within a second or two:

1. Set `WEBHOOK_TOKEN` to a random string.
2. In Seerr go to **Settings → Notifications → Webhook**, turn it on, and set the URL to `http://quotarr:8484/webhook?token=<your token>`.
3. Tick the **Request Pending Approval** notification type and save.

Quotarr ignores the body. Any POST to `/webhook` just triggers a check straight away.

## FAQ

**Does a TV request with five seasons count as five?**
No. One request is one, however many seasons it asks for. Seerr's own quota counts seasons; Quotarr doesn't.

**What about 4K?**
By default 4K and normal requests share the same limit. Turn on "Count 4K requests separately" to give each its own.

**What happens when Quotarr restarts?**
Settings, user limits and the log are in `/data/quotarr.db`, so nothing is lost. Requests made while it was down are still pending in Seerr and are handled on the first check after it starts. Login sessions are kept in memory, so you'll need to log in again.

**What if Seerr is down?**
Quotarr logs the error, shows it on the status bar, waits a bit longer each time (up to 5 minutes), and keeps trying. It doesn't crash.

**What if I approve or decline a request myself before Quotarr gets to it?**
Quotarr skips it.

**Can users see Quotarr?**
No. It's an admin page only. Users just see their request as pending in Seerr.

**Does it work with Overseerr or Jellyseerr?**
It uses the same API those projects had, so it probably does, but it has only been written against Seerr's source.

**Security notes**
The page sets an HttpOnly, SameSite=Strict session cookie and limits failed logins. If you put Quotarr behind a reverse proxy with HTTPS, the cookie is also marked Secure (it reads `X-Forwarded-Proto`). Don't expose port 8484 to the internet without a proxy in front.

## Development

Python 3.12, standard library only. No packages to install.

```sh
python -m unittest -v          # unit and web tests, uses tests/fake_seerr.py
python tests/e2e_check.py      # starts the fake Seerr and quotarr.py as processes and uses the web API
python tests/fake_seerr.py     # a fake Seerr with demo data on port 15055 (API key: demo-key)
```

## Licence

MIT. See `LICENSE`.
