"""针对 TeslaMate Postgres 的只读统计查询。

单位约定（与 TeslaMate 一致）:
- 距离 km，速度 km/h，功率 kW，能量 kWh
- cars.efficiency 单位是 kWh/km（如 0.152）
- 电池容量估算两种方法:
  1) 充电能量法: charge_energy_added / ΔSOC * 100（ΔSOC 足够大才可信）
  2) 额定续航法: (rated_range / SOC * 100) * efficiency
"""

from datetime import timezone

from .. import db


async def _currency() -> str:
    """方案没显式设币种时跟地区偏好走（之前硬编码 CNY）。"""
    try:
        from ..i18n.prefs import get as _get_prefs

        return (await _get_prefs()).get("currency") or "USD"
    except Exception:  # noqa: BLE001
        return "USD"



def _iso(dt) -> str | None:
    """TeslaMate 的 timestamp 列是 naive UTC——序列化时补上 +00:00，
    否则前端 new Date() 会当本地时间解析而整体偏移。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat()


async def cars() -> list[dict]:
    rows = await db.fetch(
        """
        SELECT c.id, c.name, c.model, c.marketing_name, c.trim_badging, c.vin,
               c.efficiency, c.exterior_color, c.wheel_type,
               s.lfp_battery
        FROM cars c
        LEFT JOIN car_settings s ON s.id = c.settings_id
        ORDER BY c.id
        """
    )
    return [dict(r) for r in rows]


async def summary(car_id: int) -> dict:
    drive = await db.fetchrow(
        """
        SELECT COUNT(*) AS n_drives,
               COALESCE(SUM(distance), 0) AS km,
               COALESCE(SUM(duration_min), 0) AS minutes,
               COALESCE(SUM(GREATEST(start_rated_range_km - end_rated_range_km, 0)), 0) AS rated_km_used,
               MAX(end_km) AS odometer,
               MIN(start_date) AS first_drive
        FROM drives WHERE car_id = $1
        """,
        car_id,
    )
    from ..tariff.calc import costs_map, get_plan

    charge = await db.fetchrow(
        """
        SELECT COUNT(*) AS n_charges,
               COALESCE(SUM(charge_energy_added), 0) AS kwh_added,
               COALESCE(SUM(charge_energy_used), 0) AS kwh_used,
               COALESCE(SUM(cost), 0) AS cost
        FROM charging_processes WHERE car_id = $1 AND charge_energy_added > 0.1
        """,
        car_id,
    )
    # TeslaMate 里没手填 cost 的场次，用电价方案算出的费用补上
    computed = await costs_map()
    plan = await get_plan()
    missing = await db.fetch(
        "SELECT id FROM charging_processes"
        " WHERE car_id = $1 AND charge_energy_added > 0.1 AND cost IS NULL",
        car_id,
    )
    computed_cost = sum(computed.get(str(r["id"]), 0.0) for r in missing)
    eff = await db.fetchrow("SELECT efficiency FROM cars WHERE id = $1", car_id)
    version = await db.fetchrow(
        "SELECT version, COUNT(*) OVER () AS n_updates FROM updates"
        " WHERE car_id = $1 ORDER BY start_date DESC LIMIT 1",
        car_id,
    )
    efficiency = float(eff["efficiency"] or 0) if eff else 0
    km = float(drive["km"] or 0)
    kwh_driven = float(drive["rated_km_used"] or 0) * efficiency
    return {
        "n_drives": drive["n_drives"],
        "km_driven": round(km, 1),
        "hours_driven": round(float(drive["minutes"] or 0) / 60, 1),
        "odometer": float(drive["odometer"] or 0),
        "first_record": _iso(drive["first_drive"]),
        "avg_consumption_wh_km": round(kwh_driven / km * 1000, 1) if km > 3 and efficiency else None,
        "n_charges": charge["n_charges"],
        "kwh_added": round(float(charge["kwh_added"]), 1),
        "kwh_used": round(float(charge["kwh_used"] or 0), 1),
        "charge_cost": round(float(charge["cost"]) + computed_cost, 2),
        "currency": plan.get("currency") or await _currency(),
        "cost_per_km": round(
            (float(charge["cost"]) + computed_cost) / km, 3
        ) if km > 10 else None,
        "software_version": version["version"] if version else None,
        "n_updates": version["n_updates"] if version else 0,
    }


async def battery_health(car_id: int) -> dict:
    car = await db.fetchrow(
        """
        SELECT c.efficiency, s.lfp_battery
        FROM cars c LEFT JOIN car_settings s ON s.id = c.settings_id
        WHERE c.id = $1
        """,
        car_id,
    )
    efficiency = float(car["efficiency"] or 0) if car else 0

    # 方法1: 充电能量推算（ΔSOC ≥ 25 的充电段）
    energy_based = await db.fetch(
        """
        SELECT start_date::date AS date,
               charge_energy_added / (end_battery_level - start_battery_level) * 100 AS capacity
        FROM charging_processes
        WHERE car_id = $1
          AND end_battery_level - start_battery_level >= 25
          AND charge_energy_added > 1
        ORDER BY start_date
        """,
        car_id,
    )

    # 方法2: 额定续航推算（按月取最大，剔除 SOC 过低的噪声点）
    range_based = await db.fetch(
        """
        SELECT date_trunc('month', date)::date AS month,
               MAX(rated_battery_range_km / NULLIF(usable_battery_level, 0) * 100) AS range_at_100
        FROM positions
        WHERE car_id = $1 AND usable_battery_level >= 40
          AND rated_battery_range_km IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        car_id,
    )

    e_series = [
        {"date": r["date"].isoformat(), "kwh": round(float(r["capacity"]), 2)}
        for r in energy_based
    ]
    r_series = [
        {
            "month": r["month"].isoformat(),
            "range_km": round(float(r["range_at_100"]), 1),
            "kwh": round(float(r["range_at_100"]) * efficiency, 2) if efficiency else None,
        }
        for r in range_based
    ]

    current = None
    baseline = None
    if r_series:
        recent = [x["range_km"] for x in r_series[-3:]]
        current = sum(recent) / len(recent)
        baseline = max(x["range_km"] for x in r_series)
    degradation = (
        round((1 - current / baseline) * 100, 1)
        if current and baseline and baseline > 0
        else None
    )
    return {
        "efficiency_kwh_km": efficiency,
        "lfp_battery": bool(car["lfp_battery"]) if car else False,
        "energy_based": e_series,
        "range_based": r_series,
        "current_range_at_100": round(current, 1) if current else None,
        "max_range_at_100": round(baseline, 1) if baseline else None,
        "degradation_pct": degradation,
        "current_capacity_kwh": round(current * efficiency, 1) if current and efficiency else None,
    }


