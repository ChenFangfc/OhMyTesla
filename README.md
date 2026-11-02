<div align="center">

<img src="services/hub/app/web/static/icon.svg" width="72" height="72" alt="">

# OhmyTesla

**Your Tesla, your data, on your own machine.**<br>
Live status and controls, Sentry alerts on your phone, every drive and charge, and your dashcam — in one self-hosted app.

[![Download for Mac](https://img.shields.io/badge/Download-macOS_·_Apple_silicon-111?style=flat-square&logo=apple&logoColor=white)](https://github.com/ChenFangfc/OhMyTesla/releases/latest/download/OhmyTesla-arm64.dmg)
[![Release](https://img.shields.io/github/v/release/ChenFangfc/OhMyTesla?style=flat-square&color=2a78d6)](https://github.com/ChenFangfc/OhMyTesla/releases/latest)
[![License](https://img.shields.io/badge/license-AGPL--3.0-555?style=flat-square)](LICENSE)
[![简体中文](https://img.shields.io/badge/README-简体中文-d9d9d9?style=flat-square)](README.zh-CN.md)

**[Download for Mac](https://github.com/ChenFangfc/OhMyTesla/releases/latest/download/OhmyTesla-arm64.dmg) · [Run with Docker](#docker) · [Phone alerts](#phone-alerts) · [Docs](#docs)**

<img src="docs/readme/film.en.webp" alt="OhmyTesla in 36 seconds: the Cybertruck's light bar comes on, its live state pops out on the 3D truck, a Sentry push lands on a phone, a year of drives and charges, the lifetime footprint, an AI assistant answering from your own data, and the Mac menu bar" width="100%">

<sub><a href="https://github.com/ChenFangfc/OhMyTesla/releases/latest/download/ohmytesla-film.en.mp4"><b>▶ The film in 4K, with sound (0:36)</b></a> · 3D model “Tesla Cybertruck 2025” by Nieve5677, CC BY 4.0</sub>

<table>
<tr>
<td align="center" width="25%"><h3>≈5 s</h3><sub>from a Sentry trigger<br>to your phone</sub></td>
<td align="center" width="25%"><h3>0.2%</h3><sub>of one CPU core: the whole<br>stack, idle, on a Mac</sub></td>
<td align="center" width="25%"><h3>Local</h3><sub>drives, charges and places<br>stay on your machine</sub></td>
<td align="center" width="25%"><h3>Free</h3><sub>open source (AGPL-3.0),<br>no subscription</sub></td>
</tr>
</table>

</div>

---

## What it does

<table>
<tr>
<td width="50%"><img src="docs/readme/overview.en.webp" alt="Overview"><br><b>Overview.</b> A true-scale 3D Cybertruck that shows what is open, locked or charging, with the place in words.</td>
<td width="50%"><img src="docs/readme/security.en.webp" alt="Security"><br><b>Sentry.</b> Someone at the car reaches your phone in about 5 seconds; on iPhone the push carries the OhmyTesla icon. Routine events stay in the app.</td>
</tr>
<tr>
<td><img src="docs/readme/charging.en.webp" alt="Charging data"><br><b>Data.</b> Every drive and charge, costs from your own tariff, battery health over time.</td>
<td><img src="docs/readme/footprint.en.webp" alt="Footprint"><br><b>Footprint.</b> Every road you've driven, on one lifetime map.</td>
</tr>
<tr>
<td><img src="docs/readme/controls.en.webp" alt="Controls"><br><b>Controls.</b> Climate, charging, locks, windows, tonneau. Risky commands ask first.</td>
<td><img src="docs/readme/mac.en.webp" alt="Mac menu bar"><br><b>Mac app.</b> Status and quick settings in the menu bar. No Docker.</td>
</tr>
</table>

Also: dashcam clips read straight off the USB drive and matched to drives and Sentry events, an AI connector ([MCP](docs/MCP.md)) that answers questions from your own data, and English or Chinese throughout. Data collection is [TeslaMate](https://github.com/teslamate-org/teslamate), unmodified.

## Install

### Mac

1. [Download the DMG](https://github.com/ChenFangfc/OhMyTesla/releases/latest/download/OhmyTesla-arm64.dmg) (Apple silicon, macOS 13+) and drag **OhmyTesla** to Applications.
2. First launch: right-click → **Open** (macOS 15+: System Settings → Privacy & Security → **Open Anyway**). The app isn't notarized yet.
3. Pick **Connect to a server** if one already runs at home (the app then needs about 40 MB with its window closed), or **Run on this Mac**. Then [set it up](#set-up).

Running on the Mac needs no Docker: the whole stack idles at about 0.2% of one CPU core and 300 MB of memory.

### Docker

On any Linux or Mac machine with Docker:

```bash
git clone https://github.com/ChenFangfc/OhMyTesla && cd OhMyTesla
./omt up                  # first run writes .env with random secrets and prints your password
```

Open `http://<this machine>:8800` (user `ohmytesla`), then [set it up](#set-up).

<details>
<summary>Everyday commands</summary>

| Command | Does |
|---|---|
| `./omt up` · `down` | start · stop (data kept) |
| `./omt status` | what is running, and whether data is flowing |
| `./omt logs` | follow logs (`./omt logs teslamate` for the collector) |
| `./omt restart` · `update` | after editing `.env` · after changing code |
| `./omt backup` | dump the database to `backups/` |

</details>

## Set up

The app walks you through this on first launch. **Settings → Setup guide** opens it again.

| Step | What to do | Time |
|---|---|---|
| 1. Connect your car | Sign in to Tesla with [tesla_auth](https://github.com/adriankumpf/tesla_auth/releases/latest) (computer) or [Auth app for Tesla](https://apps.apple.com/us/app/auth-app-for-tesla/id1552058613) (iPhone), then paste the two tokens into the app | 2 min |
| 2. Phone alerts | **iPhone:** install [Bark](https://apps.apple.com/app/id1403753865), copy the address it shows (`https://api.day.app/…`), paste it into the app and tap **Save and test**. **Android:** [ntfy](https://ntfy.sh), set up in **Settings → Notifications** | 2 min |
| Optional: vehicle controls | Lock, climate, frunk and more. Create an app at [developer.tesla.com](https://developer.tesla.com) (a card on file; one car stays within Tesla's free monthly credit), then follow **Settings → Vehicle controls** ([guide](docs/COMMANDS.md)) | about 10 min |
| Optional: Sentry alerts in 1–3 seconds | Add Fleet Telemetry. A paid relay is easiest; self-hosting is free but needs a public IP, a domain and a certificate ([guide](docs/FLEET-TELEMETRY.md)) | about 10 min with a relay |

Your password goes only to Tesla; the tokens stay encrypted on your own device.

## Phone alerts

Only Sentry and major events reach your phone, one line each:

```text
🚨 Cybertruck — Door Opened While Locked
Home · 9:41 PM
```

| Pushed | Kept in the app |
|---|---|
| Someone at the car, the alarm, a door opened while locked | Software updates, low battery, maintenance |
| The parked car moving (towed or driven off) | Data resumed, logger health |
| Low tire pressure, charging losing power | Sentry clips synced, routine checks |

Channels: **Bark** on iPhone (recommended: it shows the OhmyTesla icon, and alarms ring even in silent mode), **ntfy** on Android, plus Telegram, email and Web Push. Set them up in **Settings → Notifications** and tap **Send test**. Typical delay is about 5 seconds while Sentry is on, and 1–3 seconds with [Fleet Telemetry](docs/FLEET-TELEMETRY.md).

## Security

- Keep it on your home network or behind a VPN. Never port-forward it.
- The app refuses to serve without `OHMYTESLA_PASSWORD`. It can unlock your car.
- What each port exposes: [docs/SECURITY.md](docs/SECURITY.md).

## Docs

| | |
|---|---|
| [Quickstart](docs/QUICKSTART.md) | full walkthrough and troubleshooting *(Chinese)* |
| [Vehicle commands](docs/COMMANDS.md) | signed-command setup *(Chinese)* |
| [teslausb](docs/TESLAUSB.md) | the car's USB drive syncs footage by itself |
| [Fleet Telemetry](docs/FLEET-TELEMETRY.md) | 1–3 second alerts *(Chinese)* |
| [AI access (MCP)](docs/MCP.md) · [Let an AI install it](docs/AI-SETUP.md) | for Claude Code, Cursor and friends |
| [Mac app](packaging/macos/README.md) · [Architecture](docs/ARCHITECTURE.md) | how it's built *(architecture in Chinese)* |
| [Film source](promo/README.md) | the 30-second film, as code |

## License

[AGPL-3.0](LICENSE), like TeslaMate, which runs unmodified. Not affiliated with Tesla, Inc. or the TeslaMate project; Tesla and TeslaMate are trademarks of their owners. The bundled 3D model is CC BY 4.0 ([credits](services/hub/app/web/static/models/CREDITS.md)); icons are [Phosphor](https://phosphoricons.com) (MIT). Dashcam data decoding is an independent implementation of Tesla's published format.
