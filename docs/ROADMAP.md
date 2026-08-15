# OhmyTesla 路线图

> **这份文件是 2026-08-05 从市场调研直接生成的原始计划，不是进度表。**
> 它描述的服务拆分（NestJS 的 `ohmy-core`、独立的 `ohmy-dashcam` / `ohmy-web`、
> 以及一个独立的 `ohmy` Postgres schema）**从未落地**，实际架构是单个 FastAPI
> 容器 + 自有 SQLite，见 [ARCHITECTURE.md](ARCHITECTURE.md)。
> 下面的复选框保持原样是为了留档，**真实进度看这里**：

| 阶段 | 状态 | 说明 |
|---|---|---|
| Phase 0 编排与合规 | ✅ 基本完成 | compose 编排、AGPL、商标声明齐了；**MQTT 鉴权与认证前置仍未做**（见下方风险） |
| Phase 1 三大主功能 | ✅ 已上线 | 行车记录仪工作室、哨兵告警、数据门户、引导页全部可用 |
| Phase 2 融合与告警广度 | ✅ 大部分已上线 | 时钟自动校正、行程⇄录像、电价引擎、19 条车辆指令、Web Push、i18n/单位、中转桥接都已实现；隐私工具（打码）、导出布局扩展、SEI 入库分析未做 |
| Phase 3 生态与长尾 | ⬜ 未开始 | 自动化引擎、电池健康 v2、社区统计、证据包导出等 |

**已知与计划不符的地方**：
- Phase 0 的「authed MQTT / oauth2-proxy 认证前置 / CHECK_ORIGIN」列为 P0，
  实际 `docker-compose.yml` 仍是 `mosquitto-no-auth.conf`、Grafana 仍是 admin/admin。
  目前的缓解是「默认只适合内网 + 可选 `OHMYTESLA_PASSWORD`」，不等于做完了。
- Phase 2 说要 **bundle TeslaMateAgile**，实际是 `tariff/calc.py` 从零重写的
  分时电价引擎（支持跨时段按时间占比切分，TeslaMateAgile 没有这个）。
- Grafana 社区面板包的 provisioning 挂载没做，只挂了数据卷。
- 设计文档里的 AI 指令层（§18-21、§53）**完全未实现**。

---

## Phase 0 — Scaffold & compliance (weeks 1-4)

- [ ] Monorepo + docker-compose overlay running unmodified teslamate/teslamate, teslamate/grafana, postgres:18-trixie, mosquitto — with hardened defaults: MQTT auth, oauth2-proxy/Authelia front, CHECK_ORIGIN, one-command install script
- [ ] Legal groundwork: AGPL-3.0 license for OhmyTesla, TRADEMARK.md-required disclaimer in README, no 'TeslaMate' in product/domain naming, provenance policy for borrowed code (MIT-only vendoring, AGPL embedding documented)
- [ ] `ohmy` Postgres schema + dedicated read-only role on the TeslaMate DB; migration framework independent of TeslaMate's
- [ ] ohmy-core skeleton (NestJS): MQTT archiver persisting sentry_mode, heading, center_display_state and state transitions (data TeslaMate never stores) + healthchecks and REST/WebSocket stubs
- [ ] Grafana provisioning volume wiring with official + jheredianet + achievements dashboard packs and portal deep-link map
- [ ] CI (build, lint, integration test against a seeded TeslaMate DB fixture) and staging-upgrade test harness for TeslaMate image bumps

## Phase 1 — MVP: the three headliners (months 2-5)

