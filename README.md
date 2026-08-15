<div align="center">

<img src="services/hub/app/web/static/icon.svg" width="76" height="76" alt="OhmyTesla">

# OhmyTesla

**A self-hosted Tesla companion — dashcam studio, sentry alerts, energy insights,<br>charging costs and vehicle control, all on one page.**

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
[Dashcam](#dashcam) · [Charging costs](#charging-costs) · [AI access](#ai-access-mcp) ·
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

> [!WARNING]
> An empty `OHMYTESLA_PASSWORD` means **no login at all**, and this UI can unlock your doors,
> disable Sentry, and replay everywhere you have been. Port 8800 is reachable from your whole
> LAN, not just this machine.

## Connect your car

Tesla does not allow third-party password login, so fetch a token once:

```bash
./tools/tesla_auth      # opens Tesla's own login page; your password only goes to Tesla
```

Paste both tokens into the setup page and hit **Connect**. They refresh automatically after that.

## Phone alerts

**ntfy** — recommended, arrives in seconds. Install the app, pick an unguessable topic, then in `.env`:

```ini
NTFY_URL=https://ntfy.sh
NTFY_TOPIC=omt-<random>                 # echo "omt-$(openssl rand -hex 10)"
PUBLIC_URL=http://192.168.1.20:8800     # an address your phone can reach, so links open
```

**Email** also works (`SMTP_HOST/PORT/USER/PASS/FROM/TO`); Gmail needs an
[app password](https://myaccount.google.com/apppasswords), not your login password.
It takes minutes to arrive — use it as an archive, never as your only sentry alert.

Run `./omt restart`, then **Security → Send test alert**. If nothing arrives, that page names
the missing piece.

> [!IMPORTANT]
> The topic name *is* the password. ntfy.sh is a public server: anyone who knows the string
> receives your alerts.

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
| <http://localhost:3001> | `admin` / see `./omt grafana-password` | Grafana panels |
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
     (portal password; the username is always `ohmytesla`. Empty means no login at all.)
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
- OHMYTESLA_PASSWORD only guards :8800. Grafana (3001) and TeslaMate (4000) are separate
  containers with their own auth — securing one does not secure the others.
````

## Security

> [!CAUTION]
> **LAN only.** All three ports bind `0.0.0.0`, so every device on your Wi-Fi can reach them.
> Never port-forward any of this.

| Surface | Auth | Exposes |
|---|---|---|
| `:8800` | Basic (`ohmytesla` + your password) | unlock doors, open trunk, disable Sentry, full location history and footage |
| `:8800/mcp` | same password | the whole database, read-only |
| `:8800/healthz` | **exempt by design** | liveness only, no vehicle data |
| `:3001` Grafana | `admin/admin` by default | all dashboards |
| `:4000` TeslaMate | **none — and it supports none** | native admin UI, can change collection settings |
| mosquitto | not published to the host | Docker network only; unreachable from the LAN |

`OHMYTESLA_PASSWORD` covers the entire app except `/healthz`, but **only port 8800**:

```bash
./omt grafana-password              # existing install — editing .env will not work
GF_SECURITY_ADMIN_PASSWORD=...      # fresh install only, read once at volume init
MQTT_USERNAME=...                   # MQTT auth, settable through compose now
MQTT_PASSWORD=...
```

TeslaMate 4.0.1 offers no HTTP auth at all — its `runtime.exs` only reads `DATABASE_*`, `MQTT_*`
and `SECRET_KEY_BASE` — so `:4000` can only be protected by not exposing it. To keep 3001 and
4000 on this machine only, prefix both port lines in `docker-compose.yml` with `127.0.0.1:`;
the cost is losing phone and tablet access to Grafana.

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
| [Vehicle commands](docs/COMMANDS.md) | the 19 supported commands *(Chinese)* |
| [Fleet Telemetry](docs/FLEET-TELEMETRY.md) | streaming setup *(Chinese)* |
| [Feature matrix](docs/FEATURE-MATRIX.md) | 71 features against every competitor |

## License

[AGPL-3.0](LICENSE), same as TeslaMate.

> This project is an unofficial community tool and is not affiliated with, endorsed by, or supported by the official TeslaMate project.

Not affiliated with or endorsed by the TeslaMate team or Tesla, Inc. TeslaMate and Tesla are
trademarks of their respective owners. This project orchestrates the **unmodified** TeslaMate
Docker image under AGPL-3.0. Dashcam SEI decoding is an independent implementation based on
Tesla's publicly published schema.