async def charging(car_id: int) -> dict:
    """充电统计。费用优先用 TeslaMate 里手填的 cost，为空则用电价方案算出的值。"""
    from ..tariff.calc import costs_map, get_plan

    computed = await costs_map()
    plan = await get_plan()
    currency = plan.get("currency") or await _currency()

    def cost_of(cp_id, db_cost) -> float:
        if db_cost is not None:
            return float(db_cost)
        return float(computed.get(str(cp_id), 0.0))

    monthly_rows = await db.fetch(
        """
        SELECT cp.id, date_trunc('month', cp.start_date)::date AS month,
               cp.charge_energy_added AS kwh, cp.cost,
               COALESCE(fc.fast, false) AS is_dc
        FROM charging_processes cp
        LEFT JOIN LATERAL (
            SELECT bool_or(fast_charger_present) AS fast
            FROM charges WHERE charging_process_id = cp.id
        ) fc ON TRUE
        WHERE cp.car_id = $1 AND cp.charge_energy_added > 0.1
        ORDER BY 2
        """,
        car_id,
    )
    monthly: dict[str, dict] = {}
    for r in monthly_rows:
        m = monthly.setdefault(
            r["month"].isoformat(),
            {"month": r["month"].isoformat(), "kwh": 0.0, "kwh_dc": 0.0, "cost": 0.0, "sessions": 0},
        )
        kwh = float(r["kwh"] or 0)
        m["kwh"] += kwh
        if r["is_dc"]:
            m["kwh_dc"] += kwh
        m["cost"] += cost_of(r["id"], r["cost"])
        m["sessions"] += 1

    loc_rows = await db.fetch(
        """
        SELECT cp.id, COALESCE(g.name, a.city, a.display_name, '未知地点') AS location,
               cp.charge_energy_added AS kwh, cp.cost
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        WHERE cp.car_id = $1 AND cp.charge_energy_added > 0.1
        """,
        car_id,
    )
    locs: dict[str, dict] = {}
    for r in loc_rows:
        loc = locs.setdefault(
            r["location"], {"location": r["location"], "kwh": 0.0, "cost": 0.0, "sessions": 0}
        )
        loc["kwh"] += float(r["kwh"] or 0)
        loc["cost"] += cost_of(r["id"], r["cost"])
        loc["sessions"] += 1

    recent = await db.fetch(
        """
        SELECT cp.id, cp.start_date, cp.end_date, cp.charge_energy_added, cp.charge_energy_used,
               cp.start_battery_level, cp.end_battery_level, cp.duration_min, cp.cost,
               COALESCE(g.name, a.city, a.display_name, '') AS location,
               fc.max_power
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        LEFT JOIN LATERAL (
            SELECT MAX(charger_power) AS max_power
            FROM charges WHERE charging_process_id = cp.id
        ) fc ON TRUE
        WHERE cp.car_id = $1 AND cp.charge_energy_added > 0.1
        ORDER BY cp.start_date DESC LIMIT 20
        """,
        car_id,
    )
    return {
        "currency": currency,
        "computed_sessions": len(computed),
        "monthly": [
            {**m, "kwh": round(m["kwh"], 1), "kwh_dc": round(m["kwh_dc"], 1),
             "cost": round(m["cost"], 2)}
            for m in monthly.values()
        ],
        "top_locations": sorted(
            [{**v, "kwh": round(v["kwh"], 1), "cost": round(v["cost"], 2)} for v in locs.values()],
            key=lambda x: x["kwh"], reverse=True,
        )[:10],
        "recent": [
            {
                "id": r["id"],
                "start": _iso(r["start_date"]),
                "duration_min": r["duration_min"],
                "kwh": round(float(r["charge_energy_added"] or 0), 1),
                "soc": f"{r['start_battery_level']}→{r['end_battery_level']}",
                "cost": round(cost_of(r["id"], r["cost"]), 2),
                "cost_source": "manual" if r["cost"] is not None else (
                    "computed" if str(r["id"]) in computed else "unset"
                ),
                "location": r["location"],
                "max_power": r["max_power"],
                "type": "DC" if (r["max_power"] or 0) >= 50 else "AC",
            }
            for r in recent
        ],
    }


