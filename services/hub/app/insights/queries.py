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
from ..alerts.store import store
from ..livestate import live


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
    rated_used = float(drive["rated_km_used"] or 0)
    kwh_driven = rated_used * efficiency
    total_cost = float(charge["cost"]) + computed_cost

    # 汽油对比。基准由用户在设置里填（每百公里油耗 + 油价），没填就不显示 ——
    # 拍一个"假设 8L/100km"出来是在替用户编数据。
    ice = await store.get_setting("ice_baseline", {}) or {}
    savings = None
    try:
        l100 = float(ice.get("l_per_100km") or 0)
        fuel_price = float(ice.get("fuel_price") or 0)
        if l100 > 0 and fuel_price > 0 and km > 0:
            fuel_cost = km / 100 * l100 * fuel_price
            savings = {
                "fuel_cost": round(fuel_cost, 2),
                "electric_cost": round(total_cost, 2),
                "saved": round(fuel_cost - total_cost, 2),
                "l_per_100km": l100,
                "fuel_price": fuel_price,
            }
    except (TypeError, ValueError):
        savings = None

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
        "charge_cost": round(total_cost, 2),
        "currency": plan.get("currency") or await _currency(),
        "cost_per_km": round(total_cost / km, 3) if km > 10 else None,
        # 实际里程 ÷ 消耗掉的额定续航。100% 表示开出了标称的水平，
        # 低于 100% 说明比标称费电（高速、低温、载重都会拉低它）。
        "range_efficiency_pct": round(km / rated_used * 100, 1) if rated_used > 1 else None,
        "gas_savings": savings,
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

    # 出厂容量可由用户覆盖。默认拿"观测到的最大值"当基准，对**装本工具之前
    # 就开了几年的车**是偏袒的：那几年的衰减全被算进基准里，于是显示 0% 衰减。
    # 允许手填新车续航/容量，衰减才有意义。
    override = (await store.get_setting("battery_baseline", {}) or {}).get(str(car_id)) or {}
    baseline_source = "observed"
    if override.get("range_km"):
        baseline = float(override["range_km"])
        baseline_source = "user"
    elif override.get("kwh") and efficiency:
        baseline = float(override["kwh"]) / efficiency
        baseline_source = "user"

    degradation = (
        round((1 - current / baseline) * 100, 1)
        if current and baseline and baseline > 0
        else None
    )

    # 等效满充循环数：把一堆零碎的补电折算成"充满多少次"。
    # 用 ΔSOC 累加而不是 kWh，这样不依赖对电池容量的估计。
    cyc = await db.fetchrow(
        "SELECT COALESCE(SUM(GREATEST(end_battery_level - start_battery_level, 0)), 0) / 100.0"
        " AS cycles, COALESCE(SUM(charge_energy_added), 0) AS kwh"
        " FROM charging_processes WHERE car_id = $1",
        car_id,
    )

    # 日历老化：停放时段按 SOC 分箱累计小时数。高 SOC 静置是公认的衰减来源，
    # 这是"造成衰减的原因"，而不是"衰减发生之后的结果"。
    soc_hours = await db.fetch(
        """
        WITH s AS (
            SELECT usable_battery_level AS soc, date,
                   LEAD(date) OVER (ORDER BY date) AS next_date
            FROM positions
            WHERE car_id = $1 AND usable_battery_level IS NOT NULL
              AND date >= now() - INTERVAL '180 days'
        )
        SELECT CASE WHEN soc >= 90 THEN '90-100'
                    WHEN soc >= 80 THEN '80-90'
                    WHEN soc >= 60 THEN '60-80'
                    WHEN soc >= 30 THEN '30-60'
                    ELSE '0-30' END AS bucket,
               SUM(EXTRACT(EPOCH FROM (next_date - date)) / 3600.0) AS hours
        FROM s
        -- 采样间隔超过 1 小时的多半是车在休眠、中间没有数据，
        -- 直接按整段计入会把一次长休眠算成"一直停在这个电量"
        WHERE next_date IS NOT NULL AND next_date - date < INTERVAL '1 hour'
        GROUP BY 1 ORDER BY 1
        """,
        car_id,
    )
    buckets = {r["bucket"]: round(float(r["hours"] or 0), 1) for r in soc_hours}
    total_h = sum(buckets.values())
    high_h = buckets.get("80-90", 0) + buckets.get("90-100", 0)
    # 车休眠时采样稀疏，这些小时数只覆盖了窗口里的一部分。把覆盖率一并给出来，
    # 界面才能如实说"这是基于 N% 的时间算的"，而不是让人以为是全时段统计。
    span_row = await db.fetchrow(
        "SELECT EXTRACT(EPOCH FROM (MAX(date) - MIN(date))) / 3600.0 AS h"
        " FROM positions WHERE car_id = $1 AND date >= now() - INTERVAL '180 days'",
        car_id,
    )
    span_h = float(span_row["h"] or 0) if span_row else 0

    return {
        "efficiency_kwh_km": efficiency,
        "lfp_battery": bool(car["lfp_battery"]) if car else False,
        "energy_based": e_series,
        "range_based": r_series,
        "current_range_at_100": round(current, 1) if current else None,
        "max_range_at_100": round(baseline, 1) if baseline else None,
        "baseline_source": baseline_source,
        "degradation_pct": degradation,
        "current_capacity_kwh": round(current * efficiency, 1) if current and efficiency else None,
        "equivalent_full_cycles": round(float(cyc["cycles"] or 0), 1) if cyc else None,
        "lifetime_kwh_added": round(float(cyc["kwh"] or 0), 1) if cyc else None,
        "soc_hours": buckets,
        # 静置在 80% 以上的时间占比。LFP 电池不适用这条经验（官方建议常充满），
        # 界面据此决定要不要提这件事。
        "high_soc_pct": round(high_h / total_h * 100, 1) if total_h > 0 else None,
        "soc_hours_total": round(total_h, 1),
        "soc_coverage_pct": round(total_h / span_h * 100, 1) if span_h > 1 else None,
    }