- [ ] Dashcam ingest pipeline (ohmy-dashcam, Python/ffmpeg): USB/share scan, ffprobe per file, faststart remux, event.json parse, thumbnails, SEI extraction via an independently written wire-format decoder (teslamotors/dashcam has NO LICENSE file — its field definitions are factual and may be referenced, but no code or .proto may be vendored) into clips/events/clip_telemetry tables; encrypted-clip and moov-missing detection with guided UX
- [ ] Dashcam Studio viewer (ohmy-web): synced 4/6-cam playback with HEVC probe + server transcode fallback, SEI overlay gauges, TeslaMate-fused overlay (SOC/power/elevation/temps via drive-window join with interpolation and derived heading), event browser with geofence tags, MapLibre minimap
- [ ] Export v1: server ffmpeg composition — grid + PiP layouts, telemetry dashboard burn-in, timestamp overlay, hwaccel autodetect, quality tiers; job queue + progress UI
- [ ] Sentry alerts v1: fleet-telemetry optional compose profile (mTLS setup guide) + MyTeslaMate env profile + degraded MQTT-only tier; Aware/Panic FSM with 15s dedup and 90s escalation; channels: Telegram (inline mute button), Web Push (PWA), ntfy, SMTP; incident history feed with geofence tags; MQTT sentry_event republish
- [ ] Data portal v1: home (live MQTT summary), drives list/detail with map + speed/power/SOC/elevation traces, charge detail with kW-vs-SOC curve, battery health (TeslaMate derived-efficiency SQL in our UI), vampire drain, Grafana deep links — 'exhaustive data in one place' day-one via curated UI + provisioned dashboards
- [ ] Onboarding wizard v1: data-source tier selection (Owner API default / MyTeslaMate / DIY Fleet / zero-cost), cost estimator vs $10 credit, timezone + clip-offset calibration slider

## Phase 2 — Fusion depth & alert breadth (months 6-9)

- [ ] Drive-centric footage browsing (drive ⇄ stitched video ⇄ clickable route map) and clock-drift auto-correction via SEI-vs-positions cross-correlation
- [ ] Pre-SEI/parked-clip telemetry backfill overlays from TeslaMate positions
- [ ] Export parity: cockpit/sidebar/9:16 social-safe layouts, AP-colored minimap burned into exports, transparent-overlay-only export, WebCodecs+mp4-muxer instant in-browser clip export, motion-only fast-forward
- [ ] Timeline intelligence: speed-curve scrubber, FSD/manual bands, hard-braking markers; SEI analytics tables (per-drive FSD%, disengagements) surfaced in driving analytics
- [ ] Privacy toolkit: blur zones, WASM plate blur (YOLOv11n), GPS/timestamp-stripping export mode
- [ ] Sentry v2: break-in-with-sentry-off (DisplayStateLock + ChargePortLatch suppression), deterrent buttons via bundled vehicle-command proxy, incident⇄video auto-attach after teslausb sync, Apprise channel extensibility, latency health metric
- [ ] Alerts engine (Tessie-class): geofence, speed, charge events, doors/windows/tires, climate safety, software updates — on the shared notification fan-out
- [ ] Cost engine: bundled TeslaMateAgile + TOU/tariff editor incl. Chinese peak/valley with historical recalc; charging rollups; drive tagging + mileage export
- [ ] Platform: bundled TeslaMateApi profile (MateDroid/t-buddy compatibility), PWA polish (installability, iOS push docs), multi-vehicle UI, zh_Hans/zh_Hant portal i18n + GCJ-02/Amap map mode + NOMINATIM_PROXY/addr-fix bundling

## Phase 3 — Ecosystem & long tail (months 10+)

- [ ] Automations engine: triggers → signed commands (climate, charge limit/amps, sentry, lock) with allowlist gating; sentry scheduling with battery-savings estimates; BLE local commands via vehicle-command for garage hosts
- [ ] Battery health v2: multi-estimator pipeline with confidence bands (range-ratio + derived-efficiency + Fleet Telemetry BMS signals + Scan My Tesla CAN CSV import); cycles + lifetime throughput; guided official-test logging
- [ ] Opt-in anonymized community stats service: fleet degradation percentiles, firmware distribution tracker, leaderboards
- [ ] Evidence-package ZIP export, corrupt-clip carve recovery, custom drag-drop layout editor with shareable presets
- [ ] Encrypted-clip local decryption research track (dashcam.tesla.com account-key flow) — experimental flag
- [ ] Ecosystem bundles: teslamate-mcp AI analytics, teslamate-abrp bridge, achievements pack promotion into portal UI, HA sensor templates + addon packaging, Tessie/TeslaFi importer surfacing, WeChat notification channel
- [ ] China mode completion: developer.tesla.cn region flag, in-country telemetry hosting guide, Alibaba Cloud backup targets, WeChat mini-program evaluation

