# Hub changes requested by the macOS app

The macOS app packages `services/hub/app` exactly as it is checked out — nothing
in `services/hub/` was modified for it. Everything below works today through a
workaround in `packaging/macos/` (or not at all, where noted); these are the small
hub-side changes that would make the app (and other clients) simpler or lighter.

Line numbers refer to the tree as of 2026-11-01.

---

## Done

### Sentry fast poll reads its setting from memory (0.3.0)

`app/alerts/fastpoll.py` caches `sentry_fast_poll_seconds`: re-read at most once a minute,
and immediately after **Settings** changes it (`poller.invalidate()`). The loop still
wakes every second, so Sentry turning on is noticed at once, but an idle pass no longer
touches SQLite. The note that asked for it follows.

## Open

### 1. Sentry fast poll: don't read SQLite every second (done, above)

**File:** `app/alerts/fastpoll.py`, `_loop()` (~line 122) and `interval()` (~line 82)

**What:** cache the `sentry_fast_poll_seconds` override (refresh it when Settings
writes it, or at most once a minute), and sleep until the next car is due instead of
waking every second when no car is eligible (Sentry off, car asleep).

**Why:** the loop wakes every second and each pass awaits `store.get_setting()` — an
aiosqlite round trip through its worker thread — even when no car has Sentry Mode on.
On an idle Mac the hub is the busiest process of the stack: ~0.11 % CPU and ~24
wakeups/s (macOS counts idle wakeups against a laptop's battery), while TeslaMate,
PostgreSQL and Mosquitto together stay under 0.1 %. The fast poll itself must stay
fast when Sentry is on — this is only about the idle case.

### 2. (Optional) a font directory for the overlay renderer

**Files:** `app/dashcam/telemetry.py` (`font = "Noto Sans CJK SC"`, ~line 174),
`app/dashcam/composer.py` (`ass=filename=…`, ~line 208)

**What:** an optional setting, e.g. `overlay_fonts_dir: str = ""`; when set,
append `:fontsdir=<escaped dir>` to the `ass=` filter.

**Why:** libass must find "Noto Sans CJK SC" *by name*. The app solves it by
building its ffmpeg with libass' fontconfig provider and pointing
`FONTCONFIG_FILE` at a generated `fonts.conf` that lists the bundled Noto Sans
CJK SC fonts (verified: `fontselect: (Noto Sans CJK SC, 700, 0) ->
…/runtime/fonts/NotoSansCJKsc-Bold.otf`). A `fontsdir` would make the overlay
independent of how ffmpeg was built (e.g. Homebrew's CoreText-only libass on a
bare-metal Mac). Not needed for the app.

### 3. (FYI) Bluetooth from the hub on macOS

`tesla-fleet-api` pulls in `bleak` + `pyobjc-framework-CoreBluetooth`. In the
app the hub is a background `python3.13` started by a LaunchAgent, not an app
bundle with `NSBluetoothAlwaysUsageDescription`, so macOS privacy controls (TCC)
will not let it use Bluetooth. Signed commands over the Fleet API (HTTPS) are
unaffected. If BLE commands from the Mac are a goal, tell us and we will look at
making the app — not the background interpreter — the responsible process.
(Memory is not an issue here: none of it is imported until a command is sent.)

---

## Done in the hub (kept for reference)

* **History `id`, `ts`, `car_name`, `in_app_only` on the SSE `alert` event** — the app
  notifies each alert once by its id and no longer pushes snoozed / quiet-hours alerts
  from the event.
* **The portal's SSE consumer ignores typed events** (`util.js`, `else if (msg.type)`).
* **No Grafana is expressible**: `/api/config` returns `"grafana_url": null` when
  `GRAFANA_URL` is empty (the supervisor passes it empty).
* **MQTT reader exception at shutdown** is cancelled and awaited (`livestate.py`).
* **`/manifest.json` is served without auth** (`_PUBLIC_PATHS`).

---

### Already fine — no change needed

* **Bind address:** the supervisor runs `python -m uvicorn app.main:app --host 127.0.0.1 --port …`
  (`HUB_BIND=0.0.0.0` in `config.env` opts into LAN access); one worker process.
* **Writable data:** `DATA_DIR=~/Library/Application Support/OhmyTesla/hub` (3D models,
  `hub.db`, exports, previews all land there).
* **Postgres over a Unix socket:** `TM_DB_HOST` is the socket directory; asyncpg treats an
  absolute path as a socket directory, so no TCP listener is needed.
* **ffmpeg / ffprobe via PATH:** the supervisor prepends the bundled `ffmpeg/bin`; the
  encoder probe picks `h264_videotoolbox` on Apple Silicon.
* **Signed commands:** done in-process by `tesla-fleet-api`; the app does not bundle Tesla's
  Go `tesla-http-proxy` and never sets `TESLA_COMMAND_PROXY`. `PUBLIC_URL` (needed by the
  Settings › Vehicle commands wizard) is passed through from `config.env`.
* **Memory:** no single import dominates the hub's ~73 MB (pydantic, FastAPI,
  cryptography, asyncpg, httpx, Jinja2 share it); nothing to make lazy.
* **The menu-bar panel** reads only existing endpoints: `/api/overview`, `/api/cars`,
  `/api/alerts/history?limit=1`, `/api/alerts/snooze` (GET; POST to pause alerts),
  `/api/health/detail`, `/api/prefs` — and the live stream it already follows.