async def drives(car_id: int) -> dict:
    eff_row = await db.fetchrow("SELECT efficiency FROM cars WHERE id = $1", car_id)
    efficiency = float(eff_row["efficiency"] or 0) if eff_row else 0

    monthly = await db.fetch(
        """
        SELECT date_trunc('month', start_date)::date AS month,
               SUM(distance) AS km,
               SUM(duration_min) AS minutes,
               COUNT(*) AS n,
               SUM(GREATEST(start_rated_range_km - end_rated_range_km, 0)) AS rated_used
        FROM drives WHERE car_id = $1 AND distance > 0.1
        GROUP BY 1 ORDER BY 1
        """,
        car_id,
    )
    by_temp = await db.fetch(
        """
        SELECT round(outside_temp_avg / 3) * 3 AS temp_bucket,
               SUM(GREATEST(start_rated_range_km - end_rated_range_km, 0)) / NULLIF(SUM(distance), 0) AS range_ratio,
               COUNT(*) AS n
        FROM drives
        WHERE car_id = $1 AND distance > 3 AND outside_temp_avg IS NOT NULL
        GROUP BY 1 HAVING COUNT(*) >= 3 ORDER BY 1
        """,
        car_id,
    )
    recent = await db.fetch(
        """
        SELECT d.start_date, d.distance, d.duration_min, d.speed_max, d.power_max,
               d.ascent, d.descent, d.outside_temp_avg,
               GREATEST(d.start_rated_range_km - d.end_rated_range_km, 0) AS rated_used,
               COALESCE(sg.name, sa.city, '') AS from_loc,
               COALESCE(eg.name, ea.city, '') AS to_loc
        FROM drives d
        LEFT JOIN addresses sa ON sa.id = d.start_address_id
        LEFT JOIN addresses ea ON ea.id = d.end_address_id
        LEFT JOIN geofences sg ON sg.id = d.start_geofence_id
        LEFT JOIN geofences eg ON eg.id = d.end_geofence_id
        WHERE d.car_id = $1 AND d.distance > 0.1
        ORDER BY d.start_date DESC LIMIT 20
        """,
        car_id,
    )
    return {
        "monthly": [
            {
                "month": r["month"].isoformat(),
                "km": round(float(r["km"] or 0), 1),
                "hours": round(float(r["minutes"] or 0) / 60, 1),
                "n": r["n"],
                "wh_km": round(float(r["rated_used"] or 0) * efficiency / float(r["km"]) * 1000, 1)
                if r["km"] and float(r["km"]) > 3 and efficiency else None,
            }
            for r in monthly
        ],
        "by_temp": [
            {
                "temp": float(r["temp_bucket"]),
                "wh_km": round(float(r["range_ratio"] or 0) * efficiency * 1000, 1) if efficiency else None,
                "n": r["n"],
            }
            for r in by_temp
        ],
        "recent": [
            {
                "start": _iso(r["start_date"]),
                "from": r["from_loc"],
                "to": r["to_loc"],
                "km": round(float(r["distance"]), 1),
                "duration_min": r["duration_min"],
                "speed_max": r["speed_max"],
                "power_max": r["power_max"],
                "ascent": r["ascent"],
                "outside_temp": float(r["outside_temp_avg"]) if r["outside_temp_avg"] is not None else None,
                "wh_km": round(float(r["rated_used"] or 0) * efficiency / float(r["distance"]) * 1000, 1)
                if float(r["distance"]) > 1 and efficiency else None,
            }
            for r in recent
        ],
    }