## 已知风险（调研结论）

- LICENSE (AGPL): TeslaMate relicensed MIT→AGPL-3.0 in v3.0.0, and its README asserts an aggressive interpretation ('if your software interacts with or relies on TeslaMate, it must be open-source'). Mitigation: never modify/patch the TeslaMate image (compose aggregation only), and license OhmyTesla itself AGPL-3.0 — which also legally permits embedding SentryGuard's AGPL break-in logic and eliminates community friction. Never vendor code from AGPL-incompatible or unlicensed sources (DeaglePC is AGPL — compatible for ideas but keep provenance clean; wemate has NO license — imitate features only; bt1142msstate is source-available personal-use — ideas only).
- TRADEMARK: TRADEMARK.md prohibits 'TeslaMate' in product names/domains (even '[Product] for TeslaMate') and requires a verbatim unaffiliation disclaimer for any tool touching its API or database — include it in README/About. Separate risk: 'Tesla' in 'OhmyTesla' is Tesla Inc.'s mark — app-store listings and domains may face objections; keep branding clearly unofficial and be prepared to rename.
- OWNER API MORTALITY: TeslaMate's default data source is a zombie — dead for business accounts, tightened in June 2026 (TLS change broke TeslaMate briefly), no EOL announced. If it dies, the 1Hz positions stream that powers overlay density degrades to coarser Fleet-Telemetry-replayed streaming. Mitigation: pluggable source adapters, env-only endpoint switching, and overlay code that tolerates sparser telemetry.
- API COST/PROXY DRIFT: Fleet API polling is a cost trap ($0.002/request ≈ $86/mo at 1-min cadence); proxies change pricing (MyTeslaMate dropped its free tier — TeslaMate docs are already outdated; Teslemetry repositioned paid). Mitigation: streaming-only designs sized under the $10/mo credit, a cost estimator in onboarding, and no hard dependency on any single proxy.
- DASHCAM ENCRYPTION (fw 2026.20+ default-on): new users' first experience is unparseable files. Detection + guidance (disable in Controls > Safety) is mandatory at MVP; the account-key local-decryption flow (dashcam.tesla.com, reverse-implemented by ExportDash) is technically and legally fragile — treat as experimental, never a launch dependency.
- SELF-HOSTED FLEET TELEMETRY BARRIER: requires developer app registration, hosted public key, FQDN + CA cert + mTLS, and virtual key pairing (MCU1 unsupported; pre-2021 S/X exempt rules) — a hard wall for average self-hosters. Mitigation: three-tier onboarding (MQTT-only degraded / MyTeslaMate / DIY) with honest capability labels; advertise latency honestly (1-5s good LTE, minutes in poor coverage).
- HEVC + BROWSER PLAYBACK: Chrome/Chromium decode HEVC only with platform hardware support (the documented killer of past Electron/web viewers); HW4 6-cam 2896x1876 loads can exhaust browser memory. Mitigation: per-file codec probing, server transcode fallback, WebCodecs feature detection, and server-side ffmpeg for heavy exports — but transcode CPU load on small NAS/Pi hosts remains a real constraint.
- CHINA: separate developer portal (developer.tesla.cn, +86 phone) and API base; GFW breaks Nominatim/OSM (NOMINATIM_PROXY + addr-fix backfill needed); maps require WGS-84→GCJ-02 conversion and Amap tiles; remote live camera is disabled in-country; data-residency (PIPL) argues for keeping all video processing local. The USB-ingestion path is the China-safe core.
- iOS PUSH CONSTRAINTS: Web Push works only when the PWA is installed to Home Screen (iOS 16.4+) with permission from a user tap; self-hosted ntfy needs the ntfy.sh upstream relay for instant APNS delivery. Telegram is the reliable cross-platform default; set expectations in docs to avoid 'alerts are slow' reputation damage.
- SCHEMA/DATA DRIFT: TeslaMate migrations auto-run at container start (tokens moved to a private schema, positions indexes rewritten 2025-2026); positions has no heading column; sentry state is never persisted to Postgres (MQTT-only — our archiver is a single point of failure for incident history). Mitigation: read-only role, column-pinned queries, staging upgrades, archiver monitoring/healthchecks.
- SECURITY EXPOSURE: the official stack ships no-auth mosquitto and default Grafana credentials; OhmyTesla adds video archives, location history and push channels. Shipping without a mandatory auth front (oauth2-proxy/Authelia), MQTT auth, and CHECK_ORIGIN would be negligent — hardened defaults are a P0 deliverable, not an option.
- SEI/FORMAT ASSUMPTIONS: SEI exists only on fw 2025.44.25+ HW3+ and is absent when parked; event.json camera numbering is community-reverse-engineered and inconsistent across tools; fps/resolution vary by firmware (HW4 front 24 vs 30fps sources conflict). Treat all format constants as per-file probed values and camera mapping as a heuristic verified against real data.

