# OhmyTesla data API (analytics, insights, costs, compat)

Reference for the UI redesign. Everything here is read-only JSON over HTTP Basic auth
(`ohmytesla:<OHMYTESLA_PASSWORD>`), served by the hub against TeslaMate v4.4.0's Postgres.
Source files: `insights/analytics.py` + `analytics_api.py` (new), `insights/queries.py`,
`insights/journey.py`, `insights/costs.py`, `tariff/calc.py`, `compat/teslamateapi.py`.

## Conventions (apply to every endpoint below)

| topic | rule |
|---|---|
| Units | **Always metric**: km, km/h, kWh, Wh/km, °C, bar, m. The UI converts (`i18n/units.py` has the canonical factors). Only `/api/v1/*` (TeslaMateApi compat) is pre-converted to the units it advertises. |
| Time | Instants are ISO-8601 with `+00:00`. "Local" dates/buckets (`YYYY-MM-DD`) are in the hub time zone (`TZ`, else `TESLACAM_TZ`, else UTC), echoed as `tz`. |
| `car_id` | `/api/analytics/*`: optional. Omitted → the **most recently active, non-archived** car. Every response carries `car` (`id`, `label`, `model`, `archived`, `last_seen`, `efficiency_kwh_per_km`, `mass_kg`, `lfp_battery`). Unknown id → 404. Legacy `/api/insights/*` still require `car_id`. |
| Period | `?days=N` (last N days, `0` = all), or `?from=` / `?to=` (`YYYY-MM-DD` local calendar days, `to` inclusive; also ISO-8601 instants or epoch ms). `from`/`to` win over `days`. Each endpoint has its own default (noted below). Bad dates → 400. Echoed as `period: {from, to, tz}`. |
| Open sessions | Drives/charges with `end_date IS NULL` (in progress **or** orphaned by a TeslaMate restart) never cause errors: they are flagged `open: true`, their end is taken from the last sample, they are never priced. |
| Energy model | TeslaMate's: Δ(preferred range) × `cars.efficiency`. New `/api/analytics/*` honour `settings.preferred_range` (`rated`/`ideal`, echoed as `preferred_range`); the legacy `/api/insights/*` still assume `rated` (identical on this install). |
| Archived cars | `archived = car_settings.enabled is false OR (last_seen > 14 days ago AND no MQTT for it since hub start)`. `last_seen` = newest full `positions` row or `states` change. Open `states` rows of archived cars are capped at `last_seen` (no more "online for 68 days"). |
| Errors | `detail` is a string; machine code in header `X-Error-Code`: `db_unavailable` (503), `db_timeout` (504, query exceeded 30 s — use a shorter period), `tm_schema` (503, table/column missing: TeslaMate still migrating or schema changed), `db_query_failed` (500, bug). Out-of-range `limit`/`offset`/`max_points` are clamped, never 4xx/5xx. |

### Cost: one source of truth (`insights/costs.py`)

Every cost shown anywhere (summary, Energy, statistics, calendar, timeline, overview, exports,
`/api/v1/*/charges`, MCP) comes from `costs.session_costs()`:

| `cost_source` | meaning |
|---|---|
| `recorded` | TeslaMate's `charging_processes.cost` is set and was **not** written by the hub (hand-entered, or TeslaMate geofence pricing). Used as-is, never overwritten. |
| `computed` | Hub tariff engine. Includes sessions the hub wrote back (`written_back: true`); for those the latest computed value wins. `stale: true` = priced with an older tariff plan (Recompute updates it). |
| `unset` | Finished, but no tariff plan has been saved yet. |
| `ongoing` | `end_date IS NULL`. |

Billing energy = **wall side**, exactly TeslaMate's `put_cost`: `max(charge_energy_added, charge_energy_used)`.
TOU plans split that energy over time weighted by the per-sample wall energy (TeslaMate's
`charge_energy_used` integration, verified identical to the stored values on all 22 live sessions).
New finished sessions are priced automatically on read (≤ 2 s budget per request, hub DB only);
write-back to TeslaMate happens on manual recompute and in the background pricer (see NEEDS.md).

---

## New: `/api/analytics/*` (TeslaMate Grafana parity)

All return the common `car`/`period` blocks above. Arrays of rows unless noted.

### `GET /api/analytics/cars`
All cars with `archived`, `last_seen`, `days_since_seen`, `mqtt_live`, `model_line` (model, or from VIN[3] when TeslaMate left `model` NULL), `label`, plus `default_car_id`.

