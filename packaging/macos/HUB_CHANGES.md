# Hub changes requested by the macOS app

The macOS app packages `services/hub/app` exactly as it is checked out — nothing
in `services/hub/` was modified for it. Everything below works today through a
workaround in `packaging/macos/`; these are the small hub-side changes that would
make the app (and other clients) simpler or more correct. Ordered by value.

Line numbers refer to the tree as of 2026-10-31 (working tree on top of `93368e0`).

---

## 1. Put the history `id` (and `car_name`, `ts`) on the SSE `alert` event

**File:** `app/alerts/engine.py`, `AlertEngine._deliver` (~line 424)

**What:** `store.add_alert()` already returns the new row id. Keep it and add it
to the event you now emit:

```python
alert_id = None
with contextlib.suppress(Exception):
    alert_id = await store.add_alert(car_id, car_name, rule, level, title, line, delivered,
                                     channels if push else ["app"])
live.emit({"type": "alert", "id": alert_id, "ts": time.time(), "car_id": car_id,
           "car_name": car_name, "rule": rule, "level": level, "title": title,
           "body": line, "pushed": bool(delivered), "in_app_only": not push, "url": url})
```

**Why:** the app shows a native notification once per alert. History rows have
an `id`; the SSE event doesn't, so today the app treats the SSE event only as a
nudge and immediately re-reads `GET /api/alerts/history?limit=20` to find the
row (and its id) before notifying. With the id (and `in_app_only`, so snoozed /
quiet-hours alerts are not pushed) on the event, that extra request goes away.
The app already accepts both shapes, so this can land at any time.

## 2. Ignore typed events in the portal's SSE consumer

**File:** `app/web/static/js/util.js`, `liveConnect()` (`else if (msg.car_id)`)

**What:** `} else if (!msg.type && msg.car_id) {`

**Why:** the new `{"type": "alert", "car_id": …}` events (and any future typed
event that carries `car_id`) are treated as a state change and write
`cars[car_id]["undefined"] = undefined` into the live state on every alert.

## 3. Let "no Grafana" be expressible

**Files:** `app/main.py` `public_config()` (~line 362), `app/web/templates/settings.html` (~line 242/262)

**What:** when `settings.grafana_url` is empty, return `"grafana_url": null` from
`/api/config` and hide `#lnk-grafana` in Settings.

**Why:** the Mac app ships TeslaMate + hub without Grafana (by design). Today
`/api/config` always builds `http://<host>:<GRAFANA_PUBLIC_PORT>`, so Settings
shows an "Open Grafana ↗" button that leads nowhere. The supervisor already
passes `GRAFANA_URL=` (empty) to the hub so this works as soon as the hub honours it.

## 4. (Optional) a font directory for the overlay renderer

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

## 5. (Minor) MQTT reader exception at shutdown

**File:** `app/livestate.py`, `_run()` → `read_messages()` (~line 279)

**What:** when the listener task is cancelled, cancel and await the `reader`
task inside `contextlib.suppress(aiomqtt.MqttError, asyncio.CancelledError)`
(or catch `MqttError` in `read_messages` when the client is closing).

**Why:** every clean stop of the hub logs
`asyncio ERROR Task exception was never retrieved … aiomqtt.exceptions.MqttError:
Disconnected during message iteration`. Harmless, but it shows up in the app's
`hub.log` on every restart and looks like a crash.

## 6. (Minor) Serve `/manifest.json` without auth

**File:** `app/main.py`, `_PUBLIC_PATHS` (~line 141)

**What:** add `"/manifest.json"` to `_PUBLIC_PATHS`.

**Why:** `shell.html` already links it with `crossorigin="use-credentials"`, but
WebKit's manifest loader (WKWebView in the app, and Safari) still requests it
without answering the Basic-auth challenge, so every page load logs
`auth failed for GET /manifest.json` twice. The manifest is static (name, icons,
colours) — nothing in it needs protecting.

## 7. (FYI) Bluetooth from the hub on macOS

`tesla-fleet-api` pulls in `bleak` + `pyobjc-framework-CoreBluetooth`. In the
app the hub is a background `python3.13` started by a LaunchAgent, not an app
bundle with `NSBluetoothAlwaysUsageDescription`, so macOS privacy controls (TCC)
will not let it use Bluetooth. Signed commands over the Fleet API (HTTPS) are
unaffected. If BLE commands from the Mac are a goal, tell us and we will look at
making the app — not the background interpreter — the responsible process.

---

### Already fine — no change needed

* **Bind address:** the supervisor runs `python -m uvicorn app.main:app --host 127.0.0.1 --port …`
  (`HUB_BIND=0.0.0.0` in `config.env` opts into LAN access).
* **Writable data:** `DATA_DIR=~/Library/Application Support/OhmyTesla/hub` (3D models,
  `hub.db`, exports, previews all land there).
* **Postgres over a Unix socket:** `TM_DB_HOST` is the socket directory; asyncpg treats an
  absolute path as a socket directory, so no TCP listener is needed.
* **ffmpeg / ffprobe via PATH:** the supervisor prepends the bundled `ffmpeg/bin`; the
  encoder probe picks `h264_videotoolbox` on Apple Silicon.
* **Signed commands:** done in-process by `tesla-fleet-api`; the app does not bundle Tesla's
  Go `tesla-http-proxy` and never sets `TESLA_COMMAND_PROXY`. `PUBLIC_URL` (needed by the
  Settings › Vehicle commands wizard) is passed through from `config.env`.
