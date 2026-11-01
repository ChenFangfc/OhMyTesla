# Changes needed outside the data/analytics scope

Everything below is in files owned by someone else (`main.py`, `alerts/*`, `mcp/write_tools.py`,
`tests/run.sh`, templates/JS). Each item says why, and the exact change. No new phone pushes
are proposed anywhere (the owner wants pushes only for Sentry and major events).

## 1. `main.py` — start the background pricer (audit data-api F4)

Reads already price new sessions into hub.db, but **write-back to TeslaMate** (`COST_WRITE_BACK=true`)
should not depend on someone opening a page. `insights/costs.py` has the loop; it only needs wiring:

```python
# lifespan(), after automation.start()
from .insights.costs import autopricer
autopricer.start()          # every 5 min: price finished sessions; write back if COST_WRITE_BACK
...
# shutdown, next to `await automation.stop()`
await autopricer.stop()
```
It never sends notifications.

## 2. `main.py` — let TeslaMateApi clients authenticate with a token (audit F14)

`BasicAuthMiddleware` rejects every request without Basic credentials before
`compat/_check_token` can run, so token-only apps (MateDroid with an API token, widgets) cannot
connect. In `BasicAuthMiddleware.dispatch`, after the "password not set → 503" block and
**before** the cross-site check (browsers never attach a Bearer token by themselves, so this
cannot be used for CSRF):

```python
        tok = settings.teslamateapi_token
        path = request.url.path
        if tok and (path.startswith("/api/v1/") or path in ("/api/ping", "/api/readyz")):
            got = request.headers.get("x-api-token", "")
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
            if got and secrets.compare_digest(got.encode(), tok.encode()):
                return await call_next(request)
```
With no token configured nothing changes. `compat/_check_token` keeps requiring the token when
one is configured, so a Basic-only caller still needs it (the "family gets Basic, app gets token"
split documented in the compat module).

## 3. `main.py` — surface the TeslaMate schema guard in `/healthz`

`db.py` now checks `schema_migrations` (verified up to `20261014120000`, TeslaMate 4.4.0) and the
columns the hub reads on the first DB connection, logs ERROR when TeslaMate is newer or columns
are missing, and serves the result at `GET /api/health/teslamate`. To make it visible to
`./omt status` and monitoring, add to `healthz()` (keep HTTP 200):

```python
    from . import db
    ts = db.teslamate_schema()
    if ts.get("status") in ("newer", "incompatible"):
        body["teslamate_schema"] = {k: ts.get(k) for k in
                                    ("status", "version", "verified_version", "missing_columns")}
```
The UI should show a banner when `GET /api/health/teslamate` returns `warning: true`
(i18n keys `analytics.schema.*` exist).

## 4. `alerts/engine.py` — stop alerting on scheduled charge stops (alerts audit F10)

Live: 11/11 `charging_interrupted` alerts were false; 8 fired at 06:00 local, the end of the
owner's off-peak window. The current code fires `charging_stopped_early` on `Stopped`, which
still triggers there. `insights/charging_stops.classify_stop()` learns the car's habitual stop
times from TeslaMate (live: **06:00, 10 sessions**) and returns
`{interrupted, reason, evidence}` with `reason` in
`no_power | limit_reached | dc_session | scheduled_stop | scheduled_charging | unexplained`.
In `handle()`, the `key == "charging_state" and old == "Charging"` branch:

```python
            if new == "Stopped":
                from ..insights.charging_stops import classify_stop
                verdict = await classify_stop(
                    int(car_id),
                    end_soc=_num(car.get("battery_level")),
                    charge_limit_soc=_num(car.get("charge_limit_soc")),
                    is_dc=None,                      # inferred from the open session's samples
                    scheduled_charging_start_time=car.get("scheduled_charging_start_time"),
                    charging_state=new,
                )
                if not verdict["interrupted"]:
                    log.info("charge stop on car %s not alerted: %s", car_id, verdict["reason"])
                    return None
                # …existing level < limit - 2 check and _fire("charging_stopped_early", …)
```
Replayed against the 11 live alerts this suppresses 10 (8 `scheduled_stop`, 2 `dc_session`).
The 11th was a 14-minute AC top-up the owner ended by unplugging; to catch that class, defer the
decision ~120 s and skip if `plugged_in` became `"false"` or `charging_state` went back to
`Charging`. Keep `NoPower` alerting as is. Wrap the call so a DB outage falls back to the current
behaviour (`classify_stop` raises `db.DbUnavailable` only from its first query; it degrades to
"no learned schedule" otherwise).