async def drives_list(car_id: int, limit: int = 50) -> list[dict]:
    rows = await db.fetch(
        """
        SELECT d.id, d.start_date, d.end_date, d.distance, d.duration_min,
               d.speed_max, d.power_max,
               COALESCE(sg.name, sa.city, '') AS from_loc,
               COALESCE(eg.name, ea.city, '') AS to_loc
        FROM drives d
        LEFT JOIN addresses sa ON sa.id = d.start_address_id
        LEFT JOIN addresses ea ON ea.id = d.end_address_id
        LEFT JOIN geofences sg ON sg.id = d.start_geofence_id
        LEFT JOIN geofences eg ON eg.id = d.end_geofence_id
        WHERE d.car_id = $1 AND d.distance > 0.1
        ORDER BY d.start_date DESC LIMIT $2
        """,
        car_id, max(1, min(limit, 200)),
    )
    return [
        {
            "id": r["id"],
            "start": _iso(r["start_date"]),
            "end": _iso(r["end_date"]),
            "km": round(float(r["distance"]), 1),
            "duration_min": r["duration_min"],
            "speed_max": r["speed_max"],
            "from": r["from_loc"],
            "to": r["to_loc"],
        }
        for r in rows
    ]


async def drive_tracks(car_id: int, limit: int = 20, max_points: int = 140) -> list[dict]:
    """最近若干段行程的轨迹，一次取回并抽稀。

    行程页落地时地图上要有东西可看。逐段拉 20 次接口只为画一层背景轨迹
    太浪费，而且首屏会闪；这里一次给完，每段抽到 max_points 以内。
    """
    rows = await db.fetch(
        """
        SELECT d.id,
               array_agg(p.latitude  ORDER BY p.date) AS lats,
               array_agg(p.longitude ORDER BY p.date) AS lons
        FROM (SELECT id FROM drives
              WHERE car_id = $1 AND distance > 0.1
              ORDER BY start_date DESC LIMIT $2) d
        JOIN positions p ON p.drive_id = d.id
        WHERE p.latitude IS NOT NULL
        GROUP BY d.id
        """,
        car_id, limit,
    )
    out = []
    for r in rows:
        lats, lons = r["lats"], r["lons"]
        if not lats or len(lats) < 2:
            continue
        stride = max(1, len(lats) // max_points)
        pts = [[float(la), float(lo)] for la, lo in zip(lats[::stride], lons[::stride])]
        # 抽稀不能把终点丢了，否则轨迹看着像半途断掉
        last = [float(lats[-1]), float(lons[-1])]
        if pts[-1] != last:
            pts.append(last)
        out.append({"id": r["id"], "points": pts})
    return out


async def drive_track(drive_id: int) -> list[dict]:
    rows = await db.fetch(
        """
        SELECT date, latitude, longitude, speed, power, battery_level, elevation
        FROM positions WHERE drive_id = $1 ORDER BY date
        """,
        drive_id,
    )
    stride = max(1, len(rows) // 1500)
    return [
        {
            "t": _iso(r["date"]),
            "lat": float(r["latitude"]),
            "lon": float(r["longitude"]),
            "speed": r["speed"],
            "power": r["power"],
            "soc": r["battery_level"],
            "elev": r["elevation"],
        }
        for r in rows[::stride]
        if r["latitude"] is not None
    ]


async def drive_row(drive_id: int):
    return await db.fetchrow(
        "SELECT id, car_id, start_date, end_date FROM drives WHERE id = $1", drive_id
    )


async def tpms(car_id: int) -> dict:
    latest = await db.fetchrow(
        """
        SELECT date, tpms_pressure_fl, tpms_pressure_fr, tpms_pressure_rl, tpms_pressure_rr
        FROM positions
        WHERE car_id = $1 AND tpms_pressure_fl IS NOT NULL
        ORDER BY date DESC LIMIT 1
        """,
        car_id,
    )
    history = await db.fetch(
        """
        SELECT date_trunc('day', date)::date AS day,
               AVG(tpms_pressure_fl) AS fl, AVG(tpms_pressure_fr) AS fr,
               AVG(tpms_pressure_rl) AS rl, AVG(tpms_pressure_rr) AS rr
        FROM positions
        WHERE car_id = $1 AND date > now() - interval '30 days'
          AND tpms_pressure_fl IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        car_id,
    )
    fmt = lambda v: round(float(v), 2) if v is not None else None  # noqa: E731
    return {
        "latest": {
            "date": _iso(latest["date"]),
            "fl": fmt(latest["tpms_pressure_fl"]),
            "fr": fmt(latest["tpms_pressure_fr"]),
            "rl": fmt(latest["tpms_pressure_rl"]),
            "rr": fmt(latest["tpms_pressure_rr"]),
        }
        if latest else None,
        "history": [
            {"day": r["day"].isoformat(), "fl": fmt(r["fl"]), "fr": fmt(r["fr"]),
             "rl": fmt(r["rl"]), "rr": fmt(r["rr"])}
            for r in history
        ],
    }


async def updates(car_id: int) -> list[dict]:
    rows = await db.fetch(
        "SELECT start_date, version FROM updates WHERE car_id = $1 ORDER BY start_date DESC LIMIT 50",
        car_id,
    )
    return [{"date": _iso(r["start_date"]), "version": r["version"]} for r in rows]


async def records(car_id: int) -> dict:
    eff_row = await db.fetchrow("SELECT efficiency FROM cars WHERE id = $1", car_id)
    efficiency = float(eff_row["efficiency"] or 0) if eff_row else 0

    top_speed = await db.fetchrow(
        "SELECT speed_max, start_date FROM drives WHERE car_id = $1"
        " ORDER BY speed_max DESC NULLS LAST LIMIT 1", car_id)
    top_power = await db.fetchrow(
        "SELECT power_max, start_date FROM drives WHERE car_id = $1"
        " ORDER BY power_max DESC NULLS LAST LIMIT 1", car_id)
    top_regen = await db.fetchrow(
        "SELECT power_min, start_date FROM drives WHERE car_id = $1"
        " ORDER BY power_min ASC NULLS LAST LIMIT 1", car_id)
    longest = await db.fetchrow(
        "SELECT distance, duration_min, start_date FROM drives WHERE car_id = $1"
        " ORDER BY distance DESC NULLS LAST LIMIT 1", car_id)
    max_elev = await db.fetchrow(
        "SELECT elevation, date FROM positions WHERE car_id = $1"
        " ORDER BY elevation DESC NULLS LAST LIMIT 1", car_id)
    busiest_day = await db.fetchrow(
        """
        SELECT start_date::date AS day, SUM(distance) AS km
        FROM drives WHERE car_id = $1 AND distance > 0 GROUP BY 1 ORDER BY 2 DESC LIMIT 1
        """, car_id)
    biggest_charge = await db.fetchrow(
        "SELECT charge_energy_added, start_date FROM charging_processes"
        " WHERE car_id = $1 ORDER BY charge_energy_added DESC NULLS LAST LIMIT 1", car_id)
    best_eff = await db.fetchrow(
        """
        SELECT start_date, distance,
               GREATEST(start_rated_range_km - end_rated_range_km, 0) / NULLIF(distance, 0) AS ratio
        FROM drives
        WHERE car_id = $1 AND distance > 20
        ORDER BY 3 ASC NULLS LAST LIMIT 1
        """, car_id)

    def rec(row, key, fmt=None, date_key="start_date"):
        if not row or row[key] is None:
            return None
        v = float(row[key])
        d = row.get(date_key) if hasattr(row, "get") else row[date_key]
        return {
            "value": fmt(v) if fmt else v,
            "date": _iso(d) if hasattr(d, "hour") else (d.isoformat() if d else None),
        }

    return {
        "top_speed_kmh": rec(top_speed, "speed_max"),
        "top_power_kw": rec(top_power, "power_max"),
        "max_regen_kw": rec(top_regen, "power_min"),
        "longest_drive_km": rec(longest, "distance", lambda v: round(v, 1)),
        "max_elevation_m": rec(max_elev, "elevation", date_key="date"),
        "busiest_day_km": {
            "value": round(float(busiest_day["km"]), 1),
            "date": busiest_day["day"].isoformat(),
        } if busiest_day and busiest_day["km"] else None,
        "biggest_charge_kwh": rec(biggest_charge, "charge_energy_added", lambda v: round(v, 1)),
        "best_efficiency_wh_km": {
            "value": round(float(best_eff["ratio"]) * efficiency * 1000, 1),
            "date": _iso(best_eff["start_date"]),
            "km": round(float(best_eff["distance"]), 1),
        } if best_eff and best_eff["ratio"] is not None and efficiency else None,
    }