async def period_compare(car_id: int, days: int = 30) -> dict:
    """本期 vs 上一个等长周期。

    单独一个数字（"这个月开了 400 公里"）是没有信息量的 —— 有没有变、变了多少，
    才是人真正想知道的。所以每个指标都配一个同比。
    """
    d = max(int(days), 1)
    rows = await db.fetch(
        """
        WITH w AS (
            SELECT d.distance,
                   GREATEST(d.start_rated_range_km - d.end_rated_range_km, 0) AS rated_used,
                   CASE WHEN d.start_date >= now() - ($2::int * INTERVAL '1 day')
                        THEN 'cur' ELSE 'prev' END AS bucket
            FROM drives d
            WHERE d.car_id = $1 AND d.distance > 0.1
              AND d.start_date >= now() - (2 * $2::int * INTERVAL '1 day')
        )
        SELECT bucket, COUNT(*) AS drives,
               COALESCE(SUM(distance), 0) AS km,
               COALESCE(SUM(rated_used), 0) AS rated_used
        FROM w GROUP BY bucket
        """,
        car_id, d,
    )
    eff_row = await db.fetchrow("SELECT efficiency FROM cars WHERE id = $1", car_id)
    efficiency = float(eff_row["efficiency"] or 0) if eff_row else 0

    # 费用要连电价引擎算出来的一起算。绝大多数人的 charging_processes.cost
    # 是空的（回写默认关着），只 SUM(cost) 的话这一栏永远是 0，
    # 而同一页别处显示的费用又不是 0 —— 自相矛盾。
    from ..tariff.calc import costs_map

    computed = await costs_map()
    ch_rows = await db.fetch(
        """
        SELECT id,
               CASE WHEN start_date >= now() - ($2::int * INTERVAL '1 day')
                    THEN 'cur' ELSE 'prev' END AS bucket,
               charge_energy_added AS kwh, cost
        FROM charging_processes
        WHERE car_id = $1 AND charge_energy_added > 0.1
          AND start_date >= now() - (2 * $2::int * INTERVAL '1 day')
        """,
        car_id, d,
    )
    ch_agg: dict[str, dict] = {}
    for r in ch_rows:
        b = ch_agg.setdefault(r["bucket"], {"bucket": r["bucket"], "kwh": 0.0, "cost": 0.0})
        b["kwh"] += float(r["kwh"] or 0)
        b["cost"] += (float(r["cost"]) if r["cost"] is not None
                      else float(computed.get(str(r["id"]), 0.0)))
    ch = list(ch_agg.values())

    def pick(src, bucket, field):
        for r in src:
            if r["bucket"] == bucket:
                return float(r[field] or 0)
        return 0.0

    out = {}
    for bucket in ("cur", "prev"):
        km = pick(rows, bucket, "km")
        used = pick(rows, bucket, "rated_used")
        out[bucket] = {
            "drives": int(pick(rows, bucket, "drives")),
            "km": round(km, 1),
            "kwh": round(used * efficiency, 1),
            "wh_per_km": round(used * efficiency * 1000 / km, 1) if km > 3 else None,
            "charged_kwh": round(pick(ch, bucket, "kwh"), 1),
            "cost": round(pick(ch, bucket, "cost"), 2),
        }

    def delta(field):
        a, b = out["cur"].get(field), out["prev"].get(field)
        if a is None or b is None or not b:
            return None
        return round((a - b) / b * 100, 1)

    return {
        "days": d,
        "current": out["cur"],
        "previous": out["prev"],
        "change_pct": {f: delta(f) for f in
                       ("km", "kwh", "wh_per_km", "charged_kwh", "cost", "drives")},
    }


