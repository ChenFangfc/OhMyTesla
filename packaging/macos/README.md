# OhmyTesla for macOS

A native Mac app (Apple Silicon, macOS 13+) that runs the whole OhmyTesla stack
— PostgreSQL, Mosquitto, TeslaMate and the OhmyTesla hub — **without Docker or a
terminal**, or connects to an OhmyTesla server you already run elsewhere.

* a menu-bar panel with the car at a glance (battery, range, charging, doors, Sentry Mode,
  climate, where it is, how fresh the data is, the latest alert) and a few quick settings,
  and a main window that shows the hub (it answers the portal's password prompt for you);
* Simplified Chinese and English throughout, following the hub's language setting;
* light at idle — see [Resource use](#resource-use); connecting to a server you already
  run costs this Mac next to nothing;
* a per-user background service (LaunchAgent) keeps logging and Sentry alerts
  going with the window closed, and starts at login;
* native notifications for critical / warning alerts (Sentry and other major events);
* optional "Keep Mac awake during Sentry".

---

## Build

```bash
brew install elixir cmake meson ninja pkgconf node    # build-time only (Xcode required)
packaging/macos/build.sh
```

Output: `packaging/macos/dist/OhmyTesla-<version>-arm64.dmg` (plus a `.sha256`) — about
100 MB (the installed app is about 300 MB). Scratch work goes to `packaging/macos/build/`
(git-ignored); components are cached, so a re-run after a hub change takes a few minutes
(a cold build, including Erlang/OTP, FFmpeg and PostgreSQL from source, about 30).

Then check the result end to end — `scripts/smoke_test.sh` mounts the DMG, verifies
signatures, linkage and minimum macOS of every Mach-O, starts the whole stack from the
app with throwaway data, probes every service and shuts it down again.

| Option | Effect |
|---|---|
| `--relock` | re-resolve `services/hub/requirements.txt` into `python-requirements.lock` (run after the hub's dependencies change; the build stops and tells you when it is needed) |
| `--no-dmg` | stop after `build/OhmyTesla.app` |
| `--clean` | start from scratch, downloads included |
| `OHMYTESLA_VERSION=0.3.0 ./build.sh` | version shown in Finder and in the DMG name |

What the build does (each step is a script in `scripts/`, logs in `build/logs/`):

1. **PostgreSQL 18.6** — built from source for macOS 13 with contrib (`cube` +
   `earthdistance` are checked), no ICU/OpenSSL/LLVM, and the system libedit for `psql`.
   (The prebuilt theseus-rs archive was evaluated first: it has the extensions, but its
   binaries require macOS 15.)
2. **Python** — python-build-standalone CPython; the minor version follows
   `services/hub/Dockerfile` (`FROM python:3.13-slim` → 3.13). The hub's dependencies are
   installed from `python-requirements.lock` with `pip --require-hashes`.
3. **Mosquitto 2.0.22** — built from source with CMake: no TLS, cJSON, websockets, docs,
   clients or plugins. (2.1.x makes cJSON mandatory for the broker.)
4. **ffmpeg 8.1.3 + ffprobe** — static, built from source with libx264, libass (fontconfig
   provider), freetype, fribidi, harfbuzz and VideoToolbox. Licence: GPL-2.0-or-later.
5. **Erlang/OTP 29.1.2** — built from source for macOS 13, with OpenSSL 3.5.9 (LTS) linked
   statically into the crypto NIF. Homebrew's own erlang bottle is built for the build
   Mac's macOS (minos 26 on macOS 26) and links a Homebrew OpenSSL dylib.
6. **TeslaMate v4.4.0** — cloned at the pinned commit and built exactly like the builder
   stage of its Dockerfile (`mix deps.get --only prod`, `npm ci`, `mix assets.deploy`,
   `mix release`) on that OTP with Homebrew's Elixir 1.20; the release includes ERTS.
7. **Stage** — `services/hub/app` is copied from the working tree *at build time*
   (tracked and untracked files), next to the supervisor, the Noto Sans CJK SC fonts,
   licences and `VERSIONS.json`.
8. **App** — SwiftPM build of `App/`, bundle assembly, ad-hoc signing of every Mach-O and
   of the bundle, then `scripts/relocate.py verify`: fails the build if any Mach-O links
   anything outside the bundle or `/usr/lib` / `/System` (absolute paths, `LC_RPATH`s,
   unresolvable `@loader_path`/`@rpath`), has an invalid signature, or declares a minimum
   macOS newer than 13.0.
9. **DMG** — app + `Applications` link + "首次启动 First launch.txt" (Chinese and English),
   compressed (ULMO).

**Pinning.** Every download is pinned in `versions.sh` by version and SHA-256 (git clones
by commit; a moved tag fails the build). Transitive inputs are verified by their own lock
files: hex packages by TeslaMate's `mix.lock`, npm by `package-lock.json`, Python wheels by
`python-requirements.lock`. The ffmpeg, Mosquitto and FreeType
tarballs' GPG signatures were checked once when the pins were taken (see `versions.sh`).
Downloads live one-per-directory under `build/downloads/`.

---

## What's inside

`OhmyTesla.app/Contents/`

```
MacOS/OhmyTesla                          Swift menu-bar app (also the LaunchAgent's program)
Resources/en.lproj  zh-Hans.lproj        macOS prompts and the About panel in both languages
Library/LaunchAgents/io.github.ohmytesla.supervisor.plist   registered via SMAppService.agent
Resources/runtime/
  supervisor/supervisor.py               starts and watches everything (bundled Python)
  python/            hub/app/            CPython 3.13 + hub dependencies; the hub's code
  postgresql/  mosquitto/  teslamate/  ffmpeg/  fonts/
  etc/openssl.cnf                        keeps the bundled OpenSSL copies off any other openssl.cnf
  licenses/          VERSIONS.json
```

| Component | Version | Licence |
|---|---|---|
| OhmyTesla app, supervisor, hub | this repo | AGPL-3.0 |
| Phosphor Icons (the hub's interface icons) | @phosphor-icons/core 2.1.1 | MIT (`hub/app/web/static/vendor/phosphor/LICENSE`) |
| TeslaMate | v4.4.0 | AGPL-3.0-or-later (`licenses/teslamate`) |
| Erlang/OTP runtime (ERTS) | 29.1.2, built from source | Apache-2.0 |
| Elixir (standard library in the release) | 1.20 (Homebrew) | Apache-2.0 |
| OpenSSL (static, in OTP's crypto NIF) | 3.5.9 LTS | Apache-2.0 |
| PostgreSQL | 18.6, built from source | PostgreSQL Licence |
| Mosquitto | 2.0.22 | EPL-2.0 / EDL-1.0 |
| CPython (python-build-standalone) | 3.13.18 | PSF-2.0 (+ bundled libs) |
| Python packages (fastapi, uvicorn, asyncpg, cryptography, tesla-fleet-api, …) | see `python-requirements.lock` | MIT / BSD / Apache-2.0 / PSF / MPL-2.0 — each package's licence files in `licenses/python/site-packages` |
| FFmpeg (ffmpeg, ffprobe) | 8.1.3, `--enable-gpl` | GPL-2.0-or-later |
| x264 | stable `b35605a` | GPL-2.0-or-later |
| libass / fribidi / harfbuzz / freetype / fontconfig | 0.17.5 / 1.0.17 / 14.6.0 / 2.14.3 / 2.17.1 | ISC / LGPL-2.1+ / MIT / FTL (or GPL-2) / MIT-style |
| Noto Sans CJK SC (Regular, Bold) | Sans2.004 | SIL OFL-1.1 |

The GPL components (ffmpeg, which includes x264) run as separate programs; the corresponding
sources are the pinned upstream releases listed in `versions.sh`.

---

## First launch

1. Open the DMG and drag **OhmyTesla** to **Applications**.
2. The app is ad-hoc signed (no Developer ID), so Gatekeeper blocks the first launch once:
   * macOS 13–14: Control-click (right-click) the app › **Open** › **Open**.
   * macOS 15+: open it, dismiss the warning, then System Settings › Privacy & Security ›
     **Open Anyway**.
   * Or, any version: `xattr -dr com.apple.quarantine /Applications/OhmyTesla.app`
3. Choose:
   * **Connect to a server** — if you already run OhmyTesla (Docker on a NAS or a home
     server), this is the lightest choice: enter its URL and portal password (stored in the
     login keychain) and the app is just a native window, the menu-bar panel and
     notifications.
   * **Run on this Mac.** The app registers its LaunchAgent (macOS shows a "Background
     Items Added" notice; if it asks for approval, allow OhmyTesla under System Settings ›
     General › Login Items). The supervisor then, on first run:
     generates secrets into `config.env` (0600) — `ENCRYPTION_KEY`, the database password,
     the portal password `OHMYTESLA_PASSWORD`, Phoenix's `SECRET_KEY_BASE`/`SIGNING_SALT`;
     runs `initdb` (UTF-8, builtin `C.UTF-8` collation, SCRAM auth); creates the
     `teslamate` database with `cube` + `earthdistance`; and starts, in order,
     **PostgreSQL → Mosquitto → TeslaMate** (which runs its migrations first, like the
     container entrypoint) **→ hub**. A first start took ~10 s on an M-series Mac. The window then
     shows the hub's welcome page, where you sign in with your Tesla account.
4. The app asks once for permission to show notifications.

If the LaunchAgent cannot be registered (or the app runs translocated from the DMG), the
app runs the supervisor as its own child process instead and says so: monitoring then
stops when you quit the app. **More › Install Background Service** in the menu-bar panel retries.

To switch between the two later, use **OhmyTesla › Connect to a Server Instead…** (or
**Change Server…**) in the menu bar while the window is open, **More** in the menu-bar panel,
the same button on the startup screen, or — while the hub runs on this Mac — the line under
the setup guide's title. Leaving **Run on this Mac** stops its services and removes its
background item; its data stays, so choosing it again carries on where it left off.

### The menu-bar panel

Click the icon in the menu bar:

* **The car** — its name (a picker when the account has more than one car), what it is
  doing and where, cabin temperature, battery and range, charging power and time to the
  limit, doors, Sentry Mode and climate, and how fresh the data is: *Live* while the car is
  awake and reporting, otherwise *Updated 5 min ago* — or *Data stopped …* when the hub's
  watchdog says so. A state the car has not reported is left out, never guessed.
* **Latest alert** — time and one line; click it to open the page in the window.
* **Pause alerts** for 1, 8 or 24 hours (the hub's own snooze: non-critical pushes stop
  everywhere; intrusion alerts still come through), **Mac notifications**, **Keep Mac
  awake during Sentry** (Run on this Mac only), **Open at login**.
* **Services** (Run on this Mac) — one line; click it for the four services, restart, stop
  and logs. It opens by itself when something needs attention.
* **Open OhmyTesla**, **More** (copy the portal password, open TeslaMate / logs / data /
  `config.env`, change server or setup, stop services and quit) and **Quit**.

It reads the hub's existing endpoints when it opens and stays current while open (the
live event stream, and every 30 s); nothing is fetched while it is closed. No vehicle
commands. Colour means *live*: the accent colour marks charging, Sentry Mode watching and
climate running; orange marks an unlocked parked car and problems.

### Language

Everything the app says — panel, window, menus, onboarding, notifications, dialogs —
exists in Simplified Chinese and English, with the hub's own vocabulary. The app follows
the hub's language setting (`GET /api/prefs` → `locale`), remembers it for the next
launch, and uses the Mac's preferred language until a hub has answered. macOS's own
prompts and the About panel follow the Mac's language.

### Ports and network exposure

Everything listens on **127.0.0.1 only**; a busy port moves to the next free one and the
choice is written to `state.json` (the app reads it).

| Service | Default | Notes |
|---|---|---|
| Hub (portal) | 47800 | HTTP Basic auth, user `ohmytesla` |
| TeslaMate | 47801 | no auth (as upstream) — loopback only; `CHECK_ORIGIN` restricted |
| MQTT | 47883 | anonymous, loopback only (as the official image's no-auth config) |
| PostgreSQL | Unix socket only (`pgdata/.s.PGSQL.47432`) | SCRAM password; `POSTGRES_TCP=1` adds 127.0.0.1 |

No Erlang distribution (`RELEASE_DISTRIBUTION=none`): no `epmd`, no extra port.

### Settings (`config.env`)

`~/Library/Application Support/OhmyTesla/config.env` holds the generated secrets and any
hub setting from `docker-compose.yml`/`.env.example` (`SMTP_*`, `TELEGRAM_*`, `NTFY_*`,
`APPRISE_URLS`, `TESLA_API_HOST`, `TESLACAM_ROOT=/Volumes/TESLADRIVE`, …). Menu ›
**Edit Settings (config.env)…**, then **Restart Services**. Supervisor options:

| Key | Meaning |
|---|---|
| `OHMYTESLA_PORT`, `TESLAMATE_PORT`, `MQTT_PORT`, `POSTGRES_PORT` | preferred ports |
| `HUB_BIND=0.0.0.0` | let phones on your LAN open the portal (password-protected) |
| `POSTGRES_TCP=1` | also listen on 127.0.0.1 (for e.g. a Grafana you run yourself) |
| `TESLAMATE_<VAR>` | passes `<VAR>` to TeslaMate (`TESLAMATE_TESLA_API_HOST`, `TESLAMATE_MQTT_NAMESPACE`, …) |
| `TESLAMATE_DATABASE_POOL_SIZE`, `TESLAMATE_ERL_FLAGS`, `TESLAMATE_RELEASE_MODE` | override the light defaults (pool of 5; `+S 2:2 +SDcpu 2:2 +SDio 2 +sbwt none +sbwtdcpu none +sbwtdio none +P 65536`; `interactive`) |
| `POSTGRES_SHARED_BUFFERS` | PostgreSQL's buffer cache (default `64MB`) |
| `TZ` | time zone for TeslaMate and the hub (default: the Mac's) |

Never change `ENCRYPTION_KEY` after signing in: it encrypts the Tesla tokens in the database.

**Vehicle commands** are set up in the hub (Settings › Vehicle commands; signing happens
in-process): the wizard needs `PUBLIC_URL` in `config.env` to be a public **https** domain that
serves `/.well-known/appspecific/com.tesla.3p.public-key.pem` from this hub (e.g. a tunnel to
`http://127.0.0.1:47800`).

### Keeping the Mac awake

Monitoring needs the Mac awake. With **Keep Mac awake during Sentry** (menu-bar panel), the
supervisor checks the hub every 30 s and, while any car reports `sentry_mode=true`, holds
an idle-sleep power assertion (`caffeinate -i`, i.e. `PreventUserIdleSystemSleep`). The
display may still sleep; closing a MacBook's lid without an external display still sleeps.

---

## Resource use

Idle, measured the way `scripts/smoke_test.sh` runs the stack (fresh install, throwaway
data, no car signed in) on an M2 Max with macOS 26: CPU is percent of *one* core over
3 minutes after a 2-minute settle, including short-lived children (autovacuum workers,
`pg_isready`); memory is `phys_footprint` (Activity Monitor's "Memory"), RSS is resident size.
Before = v0.2.1, after = this version.

| Run on this Mac (services) | Before: CPU · memory · RSS | After: CPU · memory · RSS |
|---|---|---|
| TeslaMate (Erlang VM; 40 → 12 threads) | 0.10 % · 246 MB · 258 MB | 0.05 % · 126 MB · 139 MB |
| PostgreSQL 18 (20 → 11 processes) | 0.06 % · 117 MB · 176 MB | 0.02 % · 64 MB · 98 MB |
| OhmyTesla hub (Python) | 0.12 % · 72 MB · 95 MB | 0.11 % · 71 MB · 97 MB |
| Supervisor (Python) | 0.12 % · 30 MB · 44 MB | 0.03 % · 21 MB · 37 MB |
| Mosquitto | 0.02 % · 9 MB · 10 MB | 0.02 % · 9 MB · 10 MB |
| **Total** (27 → 18 processes; 50 → 44 wakeups/s) | **0.42 % · 474 MB · 583 MB** | **0.23 % · 291 MB · 381 MB** |

RSS counts PostgreSQL's shared buffers once per process that touched them, so its sum
overstates; "memory" is what Activity Monitor shows.

What changed: TeslaMate runs two schedulers that sleep instead of spinning when idle
(`+S 2:2 +SDcpu 2:2 +SDio 2 +sbwt none …`), a smaller process table (`+P 65536`), code
loaded on first use (`RELEASE_MODE=interactive`, the biggest single saving) and a pool of
5 database connections; PostgreSQL uses synchronous I/O (no I/O workers), no logical
replication launcher, a 64 MB buffer cache, 30 connections and autovacuum every 5 min;
the supervisor checks health every 30 s (TeslaMate via its own `/health_check` rather
than rendering its home page), sleeps 5 s once everything is up and is woken by signals.
Alerts are as fast as before: an MQTT message through the hub's alert engine to the alert
on its live stream (the path a Sentry alert takes after the hub has decided) took a median
4.1 ms before and 3.4 ms after (12 samples each, max 6.3 ms both). A real install with years of data uses more
(PostgreSQL's cache fills, TeslaMate holds the car's state) — roughly 300–400 MB.

The app itself, connected to a hub (its home page includes the 3D car):

| The app (+ the WebKit processes it owns) | Before: CPU · memory | After: CPU · memory |
|---|---|---|
| Main window open | 0.74 % · 484 MB | 0.28 % · 432 MB |
| Window closed, 2–3 min later (menu bar only) | 0.36 % · 425 MB, 9 wakeups/s — the page kept running | 0.11 % · 40 MB, 0.7 wakeups/s — web view released |
| Menu-bar panel open | — | ≈ 0 % · 43 MB (refreshes every 30 s) |

Closing the window now releases the web view and with it WebKit's content and GPU
processes; reopening loads the page that was showing. The app runs no timers while idle:
it follows the hub's live event stream (a keepalive every 25 s) and, on this Mac, the
supervisor's `state.json` (rewritten on change and every 30 s). The panel fetches only while open.

---

## Where things live

| What | Where |
|---|---|
| Database, secrets, hub data, state | `~/Library/Application Support/OhmyTesla/` (`pgdata/`, `config.env`, `hub/`, `teslacam/`, `teslamate/`, `state.json`, `control.json`) |
| Logs (rotated, 10 MB × 5 per service) | `~/Library/Logs/OhmyTesla/` — `supervisor.log`, `postgres.log`, `mosquitto.log`, `teslamate.log`, `hub.log` |
| Caches (Python byte-code, fontconfig) | `~/Library/Caches/OhmyTesla/` |
| App preferences | `defaults read io.github.ohmytesla.app` |
| Remote-server password | login keychain, service `io.github.ohmytesla.app.remote` |

Back up `~/Library/Application Support/OhmyTesla` (with the app's services stopped) to keep
your history; `config.env` is needed to read the Tesla tokens in it.

## Uninstall

1. Menu-bar panel › **More › Stop Services and Quit** (or **Quit** after stopping the services).
2. Remove the background service and login item:
   `/Applications/OhmyTesla.app/Contents/MacOS/OhmyTesla --unregister-agent`
   (or System Settings › General › Login Items: remove OhmyTesla).
3. Delete `/Applications/OhmyTesla.app`.
4. Delete your data (irreversible): `rm -rf ~/Library/Application\ Support/OhmyTesla ~/Library/Logs/OhmyTesla ~/Library/Caches/OhmyTesla`
5. Optional: `defaults delete io.github.ohmytesla.app` and remove the keychain item
   `io.github.ohmytesla.app.remote` in Keychain Access.

---

## Development notes

* Run the supervisor without the app (isolated data, nothing touches your real install):
  ```bash
  R=packaging/macos/build/OhmyTesla.app/Contents/Resources/runtime
  OHMYTESLA_HOME=/tmp/omt OHMYTESLA_LOGS=/tmp/omt/logs OHMYTESLA_CACHES=/tmp/omt/cache \
    $R/python/bin/python3 -I -B $R/supervisor/supervisor.py
  ```
  `kill -TERM` it for a clean shutdown (`pg_ctl stop -m fast` for the database).
* `OHMYTESLA_NO_AGENT=1 OHMYTESLA_HOME=/tmp/omt build/OhmyTesla.app/Contents/MacOS/OhmyTesla`
  runs the GUI with a child supervisor and isolated data, without touching launchd.
* `OhmyTesla --agent-status | --register-agent | --unregister-agent` manage the LaunchAgent.
* Re-check relocatability any time: `/usr/bin/python3 scripts/relocate.py verify build/OhmyTesla.app/Contents --forbid /opt/homebrew/`
* Hub-side requests from this packaging work: [`HUB_CHANGES.md`](HUB_CHANGES.md).

## Known limitations

* Apple Silicon + macOS 13 or later only. Not notarized (no Developer ID) — see First launch.
* Monitoring stops while the Mac sleeps; see "Keeping the Mac awake".
* No Grafana (TeslaMate's dashboards); the hub covers the same ground. Set `POSTGRES_TCP=1`
  to point a Grafana of your own at the database.
* No in-app updater and no automatic PostgreSQL *major* upgrade: a future PostgreSQL 19 build
  refuses to start on an 18 data directory and says so (dump/restore needed).
* Bluetooth vehicle commands from the hub don't work on macOS (see HUB_CHANGES.md #7);
  Fleet-API commands do.
* Some non-executable strings still mention build paths: compile-time source paths inside
  TeslaMate's `.beam` files and the compiled-in `OPENSSLDIR` of OpenSSL inside some Python wheels.
  Nothing loads from them — every child gets `OPENSSL_CONF` pointing at the bundle's own
  `etc/openssl.cnf` — and no Mach-O references a path outside the bundle or the OS.