### `GET /api/analytics/drives` — *Drives* + *Drive Stats* panels
Params: period (default all), `limit` (1-1000, 100), `offset`, `min_distance_km`.
`drives[]`: `id, start, end, open, from, to, distance_km, duration_min, odometer_start_km, odometer_end_km, speed_max_kmh, speed_avg_kmh, power_max_kw, power_min_kw, outside_temp_avg_c, inside_temp_avg_c, ascent_m, descent_m, start_soc, end_soc, range_used_km, net_kwh, wh_per_km, efficiency_vs_rated_pct` (distance ÷ range used × 100), `slope_adjusted_efficiency_pct` (TeslaMate formula with the car's mass: **3100 kg for Cybertruck**, 2100 kg otherwise), `reduced_range` (cold pack).
`total` (rows matching), `summary`: `drives, open, distance_km, duration_min, net_kwh, wh_per_km, median_distance_km, speed_max_kmh, km_per_day, kwh_per_day, extrapolated_monthly_km, extrapolated_annual_km, top_destinations[{label, arrivals}]`.

### `GET /api/analytics/drives/{drive_id}` — *Drive Details* panel
Params: `max_points` (50-10000, 2000; peaks of speed/power are always kept).
- `drive`: metadata as above + `from/to {label, geofence, address, city, lat, lon}`, `elevation_min_m/max_m`, `speed_avg_moving_kmh`, `points`, `last_point`, `open`.
- `energy`: `efficiency_kwh_per_km, rated_wh_per_km, net_kwh, wh_per_km, efficiency_vs_rated_pct, regen_kwh` (stream rows with negative power, gaps < 1.5 s — TeslaMate "Energy recovered"; needs streaming), `regen_share_pct, mass_kg, slope_adjusted_kwh, slope_adjusted_efficiency_pct`.
- `speed_histogram[{speed_kmh, seconds, pct}]` (10 km/h buckets, time share).
- `series` (columnar, same length): `t, speed_kmh, power_kw, soc, usable_soc, elevation_m, outside_temp_c, inside_temp_c, range_km, odometer_km, lat, lon`. Stream rows have no temps/range (null).
- `route`: GeoJSON `LineString` (`coordinates [[lon, lat]]`, ≤ 1500 pts), `encoded` (Google polyline, precision 5), `bbox [minLon, minLat, maxLon, maxLat]`.

### `GET /api/analytics/speed-histogram`
Period default 90 days. `buckets[{speed_kmh, seconds, pct}]` over all drives (Drive Stats panel).

### `GET /api/analytics/charges` — *Charges* panel
Params: period, `limit`, `offset`. `charges[]`: `id, start, end, open, duration_min, type (AC|DC), charger_brand, location, added_kwh` (battery), `used_kwh` (wall), `billed_kwh, efficiency_pct, start_soc, end_soc, range_added_km, outside_temp_avg_c, peak_kw, avg_kw, cost, cost_source, cost_per_kwh`. Plus `total`, `currency`.

### `GET /api/analytics/charges/{cp_id}` — *Charge Details* panel
Params: `max_points` (50-5000, 800).
- `charge`: `type, ac_phases` (TeslaMate phase detection incl. √3), `charger_brand, charger_type, connector, location{…}, odometer_km, start/end_soc, start/end/added range_km, outside_temp_avg_c, peak_kw, avg_kw`,
  `energy {added_kwh, used_kwh, billed_kwh, losses_kwh, efficiency_pct}` (efficiency capped at 100 % like TeslaMate),
  `cost {cost, source, written_back, stale, currency, breakdown}` (`breakdown` = per-TOU-period kWh/cost under the current plan; keys `__flat__`, `__uncovered__`, `__free_supercharging__` or your period names).
- `curve[{soc, kw, samples}]`: average kW per SoC (TeslaMate "Charging curve" B).
- `series`: `t, minutes, soc, usable_soc, kw, kw_wall, voltage, current` (**null on DC**, 4.4.0 "AC input"), `pilot_current, phases, kwh_added, range_km, outside_temp_c, battery_heater`.

### `GET /api/analytics/charging-stats` — *Charging Stats* panel
Period on `end_date` (TeslaMate). `totals {charges, added_kwh, used_kwh, cost, duration_min, suc_cost, efficiency_pct, cost_per_kwh, cost_per_100km, gross_wh_per_km}`, `ac_dc {AC|DC: {sessions, added_kwh, used_kwh, duration_min, cost, cost_per_kwh}}`, `top_stations_by_energy[]`, `top_stations_by_cost[]`, `dc_curve[{soc, kw (median), samples}]`, `schedule` (see next).

### `GET /api/analytics/charging-schedule`
`days` (7-3650, 90). Learned habitual AC stop/start times: `stop_times[{local_time, minute, sessions}]`, `start_times[…]` (± 6 min clusters, ≥ 2 sessions). Live: stop **06:00 (10 sessions)** = end of the owner's off-peak window. Used by the `charging_interrupted` fix (NEEDS.md).

### `GET /api/analytics/battery` — *Battery Health* panel (`$aux` query)
`trend` = `month|week`. `efficiency_kwh_per_100km` (modal derived), `capacity_kwh {now, new, new_source (observed_max|user), difference}`, `degradation_pct`, `health_pct`, `range_100_km {now, max, max_source, lost}`, `soc_now, stored_kwh_now, charges, kwh_added_total, kwh_used_total, charging_efficiency_pct, cycles, ac_dc_kwh {AC, DC}`, `mileage` (= `/coverage`), `capacity_by_mileage[{date, odometer_km, kwh}]`, `range_trend[{date, range_100_km, odometer_km, outside_temp_c, soc, samples}]`. "New" values honour `POST /api/insights/battery/baseline`. Live parity: 116.7 / 117.2 kWh, 503.1 km, 23.4 kWh/100 km — identical to TeslaMate's SQL.

### `GET /api/analytics/efficiency` — *Efficiency* panel + speed bands
Params: period (default all), `min_distance_km` (1). `rated_wh_per_km`, `net {kwh, km, drives, wh_per_km}` (drives with range loss ≥ 0.1 km), `gross {kwh, km, wh_per_km}` (incl. parked/sentry/climate losses, TeslaMate's shared event query), `overhead_pct` (1 − net/gross), `by_temperature[{temp_c (5 °C buckets), drives, km, efficiency_pct, wh_per_km, avg_speed_kmh}]`, `by_speed[{speed_kmh, speed_max_kmh, km, segments, wh_per_km, efficiency_pct, avg_temp_c}]` (2-km segments of full rows bucketed by average speed — TeslaLogger method), `derived_efficiencies {rated|ideal: [{kwh_per_km, count}]}`.

### `GET /api/analytics/vampire` — *Vampire Drain* panel (4.3.0 standby fix)
Params: period (default all), `min_idle_h` (6). A window = previous drive/charge end → next start; kept if longer than `min_idle_h`, range did not increase, odometer moved < 1 km. `windows[{start, end, hours, standby_pct (asleep+offline share), start_soc, end_soc, soc_diff, range_lost_km, range_lost_per_hour_km, kwh, avg_watts, kwh_per_day, reduced_range}]` (`kwh` null when the pack was cold), `summary {windows, hours, kwh, kwh_per_day, avg_watts, km_per_day, asleep_pct}`. Live parity with TeslaMate: 7 windows / 18.55 kWh / 79.2 h.

### `GET /api/analytics/states` — *States* panel
Period default 7 days. `segments[{state, start, end, minutes}]` — non-overlapping; priority `updating > driving > charging > online > asleep > offline`, `unknown` = no `states` coverage (logger down). `totals_min{state}`, `pct{state}` (of covered time), `parked_pct`, `covered_min`, `uncovered_min`, `truncated` (> 5000 segments), `current {state, since, stale}` (`stale` = archived car), `period.effective_to` (capped for archived cars).

### `GET /api/analytics/statistics` — *Statistics* panel (incl. 4.3.0 `total`)
`period` = `day|week|month|year|total` (400 otherwise), period filter optional. `rows[]` (newest first): `date` (local bucket start, or `total`), `from, to, drives, duration_min, distance_km` (odometer span, TeslaMate), `logged_km` (Σ distance), `outside_temp_avg_c, efficiency_pct, net_kwh, consumption_net_wh_per_km, consumption_gross_wh_per_km, overhead_pct, speed_max_kmh, charges, open_charges, energy_added_kwh, energy_used_kwh` (Σ max(added, used)), `avg_energy_per_charge_kwh, cost` (all sessions in the bucket, single source), `cost_per_kwh, cost_per_100km`. Charge counts/energies use TeslaMate's filter (`added IS NULL OR > 0`); costs include every session so totals equal the other pages. Live parity: Aug 48 drives / 566 min / 1789.2 km / 577.57 kWh used.

### `GET /api/analytics/mileage` — *Mileage* panel, from positions
`bucket` = `day|week|month|year`. `rows[{date, odometer_km (max), km (Δ vs previous bucket), logged_km (drives), unlogged_km}]`, `start_odometer_km, end_odometer_km, total_km, logged_km, unlogged_km`. Unlike TeslaMate's panel this shows distance TeslaMate failed to log (live: +1,082 km after 2026-08-29).

### `GET /api/analytics/locations` — *Locations* + 4.4.0 *Visited*
Params: period, `limit` (20). `counts {addresses, cities, states, countries}`; `top_places[{label, geofence_id, geofence, address, city, lat, lon, arrivals, charges, dwell_min, last_visit}]` (geofence-aware grouping; dwell excludes gaps where the odometer moved, capped at 7 days); `cities[{country_code, country, city, region, first_visit, last_visit, days, visits, lat, lon}]` (city = `COALESCE(city, neighbourhood)`, region = `COALESCE(state_district, state)`); `countries[{country_code, country, cities, visits, first, last}]`; `summary {countries, cities, days}`.

### `GET /api/analytics/updates` — *Updates* panel
`count, median_days_between, current_version`, `updates[]` newest first: `id, version, build, start, end, install_min, observed_install` (false = TeslaMate back-filled a missed update), `days_on_version, charges, range_100_km_avg, current`.

### `GET /api/analytics/charge-level` — *Charge Level* panel
Period default 30 days, `max_points` (600). `bucket_s` (auto: 2 min … 1 day), `points[{t, soc, usable_soc}]`, `limits {lower: 20, upper: 80|100 (LFP)}`, `stats {avg, p7_5, median, p92_5, above_80_pct, below_20_pct}`.

### `GET /api/analytics/projected-range` — *Projected Range* panel
`bucket` = `hour|day|week|month` (day). `points[{date, range_100_km, odometer_km, outside_temp_c, soc, samples}]`, `now_km, max_km`.

### `GET /api/analytics/coverage`
`odometer_km` (latest position), `span_km` (positions), `logged_km` (Σ drives), `unlogged_km`, `coverage_pct`, `drives_span_km` (TeslaMate's drives-only span).

### `GET /api/health/teslamate` — schema guard
`status` ∈ `ok | older | newer | incompatible | not_ready | unreachable | error`, `version` (max `schema_migrations`), `verified_version` (20261014120000 = TeslaMate 4.4.0), `missing_columns[]` (`table.column` the hub reads), `warning` (show a banner). `?refresh=1` re-checks (cached 10 min when OK, 1 min otherwise). Checked automatically on the first DB connection; `newer`/`incompatible` are logged at ERROR.

---

## Changed existing endpoints (backward compatible unless noted)

| endpoint | change |
|---|---|
| `GET /api/cars` | + `model_line, label, enabled, archived, last_seen, days_since_seen, mqtt_live, display_priority`. |
| `GET /api/insights/summary` | `charge_cost` from the single source (live $86.93, was $28.95 from 7 stale sessions); `odometer` = latest position (44,5xx km, was last drive 43,439); + `odometer_logged_drives, kwh_billed, cost_sources{recorded,computed,unset,ongoing}`. `n_charges` counts finished sessions with max(added, used) > 0.1 kWh. |
| `GET /api/insights/charging` | Costs single-source; months in local time; + `n_sessions` (**use this, not `recent.length`**, which is capped at 20), `total_kwh, total_cost, cost_sources, monthly[].kwh_billed, recent[].kwh_used`. `recent[].cost_source` values are now `recorded|computed|unset|ongoing` (**`manual` renamed to `recorded`**). `recent[].type` uses `fast_charger_present` (was a 50 kW threshold). |
| `GET /api/insights/statistics`, `/calendar`, `/timeline` | Costs single-source (were `SUM(cost)` = 0). Calendar + `total_cost, currency`; timeline charge events + `cost_source`; statistics + `currency`. |
| `GET /api/insights/compare` | Costs single-source. |
| `GET /api/insights/states` | Open states capped (archived cars → `last_seen`); in-progress/orphan drives & charges counted by overlap; + `window_min, covered_min, archived, last_seen, effective_to`. Live car 2: `online_min 0` (was 43200). |
| `GET /api/insights/coverage` | Span from positions (was drives only); + `drives_span_km, odometer_km`. |
| `GET /api/insights/vampire` | Same shape, now computed by `/api/analytics/vampire` (TeslaMate method); + `avg_watts, asleep_pct, method`, items + `to, kwh, avg_watts, standby_pct, reduced_range`. |
| `GET /api/insights/battery`, `/drives`, `/tpms`, `/records` | Day/month buckets in local time (were UTC). |
| `GET /api/insights/places`, `/visited` | City = `COALESCE(city, neighbourhood)`. |
| `GET /api/insights/charge-curve` | `cost` = effective cost; + `kwh_used`. |
| `GET /api/drives`, `/api/drives/tracks` | `limit` clamped (negative used to 503). |
| `GET /api/overview` | Default car = most recently active non-archived; + `archived, last_seen`; `charging.cost` effective + `cost_source, used_kwh`. |
| `GET /api/trips/export.csv` | `start`/`end` in **local time with offset** (`2026-08-28 22:07:52-05:00`, was naive UTC); `energy_kwh` and odometer blank when unknown (was 0.0); + `timezone` column. |
| `GET /api/export/charges.csv`, `/all.json` | Single-source cost; + `kwh_billed`; sessions with only wall energy included. |
| `POST /api/export/config/restore` | Validates `tariff_plan` (and shape of cost maps) before restoring; response + `rejected{key: reason}`; `ok` false if anything was rejected. `tariff_costs` added to the backup keys. |
| `GET /api/health/data` | `charges_without_cost` from the single source; new issues `incomplete_records` (open > 1 day, with ids) and `teslamate_schema`; + `archived, last_seen, teslamate_schema`; default car = most recently active. |
| `GET /api/tariff/preview?cp_id=` | Wall-side billing: `kwh` is now the **billed** (wall) energy; + `kwh_billed, kwh_added, kwh_used, energy_basis, free_supercharging, car_id, open`. cp 9: $12.58 (was $10.83). |
| `GET /api/tariff/plan` | + `saved, priced_sessions, stale_sessions, method, energy_basis`. |
| `POST /api/tariff/plan` | Null location rules are dropped (were saved and later crashed preview/recompute); bad `timezone` rejected; response + normalized `plan`. |
| `POST /api/tariff/recompute` | Serialized with auto-pricing (double-click = two identical runs, no frozen costs); + `unchanged, method`. Never prices or writes open sessions. |
| `POST /api/prefs` (via `i18n/prefs.py`) | Choosing a currency pins it (`currency_set`); market detection no longer resets it on the next boot. |

### TeslaMateApi compat (`/api/v1/*`)

- **Values are converted to the advertised units** (portal prefs): distances/odometer/ranges km→mi (÷1.609344), speeds → mph (integer), temperatures → °F, TPMS bar→psi (×14.5038) or kPa, `consumption_net` Wh/km→Wh/mi (×1.609344; upstream divides by mistake, we don't). Battery capacities stay kWh, elevation stays m (as upstream).
- `battery-health` uses TeslaMate's `$aux` algorithm (same as upstream); `rated_efficiency` is kWh/100 km like upstream.
- `drives` returns finished drives only (upstream behaviour); `drives`, `charges`, `updates` accept upstream `page`/`show` (+ `startDate`/`endDate` on drives & charges) as well as `limit`; all clamped.
- `charges[].cost` = effective cost (single source).
- `globalsettings` now has the upstream shape (`setting_id, account_info, teslamate_units, teslamate_webgui {preferred_range, language}, teslamate_urls`) plus the old flat unit keys.
- Auth today: Basic works; Bearer/X-Api-Token only **in addition** to Basic, because `main.py`'s Basic-auth middleware rejects first. See NEEDS.md for the middleware change that makes token-only clients work.

### MCP tools (`/mcp`)

All car-scoped tools default to the most recently active non-archived car and return `car {id, label, model, archived, last_seen, defaulted}` (+ `note_archived`). New read-only tools: `list_cars, get_charging_overview, get_statistics, get_efficiency, get_states, get_mileage, get_locations, get_software_updates`. `get_drive_detail` / `get_charge_curve` / `get_battery_health` now use the analytics implementations (compact, no full series). `get_vehicle_state` accepts `car_id` and labels cars.

## DB-side helpers for other modules

- `insights.cars`: `list_cars()`, `get_car(id)`, `default_car_id()`, `label(car)`, `open_state_cap(id)`, `mass_kg(model)`.
- `insights.costs`: `session_costs(car_id)`, `total()`, `count_by_source()`, `currency()`, `autopricer` (`start()/stop()/run_once()`).
- `insights.charging_stops`: `learned_schedule(car_id)`, `classify_stop(car_id, ended_at, end_soc=, charge_limit_soc=, is_dc=, scheduled_charging_start_time=, charging_state=)` → `{interrupted, reason: no_power|limit_reached|dc_session|scheduled_stop|scheduled_charging|unexplained, evidence}`.
- `db.check_teslamate_schema()`, `db.teslamate_schema()`, `db.DbTimeout` (subclass of `DbUnavailable`), `db.fetch(..., timeout_s=)`.
- `insights.period.parse_period()`, `local_tz_name()`; `insights.guard.guard` / `http_error`.