async def real_world_range(car_id: int, window_km: float = 160.0) -> dict:
    """按你自己最近的真实能耗算续航，而不是 EPA 标称。

    做法：从最近的行程往前累加，凑够 window_km 为止，用这段的实际能耗
    去折算当前电量还能跑多远。
    """
    rows = await db.fetch(
        """
        SELECT d.distance,
               GREATEST(d.start_rated_range_km - d.end_rated_range_km, 0) AS rated_used,
               c.efficiency
        FROM drives d JOIN cars c ON c.id = d.car_id
        WHERE d.car_id = $1 AND d.distance > 0.5
        ORDER BY d.start_date DESC LIMIT 400
        """,
        car_id,
    )
    km = kwh = 0.0
    used = 0
    for r in rows:
        km += float(r["distance"] or 0)
        kwh += float(r["rated_used"] or 0) * float(r["efficiency"] or 0)
        used += 1
        if km >= window_km:
            break
    if km < 5 or kwh <= 0:
        return {"wh_per_km": None, "range_km": None, "window_km": round(km, 1), "drives": used}

    wh_km = kwh * 1000 / km
    live_car = live.cars.get(str(car_id), {})
    try:
        soc = float(live_car.get("usable_battery_level") or live_car.get("battery_level") or 0)
    except (TypeError, ValueError):
        soc = 0.0
    cap = await battery_health(car_id)
    pack = cap.get("current_capacity_kwh") or 0
    return {
        "wh_per_km": round(wh_km, 1),
        "window_km": round(km, 1),
        "drives": used,
        "soc": soc or None,
        # 当前电量 ÷ 你自己的真实能耗
        "range_km": round(pack * (soc / 100) / (wh_km / 1000), 1) if (pack and soc) else None,
    }


def _since(days: int | None):
    """时间窗过滤片段。days 为 None/0 表示不限（"全部"）。

    返回 (SQL 片段, 参数列表)，调用方把参数接在 $1 后面。
    """
    if not days or days <= 0:
        return "", []
    return " AND {col} >= now() - ($2::int * INTERVAL '1 day')", [int(days)]