## 5. `mcp/write_tools.py` — default car and naming (data-api F17)

`t_send_command` defaults to the first key of `live.cars` (on a 3-car account that can be a car
that left the account) and `what_will_happen` never names the car. Please:

```python
    from ..insights import cars as registry
    if car_id is None:
        car_id = await registry.default_car_id()      # most recently active, non-archived
        if car_id is None:
            return {"error": "没有可用的车辆"}
    meta = await registry.get_car(int(car_id))
    if meta is None:
        return {"error": f"没有 id 为 {car_id} 的车"}
    if meta["archived"]:
        return {"error": f"{meta['label']} 已归档（不在账号里或很久没有数据），不能下发指令"}
    ...
    "what_will_happen": f"{meta['label']}：{_describe(command, params)}",
    ...
    return {"ok": ok, "command": command, "car_id": car_id, "car": meta["label"], ...}
```
Include the label in the ticket args too, so a confirmation can't be replayed on another car.
The read tools in `mcp/tools.py` already do this.

## 6. `tests/run.sh` — run the new tests where their dependencies exist

Add to `NEEDS_RUNTIME` (they self-skip with exit 0 on a bare host):
`test_data_tariff.py test_data_recompute.py test_data_migrations.py test_data_pg.py`.
`test_data_omt.py` needs only bash + stdlib and runs on the host as is.
`test_data_pg.py` runs only with `OMT_TEST_PG=postgresql://user:pw@host:port/postgres`; it creates
and drops its own `omt_data_test_<pid>` database. In the CI integration job the compose
`database` service works for this, e.g. from the host:
`OMT_TEST_PG=postgresql://teslamate:$TM_DB_PASS@127.0.0.1:5432/postgres` once the port is published
(or run it inside the hub container with `postgresql://…@database:5432/postgres`).
`test_data_omt.py` also exercises a real `omt restore` when `OMT_TEST_PG` is set
(`OMT_TEST_PG_DISPOSABLE=1` additionally drops the `omt_mcp_readonly` role first to simulate a
fresh server — only on a throwaway server).

## 7. UI (templates/JS, redesign)

- Energy page session count: use `GET /api/insights/charging` → `n_sessions`, not `recent.length`
  (capped at 20). `recent[].cost_source` value `manual` is now `recorded`.
- `energy.html:311` hides the "set up a tariff" hint as soon as any session is computed. Use
  `GET /api/tariff/plan` → `saved` / `unconfigured` instead.
- Show `archived` cars (from `/api/cars` or `/api/analytics/cars`) as history-only; default car
  is `default_car_id`.
- Trips CSV `start`/`end` are now local with offset; anything parsing them must not assume UTC.
- New parity endpoints and their shapes: `insights/API.md`. New i18n keys: `analytics.*`
  in `i18n/locales/{en,zh}/insights.json`.

## 8. Lint gate (FYI)

`ruff check services/hub/app` currently fails only on files outside this scope:
`alerts/sentry.py` (I001 at 421, BLE001 at 568) and `livestate.py` (FURB162 at 35, S110 at 415).
All data/analytics files pass.

## 9. Other data-api audit items that live in your files (not changed here)

- F2 command params can override a command's fixed body (tesla/commands.py) — the new catalog's
  strict params should cover it; worth a test with `sentry_on` + `{"on": false}`.
- F6 TeslaMate :4000 published on all interfaces (docker-compose.yml) — bind to 127.0.0.1.
- F7 signed-command proxy TLS verification (tesla/commands.py).
- F15 `_same_site` accepts `Sec-Fetch-Site: same-site` (main.py:212-214) — use `same-origin`.
- alerts F13: the seeded `display_name` marker never clears, so `/api/overview` reports
  `stale_seconds` for a live car (livestate.py).
