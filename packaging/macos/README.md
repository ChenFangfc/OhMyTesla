# OhmyTesla for macOS

A native Mac app (Apple Silicon, macOS 13+) that runs the whole OhmyTesla stack
— PostgreSQL, Mosquitto, TeslaMate and the OhmyTesla hub — **without Docker or a
terminal**, or connects to an OhmyTesla server you already run elsewhere.

* menu-bar app with service status, start / stop / restart, logs, and a main
  window that shows the hub (it answers the portal's password prompt for you);
* a per-user background service (LaunchAgent) keeps logging and Sentry alerts
  going with the window closed, and starts at login;
* native notifications for critical / warning alerts (Sentry and other major events);
* optional "Keep Mac Awake While Sentry Mode Is On".

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
| `OHMYTESLA_VERSION=0.2.0 ./build.sh` | version shown in Finder and in the DMG name |

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
9. **DMG** — app + `Applications` link + "First launch.txt", compressed (ULMO).

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
   * **Connect to an existing OhmyTesla server.** Enter its URL and portal password (stored
     in the login keychain). The app is then just a native window + notifications.
4. The app asks once for permission to show notifications.

If the LaunchAgent cannot be registered (or the app runs translocated from the DMG), the
app runs the supervisor as its own child process instead and says so: monitoring then
stops when you quit the app. **Install Background Service** in the menu retries.

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
| `TZ` | time zone for TeslaMate and the hub (default: the Mac's) |

Never change `ENCRYPTION_KEY` after signing in: it encrypts the Tesla tokens in the database.

**Vehicle commands** are set up in the hub (Settings › Vehicle commands; signing happens
in-process): the wizard needs `PUBLIC_URL` in `config.env` to be a public **https** domain that
serves `/.well-known/appspecific/com.tesla.3p.public-key.pem` from this hub (e.g. a tunnel to
`http://127.0.0.1:47800`).

### Keeping the Mac awake

Monitoring needs the Mac awake. With **Keep Mac Awake While Sentry Mode Is On**, the
supervisor checks the hub every 30 s and, while any car reports `sentry_mode=true`, holds
an idle-sleep power assertion (`caffeinate -i`, i.e. `PreventUserIdleSystemSleep`). The
display may still sleep; closing a MacBook's lid without an external display still sleeps.

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

1. Menu bar › **Stop Services and Quit** (or `Quit` after **Stop Services**).
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