async def charging(car_id: int, days: int | None = None) -> dict:
    """充电统计。费用优先用 TeslaMate 里手填的 cost，为空则用电价方案算出的值。"""
    from ..tariff.calc import costs_map, get_plan

    computed = await costs_map()
    plan = await get_plan()
    currency = plan.get("currency") or await _currency()

    def cost_of(cp_id, db_cost) -> float:
        if db_cost is not None:
            return float(db_cost)
        return float(computed.get(str(cp_id), 0.0))

    win, win_args = _since(days)
    monthly_rows = await db.fetch(
        f"""
        SELECT cp.id, date_trunc('month', cp.start_date)::date AS month,
               cp.charge_energy_added AS kwh, cp.cost,
               COALESCE(fc.fast, false) AS is_dc
        FROM charging_processes cp
        LEFT JOIN LATERAL (
            SELECT bool_or(fast_charger_present) AS fast
            FROM charges WHERE charging_process_id = cp.id
        ) fc ON TRUE
        WHERE cp.car_id = $1 AND cp.charge_energy_added > 0.1
        {win.format(col="cp.start_date")}
        ORDER BY 2
        """,  # noqa: S608 - win 是本文件的固定片段，days 仍走绑定参数
        car_id, *win_args,
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
        f"""
        -- 地点取不到时返回 NULL 而不是中文字面量：这个值会原样渲染到界面上，
        -- 英文用户会看到一串中文。文案由前端按 locale 补。
        SELECT cp.id, COALESCE(g.name, a.city, a.display_name) AS location,
               cp.charge_energy_added AS kwh, cp.cost
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        WHERE cp.car_id = $1 AND cp.charge_energy_added > 0.1
        {win.format(col="cp.start_date")}
        """,  # noqa: S608
        car_id, *win_args,
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
        f"""
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
        {win.format(col="cp.start_date")}
        ORDER BY cp.start_date DESC LIMIT 20
        """,  # noqa: S608
        car_id, *win_args,
    )
    return {
        "currency": currency,
        # 这是个按车的接口，就不能报全局那张表的大小 —— 多车账号下
        # 另一辆车算过费用会让这辆车看起来"已经配好电价了"。
        "computed_sessions": sum(1 for r in loc_rows if str(r["id"]) in computed),
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


async def drives(car_id: int, days: int | None = None) -> dict:
    eff_row = await db.fetchrow("SELECT efficiency FROM cars WHERE id = $1", car_id)
    efficiency = float(eff_row["efficiency"] or 0) if eff_row else 0

    win, win_args = _since(days)
    monthly = await db.fetch(
        f"""
        SELECT date_trunc('month', start_date)::date AS month,
               SUM(distance) AS km,
               SUM(duration_min) AS minutes,
               COUNT(*) AS n,
               SUM(GREATEST(start_rated_range_km - end_rated_range_km, 0)) AS rated_used
        FROM drives WHERE car_id = $1 AND distance > 0.1
        {win.format(col="start_date")}
        GROUP BY 1 ORDER BY 1
        """,  # noqa: S608
        car_id, *win_args,
    )
    by_temp = await db.fetch(
        f"""
        SELECT round(outside_temp_avg / 3) * 3 AS temp_bucket,
               SUM(GREATEST(start_rated_range_km - end_rated_range_km, 0)) / NULLIF(SUM(distance), 0) AS range_ratio,
               COUNT(*) AS n
        FROM drives
        WHERE car_id = $1 AND distance > 3 AND outside_temp_avg IS NOT NULL
        {win.format(col="start_date")}
        GROUP BY 1 HAVING COUNT(*) >= 3 ORDER BY 1
        """,  # noqa: S608
        car_id, *win_args,
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
        pts = [[float(la), float(lo)] for la, lo in zip(lats[::stride], lons[::stride], strict=False)]
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


async def vampire_drain(car_id: int, days: int = 90, min_idle_h: float = 6.0) -> dict:
    """停放耗电（俗称"吸血鬼耗电"）。

    做法：取相邻两段行程，若中间**没有充电**，那么上一段结束时的电量减去
    下一段开始时的电量，就是这段停放期间掉的电。这样算的好处是只依赖
    drives + charging_processes 两张表，不用去猜 states 里那些
    online/asleep/suspended 的语义。

    只统计停放 1 小时以上、掉电为正、且不超过 40% 的区间：太短的噪声大，
    掉电为负是采样抖动，超过 40% 基本是中途拔了充电枪之类的异常。
    """
    # drives 表本身不存电量，只有额定续航；SOC 要顺着 start/end_position_id
    # 回 positions 拿。额定续航差 × efficiency 就是掉了多少度电 ——
    # 这也是 TeslaMate 自己算能耗的口径。
    #
    # SOC 取 battery_level 而不是 usable_battery_level，两个原因：
    # ① 流式插入的行不带 usable_battery_level（实测本机 26571 条 positions 里
    #    24544 条为空，而 battery_level 一条都不空），而行程**起点**几乎总是
    #    流式行 —— 用 usable 的话 29 个停放区间只有 3 个两端齐全，下面那道
    #    "掉电 >40% 判为异常"的过滤等于从来没生效过；
    # ② usable_battery_level 会随电池变冷而下降，那是可用量变了、不是电跑了。
    #    量停放掉电要的是真实 SOC，battery_level 才是对的口径。
    rows = await db.fetch(
        """
        SELECT d.end_date, d.end_rated_range_km AS end_km_range,
               pe.battery_level AS end_soc,
               d.end_km AS end_odo,
               LEAD(d.start_date)           OVER w AS next_start,
               LEAD(d.start_rated_range_km) OVER w AS next_km_range,
               LEAD(ps.battery_level) OVER w AS next_soc,
               LEAD(d.start_km) OVER w AS next_odo
        FROM drives d
        LEFT JOIN positions pe ON pe.id = d.end_position_id
        LEFT JOIN positions ps ON ps.id = d.start_position_id
        WHERE d.car_id = $1 AND d.end_date >= now() - ($2::int * INTERVAL '1 day')
          AND d.end_rated_range_km IS NOT NULL
        WINDOW w AS (ORDER BY d.start_date)
        ORDER BY d.start_date
        """,
        car_id, int(max(days, 1)),
    )
    charges_in = await db.fetch(
        "SELECT start_date, end_date FROM charging_processes"
        " WHERE car_id = $1 AND start_date >= now() - ($2::int * INTERVAL '1 day')",
        car_id, int(max(days, 1)),
    )
    # 折成 kWh/天要知道电池容量。用「满电等效额定续航 × cars.efficiency」推，
    # 与 battery_health 的额定续航法同源 —— 不要拍一个"大概 75 度"的数，
    # 那是在给用户看一个编出来的值。
    cap_row = await db.fetchrow(
        """
        SELECT c.efficiency,
               MAX(p.rated_battery_range_km / NULLIF(p.usable_battery_level, 0) * 100) AS range_100
        FROM cars c LEFT JOIN positions p
          ON p.car_id = c.id AND p.usable_battery_level >= 40
         AND p.rated_battery_range_km IS NOT NULL
        WHERE c.id = $1 GROUP BY c.efficiency
        """,
        car_id,
    )
    efficiency = float(cap_row["efficiency"] or 0) if cap_row else 0
    range_100 = float(cap_row["range_100"]) if cap_row and cap_row["range_100"] else 0
    pack_kwh = round(range_100 * efficiency, 1) if (range_100 and efficiency) else 0

    def charged_between(a, b) -> bool:
        return any(c["start_date"] and a <= c["start_date"] <= b for c in charges_in)

    items, total_km, total_h = [], 0.0, 0.0
    for r in rows:
        if r["next_start"] is None or r["next_km_range"] is None:
            continue
        hours = (r["next_start"] - r["end_date"]).total_seconds() / 3600
        km_drop = float(r["end_km_range"]) - float(r["next_km_range"])
        pct_drop = (
            float(r["end_soc"]) - float(r["next_soc"])
            if r["end_soc"] is not None and r["next_soc"] is not None else None
        )
        # 太短的区间噪声大；掉电为负是采样抖动；掉太多基本是中途拔了枪之类的异常。
        #
        # 门槛取 6 小时是跟上游对齐：TeslaMate 自己的 vampire-drain 面板把
        # "min. Idle Time (h)" 默认设成 6（可选 0/1/3/6/12/18/24）。原来这里
        # 写的是 1 小时，导致大量"刚停下的一两个小时"被计入——那段电池还热、
        # 空调可能还在跑、哨兵刚布防，掉电速率远高于长期停放，外推成全天
        # 会把每日耗电抬高好几倍（本机实测 1h 门槛下算出 14.4 kWh/天）。
        if hours < min_idle_h or km_drop <= 0 or (pct_drop is not None and pct_drop > 40):
            continue
        # 里程表在这段"停放"里走了 —— 那是 TeslaMate 没记上的行车，不是停放耗电。
        # 实测本机 10 个候选区间里有 3 个分别走了 19 / 494 / 272 公里，
        # 不排掉的话每日耗电会被抬高好几倍（8.86 → 实际 1.x kWh/天）。
        # pct>40% 那道闸只拦得住最夸张的两段，19 公里那段会混进来。
        if (r["end_odo"] is not None and r["next_odo"] is not None
                and r["next_odo"] - r["end_odo"] > 0.5):
            continue
        if charged_between(r["end_date"], r["next_start"]):
            continue
        items.append({
            "from": _iso(r["end_date"]),
            "hours": round(hours, 1),
            "km_lost": round(km_drop, 1),
            "pct": round(pct_drop, 1) if pct_drop is not None else None,
            "kwh_per_day": round(km_drop * efficiency / hours * 24, 2) if efficiency else None,
        })
        total_km += km_drop
        total_h += hours

    kwh_day = (
        round(total_km * efficiency / total_h * 24, 2) if (total_h and efficiency) else None
    )
    return {
        "sessions": len(items),
        # 回显**实际用的**天数，不是用户传进来的。传 -5 时内部夹到 1，
        # 却在响应里回 -5，等于告诉调用方"我按 -5 天算的"。
        "days": max(int(days), 1),
        "min_idle_h": min_idle_h,
        # 额定续航差 × efficiency 直接就是度数，不需要先知道电池容量
        "avg_kwh_per_day": kwh_day,
        "avg_km_per_day": round(total_km / total_h * 24, 1) if total_h else None,
        # 百分比由同一份度数换算而来，而不是另起一套 SOC 采样去算。
        # 两套算法样本量不同（SOC 两端经常缺值），并排显示会给出两个互相矛盾
        # 的数字，而用户没有办法判断该信哪个。
        "avg_pct_per_day": (
            round(kwh_day / pack_kwh * 100, 2) if (kwh_day and pack_kwh) else None
        ),
        "pack_kwh_est": pack_kwh or None,
        # 界面只用最近 30 条；导出要全量，所以两个都给
        "recent": items[-30:],
        "all": items,
    }


async def charge_curve(cp_id: int) -> dict:
    """单次充电的功率-电量曲线。DC 快充的充电曲线是所有记录器的标配图表。"""
    head = await db.fetchrow(
        """
        SELECT cp.id, cp.car_id, cp.start_date, cp.end_date, cp.charge_energy_added,
               cp.start_battery_level, cp.end_battery_level, cp.duration_min, cp.cost,
               COALESCE(g.name, a.display_name, '') AS location
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        WHERE cp.id = $1
        """,
        cp_id,
    )
    if head is None:
        return {}
    rows = await db.fetch(
        "SELECT date, battery_level, usable_battery_level, charger_power,"
        " charger_voltage, charger_actual_current, charger_phases,"
        " charge_energy_added, outside_temp, fast_charger_present, battery_heater"
        " FROM charges WHERE charging_process_id = $1 ORDER BY date",
        cp_id,
    )
    t0 = rows[0]["date"] if rows else None
    # 峰值必须在抽稀**之前**算：抽稀会跳着取点，真正的峰值那一点很可能被跳过，
    # 于是"峰值 250kW"变成"峰值 180kW" —— 而这正是用户最关心的数字。
    peak_kw = max((float(r["charger_power"] or 0) for r in rows), default=0)
    is_dc = any(r["fast_charger_present"] for r in rows)

    # 一次慢充能有一两千个采样点，整份丢给浏览器画曲线没有意义。
    # 抽稀到 ~400 点，但**保留最后一个**，否则曲线看着像没充完就断了。
    MAX_POINTS = 400
    if len(rows) > MAX_POINTS:
        stride = len(rows) // MAX_POINTS + 1
        kept = rows[::stride]
        if kept[-1] is not rows[-1]:
            kept.append(rows[-1])
        rows = kept
    return {
        "id": head["id"],
        "start": _iso(head["start_date"]),
        "end": _iso(head["end_date"]),
        "location": head["location"] or None,
        "kwh": round(float(head["charge_energy_added"] or 0), 2),
        "duration_min": head["duration_min"],
        "soc": f"{head['start_battery_level']}→{head['end_battery_level']}",
        "cost": float(head["cost"]) if head["cost"] is not None else None,
        "points": [
            {
                "t": round((r["date"] - t0).total_seconds() / 60, 2) if t0 else 0,
                "soc": r["usable_battery_level"] if r["usable_battery_level"] is not None
                       else r["battery_level"],
                "kw": float(r["charger_power"] or 0),
                "volts": r["charger_voltage"],
                "amps": r["charger_actual_current"],
                "phases": r["charger_phases"],
                "kwh": float(r["charge_energy_added"] or 0),
                "temp": float(r["outside_temp"]) if r["outside_temp"] is not None else None,
                "heater": bool(r["battery_heater"]),
            }
            for r in rows
        ],
        "peak_kw": peak_kw,
        "is_dc": is_dc,
    }


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