## 差异化亮点（无竞品实现）

- Video-database fusion overlay: automatic time-matched join of TeslaCam footage with TeslaMate Postgres, overlaying SOC, power/regen kW, elevation, temps, odometer and range that SEI cannot provide — the only existing product that combines both (Telemetry Overlay, $199) requires manual CSV export and manual alignment; we automate the entire join.
- Telemetry backfill for pre-SEI footage: clips recorded before firmware 2025.44.25 (and parked Sentry clips with no SEI) get speed/GPS/power/SOC overlays derived purely from TeslaMate positions — instantly annotates years of archived footage no other tool can touch.
- Drive-centric footage browsing: pick a drive from TeslaMate's drives table and watch its footage as one stitched timeline with the route map; click any point on the route to seek the video. Every competitor starts from the USB folder tree.
- Clock-drift auto-correction: cross-correlate SEI GPS/speed against TeslaMate positions to solve the exact per-car offset between clip filenames and wall clock — Telemetry Overlay makes users align by hand; nobody else even tries.
- Sentry incident ⇄ video ⇄ location fusion: real-time Aware/Panic alert, auto-tagged with TeslaMate geofence ('Sentry @ Home'), then automatically linked to the actual SentryClips footage once the car syncs over home WiFi (teslausb integration) — commercial sentry apps have alerts but no video; viewers have video but no alerts.
- Longitudinal SEI analytics in SQL: one-time ingestion of dashcam SEI into Postgres enables archive-wide queries no player can do — FSD% and disengagement counts per drive, hard-braking event search, AP-state timeline bands — and provides FSD usage analytics without any Tesla API support.
- Self-hosted sentry alerts with actionable buttons at effectively zero cost: a SentryMode-focused Fleet Telemetry config fits inside Tesla's $10/month free credit, versus $5.99-$12.99/month subscriptions (SentryPro/Tessie), with Telegram honk/flash/mute buttons and no per-vehicle billing.
- MQTT sentry-event republishing (teslamate/cars/$car_id/sentry_event): no existing product exposes sentry incidents to Home Assistant/Node-RED automation — this plugs directly into TeslaMate's existing integration culture.
- Zero-cost mode: the entire app functions with no Tesla developer account, no fees, no cloud — TeslaMate MQTT + Postgres + local USB TeslaCam ingestion — with Fleet Telemetry as an opt-in upgrade. Also the China-safe core: USB parsing is fully offline, identical worldwide, and sidesteps data-residency issues.
- Open data hub: telemetry export API in the format dashcam viewers already consume (Sentry Clip Viewer imports TeslaMate/Tessie data today), TeslaMateApi compatibility for existing mobile apps (MateDroid, t-buddy), and importers/exporters so users are never locked in.