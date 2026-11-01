<div align="center">

<img src="services/hub/app/web/static/icon.svg" width="76" height="76" alt="OhmyTesla">

# OhmyTesla

**A self-hosted Tesla companion — Sentry alerts on your phone the moment they fire,<br>3D vehicle control, dashcam studio, energy insights and charging costs, all on one page.**

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue?style=flat-square)](LICENSE)
[![Built on TeslaMate](https://img.shields.io/badge/built_on-TeslaMate-cc0000?style=flat-square)](https://github.com/teslamate-org/teslamate)
[![Self-hosted](https://img.shields.io/badge/your_data-never_leaves_your_machine-2ea44f?style=flat-square)](#security)
[![MCP](https://img.shields.io/badge/MCP-15_read--only_tools-8a63d2?style=flat-square)](#ai-access-mcp)

**English** · [中文](README.zh-CN.md)

</div>

---

Data collection runs on [TeslaMate](https://github.com/teslamate-org/teslamate) — unmodified,
so you can upgrade it any time. Everything else is this project.

**[Install](#install)** · [Connect your car](#connect-your-car) · [Phone alerts](#phone-alerts) ·
[Vehicle control](#vehicle-control) · [3D car](#3d-car) · [Dashcam](#dashcam) · [Charging costs](#charging-costs) · [AI access](#ai-access-mcp) ·
[Commands](#commands) · [Automated setup](#automated-setup) · [Security](#security)

---

## Install

```bash
git clone <this repo> && cd OhmyTesla
cp .env.example .env

# Generate a portal password, paste it into OHMYTESLA_PASSWORD= in .env
openssl rand -base64 32 | tr -d '/+=' | cut -c1-28

./omt up
```

Open **http://localhost:8800** — username `ohmytesla`, password as generated above.

**On a Mac without Docker**, the native app runs the same stack (TeslaMate, Postgres, Mosquitto
and the hub) from one DMG: [download it from Releases](https://github.com/ChenFangfc/OhMyTesla/releases/latest)
(Apple Silicon, macOS 13+). Building it yourself: [packaging/macos/README.md](packaging/macos/README.md).

> [!WARNING]
> This UI can unlock your doors, disable Sentry, and replay everywhere you have been, so with an
> empty `OHMYTESLA_PASSWORD` it refuses to serve at all (HTTP 503). Port 8800 is reachable from
> your whole LAN, not just this machine.

## Connect your car

Tesla does not allow third-party password login, so fetch a token once:

```bash
./tools/tesla_auth      # opens Tesla's own login page; your password only goes to Tesla
```

Paste both tokens into the setup page and hit **Connect**. They refresh automatically after that.

## Phone alerts

Only **Sentry** and **things that really matter** reach your phone, one line each:

```text
🚨 Cybertruck — door opened while locked
Home · 9:41 PM
```

| Pushed to your phone | Kept in the app only |
|---|---|
| Sentry: someone at the car, the alarm, a door or handle on a locked car | Software updates, low battery, maintenance due |
| The parked car moving (towed or driven off) | Data resumed, logger health checks |
| Sentry alerts paused (no data from the car) | Sentry clips synced |
| Low tire pressure, charging losing power | Other routine diagnostics |

Each type can be switched to **Push / In app / Off** under **Security → Alert types**. You coming
back with your key card or phone key is not an intrusion; at home only escalated events push by
default, which you can change per place in **Settings → Sentry alerts**.

**Channels** are set up in **Settings → Notifications**, no `.env` editing: ntfy (recommended,
arrives in seconds), Bark (iOS Critical Alerts, breaks through Do Not Disturb), Telegram, email,
webhook, Apprise. Headless installs can still use `NTFY_*`, `SMTP_*` and friends in `.env`; values
entered in the app win. Then **Security → Send test alert**.

Set `PUBLIC_URL` to an address your phone can reach, so the **Open** link in a push works.

| How fast you hear | Delay | Needs |
|---|---|---|
| TeslaMate polling | about 1 minute | nothing |
| Fast check while Sentry is on | about 10 seconds | nothing (reads with TeslaMate's token, never wakes the car) |
| Fleet Telemetry | 1–2 seconds | a public server or a relay, see [docs/FLEET-TELEMETRY.md](docs/FLEET-TELEMETRY.md) |

> [!IMPORTANT]
> An ntfy topic name *is* the password. ntfy.sh is a public server: anyone who knows the string
> receives your alerts. **Generate new topic** in Settings gives you a random one.

## Vehicle control

Tap a door, the frunk or the charge port on the 3D car on **Overview**; **Controls** puts climate,
seats, charging, windows and Sentry on one page. Cybertruck adds the tonneau, outlets, Powershare,
light bar and ride height.

Cars built since 2021 (every Cybertruck) only accept signed commands. Set that up once in
**Settings → Vehicle commands**: six guided steps (a Tesla developer app, a public key, sign-in,
and approving the virtual key in the Tesla app). No signing proxy to run. Details and
troubleshooting: [docs/COMMANDS.md](docs/COMMANDS.md).

## 3D car

A Cybertruck model ships with the app ([DriveOS](https://branzoom.github.io/driveos), CC BY 4.0).
For other cars, upload a `.glb` in **Settings → Vehicles → 3D model**. For doors and lids to open,
parts need canonical names, see [static/models/README.md](services/hub/app/web/static/models/README.md);
[`scripts/models/rig_vehicle.py`](scripts/models/rig_vehicle.py) converts a Blender model to that format.

## Dashcam

Plug the car's USB drive into this machine and open **Dashcam → Browse USB drive** — read in
place, nothing uploaded or copied. Or copy `TeslaCam/` into `data/teslacam/`.

> [!NOTE]
> Clips listed but no picture? Tesla can encrypt dashcam footage. Turn it off in the car under
> **Controls → Safety → Dashcam encryption**; only new recordings become readable.

## Charging costs

**Settings → Tariff** — enter your rate once (time-of-use and per-location supported), hit
recalculate, and every past charge gets a cost. Unpriced sessions show `–` instead of pretending
to be 0.

## AI access (MCP)

Ask *"how much did charging cost last month?"* or *"why did efficiency drop?"* and let the model
query the data itself:

```bash
claude mcp add --transport http ohmytesla http://localhost:8800/mcp \
  --header "Authorization: Basic $(printf 'ohmytesla:%s' \
    "$(grep '^OHMYTESLA_PASSWORD=' .env | cut -d= -f2-)" | base64)"
```

15 read-only tools — trips, charge curves, battery health, vampire drain, TPMS, alerts, sentry
sessions — plus a guarded `run_sql` with four independent layers:

| # | Guard |
|---|---|
| 1 | `SELECT` / `WITH` only; no write keywords, no comments |
| 2 | Runs as a dedicated **non-superuser role** (`omt_mcp_readonly`) |
| 3 | Read-only transaction, always rolled back |
| 4 | 200 rows and 8 seconds, enforced by a server-side cursor |

Layer 2 matters most: compose hands the hub TeslaMate's own account, which *is* a superuser.
Without the downgrade, `pg_read_file()` reads any file in the container and `private.tokens` is
readable. Both are `permission denied` now.

Nothing leaves your machine — which commercial apps cannot offer, since your data lives on their
servers.

## Commands

| Command | Does |
|---|---|
| `./omt up` · `down` | start · stop (data kept) |
| `./omt status` | what is running, and whether data is flowing |
| `./omt logs` | follow logs (`./omt logs teslamate` for the collector) |
| `./omt restart` | after editing `.env` |
| `./omt update` | after changing code (rebuilds) |
| `./omt backup` | dump the database to `backups/` |
| `./omt grafana-password` | set Grafana's admin password |

| URL | Login | What it is |
|---|---|---|
| <http://localhost:8800> | `ohmytesla` / `OHMYTESLA_PASSWORD` | OhmyTesla — the only one you need |
| <http://localhost:3000> | `admin` / see `./omt grafana-password` | Grafana panels (port: `GRAFANA_PORT`) |
| <http://localhost:4000> | none | TeslaMate native UI |

Press <kbd>⌘</kbd><kbd>K</kbd> for the command palette.

## Automated setup

Using Claude Code or Cursor? Paste the block below and it installs everything for you.

````text
Deploy OhmyTesla (a self-hosted Tesla companion) on this machine and set up phone push.

Prerequisite: Docker Desktop must be running. If it isn't, tell me to start it first.

1. In the repo root, run `cp .env.example .env`
2. Generate and write these three — never leave them empty, never use the sample values:
   - TM_ENCRYPTION_KEY  = `openssl rand -base64 32`
   - TM_DB_PASS         = `openssl rand -base64 18 | tr -d '/+='`
   - OHMYTESLA_PASSWORD = `openssl rand -base64 32 | tr -d '/+=' | cut -c1-28`
     (portal password; the username is always `ohmytesla`. Empty means the portal refuses to serve.)
3. If a port is taken, change OHMYTESLA_PORT / GRAFANA_PORT / TESLAMATE_PORT in .env
4. Run `./omt up`. It returns only once the app is actually reachable.
5. Generate an ntfy topic: `echo "omt-$(openssl rand -hex 10)"`
   Write it to .env as NTFY_TOPIC, together with NTFY_URL=https://ntfy.sh
6. Put this machine's LAN IP into PUBLIC_URL as http://<IP>:8800
   (macOS: `ipconfig getifaddr en0`; Linux: `hostname -I | awk '{print $1}'`)
7. Run `./omt restart` (reloads .env, does not rebuild the image)
8. Verify. With the password set, -u is mandatory or every request is 401:
   `curl -s -u ohmytesla:<password> -X POST http://localhost:8800/api/alerts/test`
   expect {"ok":true,"channels":["ntfy"]}
   Then confirm it truly landed — the API return alone is not proof:
   `curl -s "https://ntfy.sh/<topic>/json?poll=1&since=5m"`
9. Tell me the topic name and the portal password, and remind me to install the
   ntfy app, tap +, and enter that topic.
10. Tell me to open http://localhost:8800 (the browser will ask for ohmytesla plus the
    password) and paste the Tesla tokens into the setup page. Get them with
    `./tools/tesla_auth` — Tesla rejects username/password login for third parties.
11. Run `./omt status` and confirm all five containers are up.

Optional email archive: ask me for SMTP details. Gmail requires an app password
(https://myaccount.google.com/apppasswords, 2FA first), then write
SMTP_HOST/PORT/USER/PASS/FROM/TO and run `./omt restart`. Setting SMTP_HOST without a
password makes the app report "half configured" rather than pretend to work.

Notes:
- Changed .env → `./omt restart`. Changed code → `./omt update` (rebuilds).
- The ntfy topic is a password. Never put it anywhere public; .env is already gitignored.
- OHMYTESLA_PASSWORD only guards :8800. Grafana (3000) and TeslaMate (4000) are separate
  containers with their own auth — securing one does not secure the others.
````

## Security

> [!CAUTION]
> **LAN only.** 8800 and Grafana bind `0.0.0.0`, so every device on your Wi-Fi can reach them.
> Never port-forward any of this.

| Surface | Auth | Exposes |
|---|---|---|
| `:8800` | Basic (`ohmytesla` + your password) | unlock doors, open trunk, disable Sentry, full location history and footage |
| `:8800/mcp` | same password | the whole database, read-only |
| `:8800/healthz` | **exempt by design** | liveness only, no vehicle data |
| `:3000` Grafana | password generated by `./omt up` | all dashboards |
| `:4000` TeslaMate | **none — and it supports none** | native admin UI; **this machine only** by default (`TESLAMATE_BIND_ADDR`) |
| mosquitto | not published to the host | Docker network only; unreachable from the LAN |

`OHMYTESLA_PASSWORD` covers the entire app except `/healthz`, but **only port 8800**:

```bash
./omt grafana-password              # existing install — editing .env will not work
GF_SECURITY_ADMIN_PASSWORD=...      # fresh install only, read once at volume init
MQTT_USERNAME=...                   # MQTT auth, settable through compose now
MQTT_PASSWORD=...
```

TeslaMate offers no HTTP auth at all — its `runtime.exs` only reads `DATABASE_*`, `MQTT_*`
and `SECRET_KEY_BASE` — and its page can sign you out of Tesla. Sign-in, geofences and car
settings all live in OhmyTesla now, so `:4000` answers on this machine only; set
`TESLAMATE_BIND_ADDR=0.0.0.0` in `.env` to reach it from elsewhere. To keep Grafana on this
machine too, prefix its port line in `docker-compose.yml` with `127.0.0.1:` (`BIND_ADDR` would lock
8800 as well); the cost is losing phone and tablet access to Grafana.

For remote access, put the stack behind a VPN (Tailscale / WireGuard) or an authenticating
reverse proxy. It holds your location history, dashcam footage, notification channels, and
control of your car.

> [!NOTE]
> `run_sql` still reads every trip and position you have. That is its purpose, not a flaw.

## Documentation

| | |
|---|---|
| [Quickstart](docs/QUICKSTART.md) | full walkthrough, daily use, troubleshooting *(Chinese)* |
| [Architecture](docs/ARCHITECTURE.md) | how the pieces fit together *(Chinese)* |
| [Vehicle commands](docs/COMMANDS.md) | one-time signed-command setup, safety, troubleshooting *(Chinese)* |
| [teslausb](docs/TESLAUSB.md) | the car's USB drive syncs footage to this machine by itself |
| [Fleet Telemetry](docs/FLEET-TELEMETRY.md) | streaming setup *(Chinese)* |
| [Feature matrix](docs/FEATURE-MATRIX.md) | 71 features against every competitor |

## License

[AGPL-3.0](LICENSE), same as TeslaMate.

> This project is an unofficial community tool and is not affiliated with, endorsed by, or supported by the official TeslaMate project.

Not affiliated with or endorsed by the TeslaMate team or Tesla, Inc. TeslaMate and Tesla are
trademarks of their respective owners. This project orchestrates the **unmodified** TeslaMate
Docker image under AGPL-3.0. Dashcam SEI decoding is an independent implementation based on
Tesla's publicly published schema.
