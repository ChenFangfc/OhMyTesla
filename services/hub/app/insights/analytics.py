"""与 TeslaMate v4.4.0 的 Grafana 面板对齐的分析查询（JSON，公制单位）。

每个函数都注明它对齐的是哪个面板、哪条 SQL（research/teslamate.md §5 与附录 A）。
口径约定：
- 单位一律公制：km、km/h、kWh、Wh/km、°C、bar、m；前端负责换算。
- 时间一律带 +00:00 的 ISO；"本地日期"按 period.local_tz_name() 切。
- 能耗 = 首选续航（settings.preferred_range，默认 rated）的差 × cars.efficiency，
  与 TeslaMate 完全同一口径（它没有实测行车电量）。
- 进行中 / 孤儿记录（end_date IS NULL）不报 500：结束时间取最后一个采样点。
- positions.drive_id 只有 BRIN 索引：按行程取点一律写成
  `drive_id = $1 AND date BETWEEN 起 AND 止`，让 BRIN(drive_id, date) 两个键都用上；
  按时间段取完整行一律带 `ideal_battery_range_km IS NOT NULL`，命中部分 btree 索引。
"""

import itertools
import logging
import math
import statistics as _stats
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from .. import db
from . import cars as car_registry
from . import costs as cost_source
from .period import EARLIEST, Period, iso, local_tz_name, utcnow

log = logging.getLogger(__name__)

_pr_cache: tuple[float, str] | None = None


async def preferred_range() -> str:
    """'rated' 或 'ideal'（TeslaMate 设置页里的"首选续航"）。cars.efficiency 就是按它推的。"""
    global _pr_cache
    import time

    if _pr_cache and time.monotonic() - _pr_cache[0] < 60:
        return _pr_cache[1]
    try:
        v = await db.fetchval("SELECT preferred_range::text FROM settings LIMIT 1")
    except db.DbUnavailable:
        raise
    except Exception:  # noqa: BLE001 - settings 表缺失就按 TeslaMate 默认值
        v = None
    pr = "ideal" if v == "ideal" else "rated"
    _pr_cache = (time.monotonic(), pr)
    return pr


async def car_meta(car_id: int) -> dict:
    c = await car_registry.get_car(int(car_id))
    if c is None:
        raise HTTPException(404, f"car {car_id} not found")
    eff = float(c["efficiency"]) if c.get("efficiency") else None
    return {
        "id": c["id"], "label": c["label"], "name": c.get("name"), "model": c.get("model_line"),
        "efficiency_kwh_per_km": eff, "mass_kg": car_registry.mass_kg(c.get("model_line")),
        "lfp_battery": bool(c.get("lfp_battery")), "archived": c["archived"],
        "last_seen": c["last_seen"],
    }


def _f(v, nd: int | None = None):
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd) if nd is not None else x


def _div(a, b, nd: int | None = None, scale: float = 1.0):
    a, b = _f(a), _f(b)
    if a is None or not b:
        return None
    return _f(a / b * scale, nd)


def _place_sql(g: str, a: str) -> str:
    """TeslaMate 4.3+ 的地点标签：围栏名 > 地址名 > 路名门牌，再接 COALESCE(city, neighbourhood)。"""
    return (
        f"COALESCE({g}.name, NULLIF(CONCAT_WS(', ',"
        f" COALESCE(NULLIF({a}.name, ''), NULLIF(CONCAT_WS(' ', {a}.road, {a}.house_number), '')),"
        f" COALESCE({a}.city, {a}.neighbourhood)), ''), {a}.display_name)"
    )


def _range_cols(pr: str) -> tuple[str, str, str]:
    """(drives/charging_processes 的起点列, 终点列, positions/charges 的瞬时列)。"""
    assert pr in ("rated", "ideal")
    return f"start_{pr}_range_km", f"end_{pr}_range_km", f"{pr}_battery_range_km"


def _downsample(n: int, max_points: int, keep: set[int] | None = None) -> list[int]:
    """均匀抽稀到 max_points 以内，保留首尾和 keep 里的下标（峰值点）。"""
    if n <= max_points:
        return list(range(n))
    stride = n / max_points
    idx = {int(i * stride) for i in range(max_points)}
    idx.update({0, n - 1})
    if keep:
        idx.update(i for i in keep if 0 <= i < n)
    return sorted(idx)


def _encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    """Google encoded polyline（lat, lon），MapLibre/Leaflet 都有现成的解码器。"""
    factor = 10 ** precision
    out = []
    plat = plon = 0
    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for d in (ilat - plat, ilon - plon):
            v = ~(d << 1) if d < 0 else d << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def _simplify(pts: list[tuple[float, float]], max_points: int) -> list[tuple[float, float]]:
    """去掉几乎重合的点（停车时 GPS 抖动），再均匀抽稀。终点一定保留。"""
    if not pts:
        return []
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) > 2e-5 or abs(p[1] - out[-1][1]) > 2e-5:
            out.append(p)
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    idx = _downsample(len(out), max_points)
    return [out[i] for i in idx]


def _kwh_potential(mass: float, meters: float) -> float:
    return mass * 9.81 * meters / 3600 / 1000


# ======================================================================== 行程

async def drive_detail(drive_id: int, max_points: int = 2000) -> dict:
    """单次行程完整档案 —— 对齐 Drive Details 面板（internal/drive-details.json）。

    速度/功率/海拔/SOC/车外温度的时间序列 + 路线，净能耗与额定能耗之比、
    动能回收（Energy recovered）、坡度修正效率（TeslaMate 硬编码 2100 kg，
    这里按车型取质量，Cybertruck 3100 kg）、速度分布。
    """
    pr = await preferred_range()
    s_col, e_col, p_col = _range_cols(pr)
    head = await db.fetchrow(
        f"""
        SELECT d.id, d.car_id, d.start_date, d.end_date, d.distance, d.duration_min,
               d.start_km, d.end_km, d.speed_max, d.power_max, d.power_min,
               d.outside_temp_avg, d.inside_temp_avg, d.ascent, d.descent,
               d.{s_col} AS start_range, d.{e_col} AS end_range,
               ps.battery_level AS s_bl, ps.usable_battery_level AS s_ubl,
               pe.battery_level AS e_bl, pe.usable_battery_level AS e_ubl,
               {_place_sql('sg', 'sa')} AS from_label, {_place_sql('eg', 'ea')} AS to_label,
               sg.name AS from_geofence, eg.name AS to_geofence,
               sa.display_name AS from_address, ea.display_name AS to_address,
               COALESCE(sa.city, sa.neighbourhood) AS from_city,
               COALESCE(ea.city, ea.neighbourhood) AS to_city
        FROM drives d
        LEFT JOIN positions ps ON ps.id = d.start_position_id
        LEFT JOIN positions pe ON pe.id = d.end_position_id
        LEFT JOIN geofences sg ON sg.id = d.start_geofence_id
        LEFT JOIN geofences eg ON eg.id = d.end_geofence_id
        LEFT JOIN addresses sa ON sa.id = d.start_address_id
        LEFT JOIN addresses ea ON ea.id = d.end_address_id
        WHERE d.id = $1
        """,  # noqa: S608 - 只插入白名单列名与模块常量
        drive_id,
    )
    if head is None:
        raise HTTPException(404, "drive not found")
    car = await car_meta(head["car_id"])
    eff = car["efficiency_kwh_per_km"]
    # 进行中 / 孤儿行程没有 end_date：时间窗开到"现在"，结束时刻取最后一个点
    upper = head["end_date"] or (utcnow() + timedelta(minutes=5))
    rows = await db.fetch(
        f"""
        SELECT date, latitude, longitude, speed, power, battery_level, usable_battery_level,
               elevation, outside_temp, inside_temp, odometer, {p_col} AS range_km,
               est_battery_range_km, ideal_battery_range_km IS NOT NULL AS full_row
        FROM positions
        WHERE drive_id = $1 AND date >= $2 AND date <= $3
        ORDER BY date
        """,  # noqa: S608
        drive_id, head["start_date"], upper,
    )
    open_ = head["end_date"] is None
    t0 = rows[0]["date"] if rows else head["start_date"]
    t1 = head["end_date"] or (rows[-1]["date"] if rows else head["start_date"])
    duration_min = head["duration_min"]
    if duration_min is None:
        duration_min = round((t1 - t0).total_seconds() / 60)
    odo = [r["odometer"] for r in rows if r["odometer"] is not None]
    distance = _f(head["distance"])
    if distance is None and len(odo) >= 2:
        distance = max(odo) - min(odo)
    start_range = _f(head["start_range"])
    end_range = _f(head["end_range"])
    full = [r for r in rows if r["full_row"] and r["range_km"] is not None]
    if start_range is None and full:
        start_range = _f(full[0]["range_km"])
    if end_range is None and full:
        end_range = _f(full[-1]["range_km"])
    range_used = (start_range - end_range) if (start_range is not None and end_range is not None) else None

    # 动能回收：TeslaMate drive-details "Energy recovered"——只看流式行（≈1 Hz）里
    # 负功率的相邻点，间隔 < 1.5 s 才计（需要 Streaming API）
    regen = 0.0
    prev_neg = None
    for r in rows:
        p = r["power"]
        if p is None or p >= 0:
            continue
        if prev_neg is not None:
            dt = (r["date"] - prev_neg).total_seconds()
            if dt < 1.5:
                regen += -p * dt / 3600
        prev_neg = r["date"]

    # 速度分布：每个点到下一个点的时长计入它的 10 km/h 档（drive-details 同款）
    hist: dict[int, float] = {}
    moving_s = 0.0
    for a, b in itertools.pairwise(rows):
        if a["speed"] is None or a["speed"] <= 0:
            continue
        dt = (b["date"] - a["date"]).total_seconds()
        if dt <= 0 or dt > 120:
            continue
        k = round(a["speed"] / 10.0) * 10
        if k <= 0:
            continue
        hist[k] = hist.get(k, 0.0) + dt
        moving_s += dt

    ascent = head["ascent"]
    descent = head["descent"]
    elev = [r["elevation"] for r in rows if r["elevation"] is not None]
    net_kwh = range_used * eff if (range_used is not None and eff) else None
    mass = car["mass_kg"]
    flat_kwh = None
    slope_pct = None
    if net_kwh is not None and ascent is not None and descent is not None:
        flat_kwh = net_kwh + 0.85 * _kwh_potential(mass, descent) - _kwh_potential(mass, ascent)
        if flat_kwh and distance:
            slope_pct = _f(distance * eff / flat_kwh * 100, 1)

    # 冷电池：完整行里超过 25% 的点 battery_level > usable_battery_level（drives.json）
    reduced = None
    if full:
        cold = sum(1 for r in full if r["usable_battery_level"] is not None
                   and r["battery_level"] is not None and r["battery_level"] > r["usable_battery_level"])
        reduced = cold > 0.25 * len(full)

    keep = set()
    if rows:
        sp = [(r["speed"] or -1, i) for i, r in enumerate(rows)]
        pw = [(r["power"] if r["power"] is not None else 0, i) for i, r in enumerate(rows)]
        keep = {max(sp)[1], max(pw)[1], min(pw)[1]}
    idx = _downsample(len(rows), max(50, min(int(max_points), 10000)), keep)
    sel = [rows[i] for i in idx]
    pts = [(float(r["latitude"]), float(r["longitude"])) for r in rows]
    route = _simplify(pts, 1500)

    def col(key, nd=None):
        return [_f(r[key], nd) for r in sel]

    speeds = [r["speed"] for r in rows if r["speed"] is not None and r["speed"] > 0]
    return {
        "car": car,
        "preferred_range": pr,
        "drive": {
            "id": head["id"], "car_id": head["car_id"], "open": open_,
            "start": iso(head["start_date"]), "end": iso(head["end_date"]),
            "last_point": iso(rows[-1]["date"]) if rows else None,
            "duration_min": duration_min,
            "distance_km": _f(distance, 2),
            "odometer_start_km": _f(head["start_km"] if head["start_km"] is not None else (min(odo) if odo else None), 1),
            "odometer_end_km": _f(head["end_km"] if head["end_km"] is not None else (max(odo) if odo else None), 1),
            "from": {"label": head["from_label"], "geofence": head["from_geofence"],
                     "address": head["from_address"], "city": head["from_city"],
                     "lat": _f(rows[0]["latitude"]) if rows else None,
                     "lon": _f(rows[0]["longitude"]) if rows else None},
            "to": {"label": head["to_label"], "geofence": head["to_geofence"],
                   "address": head["to_address"], "city": head["to_city"],
                   "lat": _f(rows[-1]["latitude"]) if rows else None,
                   "lon": _f(rows[-1]["longitude"]) if rows else None},
            "speed_max_kmh": head["speed_max"] if head["speed_max"] is not None else (max(speeds) if speeds else None),
            "speed_avg_kmh": _div(distance, duration_min, 1, 60) if duration_min else None,
            "speed_avg_moving_kmh": _f(sum(speeds) / len(speeds), 1) if speeds else None,
            "power_max_kw": head["power_max"], "power_min_kw": head["power_min"],
            "ascent_m": ascent, "descent_m": descent,
            "elevation_min_m": min(elev) if elev else None,
            "elevation_max_m": max(elev) if elev else None,
            "outside_temp_avg_c": _f(head["outside_temp_avg"], 1),
            "inside_temp_avg_c": _f(head["inside_temp_avg"], 1),
            "start_soc": head["s_ubl"] if head["s_ubl"] is not None else head["s_bl"],
            "end_soc": head["e_ubl"] if head["e_ubl"] is not None else head["e_bl"],
            "start_range_km": _f(start_range, 1), "end_range_km": _f(end_range, 1),
            "range_used_km": _f(range_used, 2),
            "reduced_range": reduced,
            "points": len(rows),
        },
        "energy": {
            "efficiency_kwh_per_km": eff,
            "rated_wh_per_km": _f(eff * 1000, 1) if eff else None,
            "net_kwh": _f(net_kwh, 2),
            "wh_per_km": _div(net_kwh, distance, 1, 1000) if (distance or 0) > 0.1 else None,
            # 100% = 开出了标称的里程；低于 100% 说明比额定费电
            "efficiency_vs_rated_pct": _div(distance, range_used, 1, 100)
            if (range_used or 0) > 0.1 else None,
            "regen_kwh": _f(regen, 2),
            "regen_share_pct": _div(regen, (net_kwh or 0) + regen, 1, 100)
            if net_kwh is not None and (net_kwh + regen) > 0 else None,
            "mass_kg": mass,
            "slope_adjusted_kwh": _f(flat_kwh, 2),
            "slope_adjusted_efficiency_pct": slope_pct,
        },
        "speed_histogram": [
            {"speed_kmh": k, "seconds": round(v), "pct": _f(v / moving_s * 100, 1) if moving_s else None}
            for k, v in sorted(hist.items())
        ],
        "series": {
            "t": [iso(r["date"]) for r in sel],
            "speed_kmh": col("speed"),
            "power_kw": col("power"),
            "soc": [r["battery_level"] for r in sel],
            "usable_soc": [r["usable_battery_level"] for r in sel],
            "elevation_m": col("elevation"),
            "outside_temp_c": col("outside_temp", 1),
            "inside_temp_c": col("inside_temp", 1),
            "range_km": col("range_km", 1),
            "odometer_km": col("odometer", 3),
            "lat": col("latitude", 6),
            "lon": col("longitude", 6),
        },
        "route": {
            "type": "LineString",
            "coordinates": [[lon, lat] for lat, lon in route],
            "encoded": _encode_polyline(route),
            "bbox": [min(p[1] for p in pts), min(p[0] for p in pts),
                     max(p[1] for p in pts), max(p[0] for p in pts)] if pts else None,
        },
    }


async def drives_table(car_id: int, period: Period, limit: int = 100, offset: int = 0,
                       min_distance_km: float = 0.0) -> dict:
    """行程表 —— 对齐 Drives 面板（drives.json，三种效率口径）+ Drive Stats 的汇总。"""
    pr = await preferred_range()
    s_col, e_col, _ = _range_cols(pr)
    car = await car_meta(car_id)
    eff = car["efficiency_kwh_per_km"]
    lo, hi = period.bounds()
    rows = await db.fetch(
        f"""
        SELECT d.id, d.start_date, d.end_date, d.distance, d.duration_min, d.start_km, d.end_km,
               d.speed_max, d.power_max, d.power_min, d.outside_temp_avg, d.inside_temp_avg,
               d.ascent, d.descent, d.{s_col} - d.{e_col} AS range_diff,
               ps.battery_level AS s_bl, ps.usable_battery_level AS s_ubl,
               pe.battery_level AS e_bl, pe.usable_battery_level AS e_ubl,
               {_place_sql('sg', 'sa')} AS from_label, {_place_sql('eg', 'ea')} AS to_label,
               count(*) OVER () AS total
        FROM drives d
        LEFT JOIN positions ps ON ps.id = d.start_position_id
        LEFT JOIN positions pe ON pe.id = d.end_position_id
        LEFT JOIN geofences sg ON sg.id = d.start_geofence_id
        LEFT JOIN geofences eg ON eg.id = d.end_geofence_id
        LEFT JOIN addresses sa ON sa.id = d.start_address_id
        LEFT JOIN addresses ea ON ea.id = d.end_address_id
        WHERE d.car_id = $1 AND d.start_date >= $2 AND d.start_date < $3
          AND (d.distance IS NULL OR d.distance >= $4)
        ORDER BY d.start_date DESC
        LIMIT $5 OFFSET $6
        """,  # noqa: S608
        car_id, lo, hi, float(min_distance_km), int(limit), int(offset),
    )
    agg = await db.fetchrow(
        f"""
        SELECT count(*) AS n, count(*) FILTER (WHERE end_date IS NULL) AS open,
               COALESCE(sum(distance), 0) AS km,
               COALESCE(sum(duration_min), 0) AS minutes,
               sum({s_col} - {e_col}) FILTER (WHERE distance IS NOT NULL
                                             AND {s_col} - {e_col} >= 0.1) AS range_net,
               sum(distance) FILTER (WHERE distance IS NOT NULL
                                     AND {s_col} - {e_col} >= 0.1) AS km_net,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY distance) AS median_km,
               max(speed_max) AS speed_max, min(start_date) AS first, max(start_date) AS last
        FROM drives
        WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
          AND (distance IS NULL OR distance >= $4)
        """,  # noqa: S608
        car_id, lo, hi, float(min_distance_km),
    )
    top = await db.fetch(
        f"""
        SELECT {_place_sql('g', 'a')} AS label, count(*) AS n
        FROM drives d
        LEFT JOIN geofences g ON g.id = d.end_geofence_id
        LEFT JOIN addresses a ON a.id = d.end_address_id
        WHERE d.car_id = $1 AND d.start_date >= $2 AND d.start_date < $3 AND d.end_date IS NOT NULL
        GROUP BY 1 HAVING {_place_sql('g', 'a')} IS NOT NULL ORDER BY 2 DESC LIMIT 10
        """,  # noqa: S608
        car_id, lo, hi,
    )
    mass = car["mass_kg"]
    items = []
    for r in rows:
        dist = _f(r["distance"])
        rd = _f(r["range_diff"])
        kwh = rd * eff if (rd is not None and eff) else None
        flat = None
        if kwh is not None and r["ascent"] is not None and r["descent"] is not None:
            flat = kwh + 0.85 * _kwh_potential(mass, r["descent"]) - _kwh_potential(mass, r["ascent"])
        items.append({
            "id": r["id"], "start": iso(r["start_date"]), "end": iso(r["end_date"]),
            "open": r["end_date"] is None,
            "from": r["from_label"], "to": r["to_label"],
            "distance_km": _f(dist, 2), "duration_min": r["duration_min"],
            "odometer_start_km": _f(r["start_km"], 1), "odometer_end_km": _f(r["end_km"], 1),
            "speed_max_kmh": r["speed_max"],
            "speed_avg_kmh": _div(dist, r["duration_min"], 1, 60),
            "power_max_kw": r["power_max"], "power_min_kw": r["power_min"],
            "outside_temp_avg_c": _f(r["outside_temp_avg"], 1),
            "inside_temp_avg_c": _f(r["inside_temp_avg"], 1),
            "ascent_m": r["ascent"], "descent_m": r["descent"],
            "start_soc": r["s_ubl"] if r["s_ubl"] is not None else r["s_bl"],
            "end_soc": r["e_ubl"] if r["e_ubl"] is not None else r["e_bl"],
            "range_used_km": _f(rd, 2),
            "net_kwh": _f(kwh, 2),
            "wh_per_km": _div(kwh, dist, 1, 1000) if (dist or 0) > 0.1 and kwh is not None else None,
            "efficiency_vs_rated_pct": _div(dist, rd, 1, 100) if (rd or 0) > 0 else None,
            "slope_adjusted_efficiency_pct": _div((dist or 0) * (eff or 0), flat, 1, 100)
            if flat and dist and eff else None,
            "reduced_range": bool(r["s_ubl"] is not None and r["s_bl"] is not None
                                  and r["s_bl"] > r["s_ubl"]),
        })
    n = int(agg["n"] or 0)
    km = _f(agg["km"]) or 0.0
    span_days = None
    if agg["first"] is not None:
        end = period.end or utcnow()
        start = period.start or agg["first"]
        span_days = max((end - start).total_seconds() / 86400, 1.0)
    net_kwh = (_f(agg["range_net"]) or 0) * eff if eff else None
    return {
        "car": car, "period": period.as_dict(), "preferred_range": pr,
        "total": int(rows[0]["total"]) if rows else n,
        "limit": limit, "offset": offset,
        "drives": items,
        "summary": {
            "drives": n, "open": int(agg["open"] or 0),
            "distance_km": round(km, 1),
            "duration_min": int(agg["minutes"] or 0),
            "net_kwh": _f(net_kwh, 1),
            "wh_per_km": _div(net_kwh, agg["km_net"], 1, 1000),
            "median_distance_km": _f(agg["median_km"], 1),
            "speed_max_kmh": agg["speed_max"],
            "km_per_day": _div(km, span_days, 1) if span_days else None,
            "kwh_per_day": _div(net_kwh, span_days, 2) if span_days and net_kwh is not None else None,
            # Drive Stats 的"外推月/年里程"：按窗口内日均里程外推
            "extrapolated_monthly_km": _f(km / span_days * 365 / 12, 0) if span_days else None,
            "extrapolated_annual_km": _f(km / span_days * 365, 0) if span_days else None,
            "top_destinations": [{"label": t["label"], "arrivals": t["n"]} for t in top],
        },
    }


async def speed_histogram(car_id: int, period: Period) -> list[dict]:
    """整段时间的速度分布（Drive Stats 面板）。按时间占比，10 km/h 一档。"""
    lo, hi = period.bounds()
    rows = await db.fetch(
        """
        WITH p AS (
            SELECT p.speed, EXTRACT(EPOCH FROM (lead(p.date) OVER w - p.date)) AS dt
            FROM positions p
            WHERE p.car_id = $1 AND p.date >= $2 AND p.date < $3 AND p.drive_id IS NOT NULL
            WINDOW w AS (PARTITION BY p.drive_id ORDER BY p.date)
        )
        SELECT (round(speed / 10.0) * 10)::int AS bucket, sum(dt) AS secs
        FROM p WHERE speed >= 5 AND dt > 0 AND dt < 120
        GROUP BY 1 ORDER BY 1
        """,
        car_id, lo, hi, timeout_s=25,
    )
    tot = sum(float(r["secs"] or 0) for r in rows)
    return [{"speed_kmh": r["bucket"], "seconds": round(float(r["secs"])),
             "pct": _f(float(r["secs"]) / tot * 100, 1) if tot else None} for r in rows]


# ======================================================================== 充电

def _phases_from(rows: list) -> float | None:
    """TeslaMate 的 $determine_phases（charge-details.json / log.ex:590-630）。"""
    ps, rs = [], []
    for r in rows:
        i, v, p = r["charger_actual_current"], r["charger_voltage"], r["charger_power"]
        if i and v and p is not None:
            ps.append(p * 1000.0 / (int(i) * int(v)))
        if r["charger_phases"] is not None:
            rs.append(r["charger_phases"])
    if not ps or len(rows) <= 15:
        return None
    p = sum(ps) / len(ps)
    if p <= 0:
        return None
    r_ = round(sum(rs) / len(rs)) if rs else None
    if r_ is not None and r_ == round(p):
        return float(r_)
    if r_ == 3 and abs(p / math.sqrt(3) - 1) <= 0.1:
        return math.sqrt(3)
    if round(p) > 0 and abs(round(p) - p) <= 0.3:
        return float(round(p))
    return None


async def charge_detail(cp_id: int, max_points: int = 800) -> dict:
    """单次充电完整档案 —— 对齐 Charge Details 面板（internal/charge-details.json）。

    kW-SOC 曲线（逐点 + 按 SOC 平均）、电压/电流/相数（直流时隐藏"交流输入"电压电流，
    4.4.0 同款）、交流/直流、墙端 vs 电池端电量与充电效率、有效费用（唯一真源）。
    """
    pr = await preferred_range()
    s_col, e_col, p_col = _range_cols(pr)
    head = await db.fetchrow(
        f"""
        SELECT cp.id, cp.car_id, cp.start_date, cp.end_date, cp.duration_min,
               cp.charge_energy_added, cp.charge_energy_used, cp.start_battery_level,
               cp.end_battery_level, cp.{s_col} AS start_range, cp.{e_col} AS end_range,
               cp.outside_temp_avg, cp.cost,
               {_place_sql('g', 'a')} AS label, g.name AS geofence, a.display_name AS address,
               COALESCE(a.city, a.neighbourhood) AS city,
               p.latitude, p.longitude, p.odometer
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        LEFT JOIN positions p ON p.id = cp.position_id
        WHERE cp.id = $1
        """,  # noqa: S608
        cp_id,
    )
    if head is None:
        raise HTTPException(404, "charging session not found")
    car = await car_meta(head["car_id"])
    rows = await db.fetch(
        f"""
        SELECT date, battery_level, usable_battery_level, charger_power, charger_voltage,
               charger_actual_current, charger_pilot_current, charger_phases, charge_energy_added,
               outside_temp, fast_charger_present, fast_charger_brand, fast_charger_type,
               conn_charge_cable, battery_heater, battery_heater_on, {p_col} AS range_km
        FROM charges WHERE charging_process_id = $1 ORDER BY date
        """,  # noqa: S608
        cp_id,
    )
    open_ = head["end_date"] is None
    is_dc = any(r["fast_charger_present"] for r in rows)
    phases = None if is_dc else _phases_from(rows)

    def mode(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return max(set(vals), key=vals.count) if vals else None

    # 墙端功率：TeslaMate 积分 charge_energy_used 的同一公式
    def wall_kw(r):
        if r["charger_phases"] is None or is_dc:
            return _f(r["charger_power"])
        if phases and r["charger_actual_current"] is not None and r["charger_voltage"] is not None:
            return phases * int(r["charger_actual_current"]) * int(r["charger_voltage"]) / 1000.0
        return _f(r["charger_power"])

    t0 = rows[0]["date"] if rows else head["start_date"]
    t_end = head["end_date"] or (rows[-1]["date"] if rows else head["start_date"])
    duration_min = head["duration_min"]
    if duration_min is None:
        duration_min = round((t_end - t0).total_seconds() / 60)
    added = _f(head["charge_energy_added"])
    if added is None and rows:     # 进行中：取当前计数（与 charge-details 的 c_added 一致）
        vals = [_f(r["charge_energy_added"]) for r in rows if r["charge_energy_added"] is not None]
        added = (vals[-1] - vals[0]) if vals else None
    used = _f(head["charge_energy_used"])
    if used is None and len(rows) >= 2:
        e = 0.0
        for a, b in itertools.pairwise(rows):
            w = wall_kw(b)
            dt = (b["date"] - a["date"]).total_seconds()
            if w is not None and w >= 0 and dt > 0:
                e += w * dt / 3600
        used = e
    billed = max([v for v in (added, used) if v is not None], default=None)

    peak = max((_f(r["charger_power"]) or 0 for r in rows), default=None) if rows else None
    by_soc: dict[int, list[float]] = {}
    for r in rows:
        if (r["charger_power"] or 0) > 0 and r["battery_level"] is not None:
            by_soc.setdefault(int(r["battery_level"]), []).append(float(r["charger_power"]))

    costs = await cost_source.session_costs(head["car_id"], autoprice=not open_)
    ci = costs.get(cp_id) or {"cost": None, "source": "unset"}
    breakdown = None
    if ci.get("source") == "computed":
        from ..tariff import calc

        plan = await calc.get_plan()
        prev = await calc.compute_session(cp_id, plan, calc.plan_tz(plan))
        breakdown = prev.get("breakdown") if prev else None

    keep = set()
    if rows:
        keep = {max((r["charger_power"] or 0, i) for i, r in enumerate(rows))[1]}
    idx = _downsample(len(rows), max(50, min(int(max_points), 5000)), keep)
    sel = [rows[i] for i in idx]
    ac_only = (lambda v: None if is_dc else v)   # noqa: E731 - 4.4.0：直流时不显示"交流输入"
    return {
        "car": car,
        "preferred_range": pr,
        "charge": {
            "id": head["id"], "car_id": head["car_id"], "open": open_,
            "start": iso(head["start_date"]), "end": iso(head["end_date"]),
            "last_sample": iso(rows[-1]["date"]) if rows else None,
            "duration_min": duration_min,
            "type": "DC" if is_dc else "AC",
            "ac_phases": _f(phases, 3),
            "charger_brand": mode("fast_charger_brand"),
            "charger_type": mode("fast_charger_type"),
            "connector": mode("conn_charge_cable"),
            "location": {"label": head["label"], "geofence": head["geofence"],
                         "address": head["address"], "city": head["city"],
                         "lat": _f(head["latitude"]), "lon": _f(head["longitude"])},
            "odometer_km": _f(head["odometer"], 1),
            "start_soc": head["start_battery_level"] if head["start_battery_level"] is not None
            else (rows[0]["battery_level"] if rows else None),
            "end_soc": head["end_battery_level"] if head["end_battery_level"] is not None
            else (rows[-1]["battery_level"] if rows else None),
            "start_range_km": _f(head["start_range"], 1), "end_range_km": _f(head["end_range"], 1),
            "range_added_km": _f((_f(head["end_range"]) or 0) - (_f(head["start_range"]) or 0), 1)
            if head["start_range"] is not None and head["end_range"] is not None else None,
            "outside_temp_avg_c": _f(head["outside_temp_avg"], 1),
            "peak_kw": peak,
            "avg_kw": _div(added, duration_min, 1, 60) if duration_min else None,
            "energy": {
                "added_kwh": _f(added, 2),          # 进电池
                "used_kwh": _f(used, 2),            # 从墙上（TeslaMate 积分）
                "billed_kwh": _f(billed, 2),        # max(两者) —— TeslaMate 计费口径
                "losses_kwh": _f(max((used or 0) - (added or 0), 0), 2) if used is not None and added is not None else None,
                # charge-details "Efficiency"：added / used，封顶 100%
                "efficiency_pct": _f(min(added / used, 1.0) * 100, 1) if (added is not None and used) else None,
            },
            "cost": {**ci, "currency": await cost_source.currency(), "breakdown": breakdown},
        },
        "curve": [{"soc": s, "kw": _f(sum(v) / len(v), 1), "samples": len(v)}
                  for s, v in sorted(by_soc.items())],
        "series": {
            "t": [iso(r["date"]) for r in sel],
            "minutes": [_f((r["date"] - t0).total_seconds() / 60, 2) for r in sel],
            "soc": [r["battery_level"] for r in sel],
            "usable_soc": [r["usable_battery_level"] for r in sel],
            "kw": [_f(r["charger_power"]) for r in sel],
            "kw_wall": [_f(wall_kw(r), 2) for r in sel],
            "voltage": [ac_only(r["charger_voltage"]) for r in sel],
            "current": [ac_only(r["charger_actual_current"]) for r in sel],
            "pilot_current": [r["charger_pilot_current"] for r in sel],
            "phases": [r["charger_phases"] for r in sel],
            "kwh_added": [_f(r["charge_energy_added"], 2) for r in sel],
            "range_km": [_f(r["range_km"], 1) for r in sel],
            "outside_temp_c": [_f(r["outside_temp"], 1) for r in sel],
            "battery_heater": [bool(r["battery_heater_on"] or r["battery_heater"]) for r in sel],
        },
    }


_CP_KIND_SQL = """
LEFT JOIN LATERAL (
    SELECT bool_or(fast_charger_present) AS fast, max(charger_power) AS peak,
           mode() WITHIN GROUP (ORDER BY fast_charger_brand) AS brand
    FROM charges WHERE charging_process_id = cp.id
) fc ON TRUE
"""


async def charges_table(car_id: int, period: Period, limit: int = 100, offset: int = 0) -> dict:
    """充电表 —— 对齐 Charges 面板（charges.json）。进行中的场次排在最前、标 open。"""
    pr = await preferred_range()
    s_col, e_col, _ = _range_cols(pr)
    car = await car_meta(car_id)
    lo, hi = period.bounds()
    rows = await db.fetch(
        f"""
        SELECT cp.id, cp.start_date, cp.end_date, cp.duration_min,
               cp.charge_energy_added, cp.charge_energy_used,
               COALESCE(cp.start_battery_level,
                        (SELECT battery_level FROM charges c WHERE c.charging_process_id = cp.id
                         ORDER BY date LIMIT 1)) AS start_soc,
               cp.end_battery_level, cp.{e_col} - cp.{s_col} AS range_added,
               cp.outside_temp_avg, {_place_sql('g', 'a')} AS label,
               COALESCE(fc.fast, false) AS is_dc, fc.peak, fc.brand,
               count(*) OVER () AS total
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        {_CP_KIND_SQL}
        WHERE cp.car_id = $1 AND cp.start_date >= $2 AND cp.start_date < $3
        ORDER BY cp.start_date DESC
        LIMIT $4 OFFSET $5
        """,  # noqa: S608
        car_id, lo, hi, int(limit), int(offset),
    )
    costs = await cost_source.session_costs(car_id)
    items = []
    for r in rows:
        added, used = _f(r["charge_energy_added"]), _f(r["charge_energy_used"])
        billed = max([v for v in (added, used) if v is not None], default=None)
        ci = costs.get(r["id"]) or {}
        items.append({
            "id": r["id"], "start": iso(r["start_date"]), "end": iso(r["end_date"]),
            "open": r["end_date"] is None, "duration_min": r["duration_min"],
            "type": "DC" if r["is_dc"] else "AC", "charger_brand": r["brand"],
            "location": r["label"],
            "added_kwh": _f(added, 2), "used_kwh": _f(used, 2), "billed_kwh": _f(billed, 2),
            "efficiency_pct": _f(min(added / used, 1.0) * 100, 1) if (added is not None and used) else None,
            "start_soc": r["start_soc"], "end_soc": r["end_battery_level"],
            "range_added_km": _f(r["range_added"], 1),
            "outside_temp_avg_c": _f(r["outside_temp_avg"], 1),
            "peak_kw": r["peak"],
            "avg_kw": _div(added, r["duration_min"], 1, 60),
            "cost": ci.get("cost"), "cost_source": ci.get("source", "unset"),
            "cost_per_kwh": _div(ci.get("cost"), billed, 3) if billed else None,
        })
    return {"car": car, "period": period.as_dict(), "currency": await cost_source.currency(),
            "total": int(rows[0]["total"]) if rows else 0, "limit": limit, "offset": offset,
            "charges": items}


async def charging_stats(car_id: int, period: Period) -> dict:
    """充电统计 —— 对齐 Charging Stats 面板（charging-stats.json）。

    次数/电量/费用、超充费用（4.4.0：按整场采样的众数判直流 + 地址/围栏/品牌认超充）、
    每 100 km 成本（毛能耗 × 每 kWh 成本）、交直流占比、常用充电站、直流充电曲线
    （跨场次每个 SOC 的中位功率）、学到的定时充电起止时刻。
    """
    car = await car_meta(car_id)
    lo, hi = period.bounds()
    rows = await db.fetch(
        f"""
        SELECT cp.id, cp.start_date, cp.end_date, cp.duration_min,
               cp.charge_energy_added::float8 AS added, cp.charge_energy_used::float8 AS used,
               {_place_sql('g', 'a')} AS label, a.name AS addr_name, g.name AS geofence,
               COALESCE(fc.fast, false) AS is_dc, fc.peak, fc.brand
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        {_CP_KIND_SQL}
        WHERE cp.car_id = $1 AND cp.end_date IS NOT NULL
          AND cp.end_date >= $2 AND cp.end_date < $3
        """,  # noqa: S608
        car_id, lo, hi,
    )
    costs = await cost_source.session_costs(car_id)
    from ..tariff.calc import is_tesla_supercharger

    tot = {"charges": 0, "added_kwh": 0.0, "used_kwh": 0.0, "cost": 0.0, "duration_min": 0,
           "suc_cost": 0.0}
    split = {k: {"sessions": 0, "added_kwh": 0.0, "used_kwh": 0.0, "duration_min": 0,
                 "cost": 0.0, "priced_kwh": 0.0} for k in ("AC", "DC")}
    stations: dict[str, dict] = {}
    for r in rows:
        added, used = r["added"] or 0.0, r["used"]
        billed = max([v for v in (r["added"], used) if v is not None], default=0.0)
        kind = "DC" if r["is_dc"] else "AC"
        c = (costs.get(r["id"]) or {}).get("cost")
        tot["charges"] += 1
        tot["added_kwh"] += added
        tot["used_kwh"] += billed
        tot["duration_min"] += r["duration_min"] or 0
        s = split[kind]
        s["sessions"] += 1
        s["added_kwh"] += added
        s["used_kwh"] += billed
        s["duration_min"] += r["duration_min"] or 0
        if c is not None:
            tot["cost"] += c
            s["cost"] += c
            s["priced_kwh"] += billed
            if is_tesla_supercharger({"is_dc": r["is_dc"], "charger_brand": r["brand"],
                                      "geofence": r["geofence"], "addr_name": r["addr_name"]}):
                tot["suc_cost"] += c
        st = stations.setdefault(r["label"] or "?", {"location": r["label"], "sessions": 0,
                                                      "added_kwh": 0.0, "cost": 0.0, "type": kind})
        st["sessions"] += 1
        st["added_kwh"] += added
        st["cost"] += c or 0.0

    gross = await _gross(car_id, period, (await preferred_range()))
    cost_per_kwh_added = _div(tot["cost"], tot["added_kwh"])
    dc_curve = await db.fetch(
        """
        SELECT c.battery_level AS soc,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY c.charger_power) AS kw,
               count(*) AS n
        FROM charging_processes cp JOIN charges c ON c.charging_process_id = cp.id
        WHERE cp.car_id = $1 AND cp.end_date >= $2 AND cp.end_date < $3
          AND c.fast_charger_present AND c.charger_power > 0
        GROUP BY 1 ORDER BY 1
        """,
        car_id, lo, hi,
    )
    from .charging_stops import learned_schedule

    def fin(d):
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}

    return {
        "car": car, "period": period.as_dict(), "currency": await cost_source.currency(),
        "totals": {
            **fin(tot),
            "efficiency_pct": _div(tot["added_kwh"], tot["used_kwh"], 1, 100),
            "cost_per_kwh": _div(tot["cost"], tot["used_kwh"], 3),
            # charging-stats "Ø Cost per 100 km" = 毛能耗 × (Σ费用/Σ电池端电量)
            "cost_per_100km": _f(gross["wh_per_km"] / 1000 * 100 * cost_per_kwh_added, 2)
            if gross.get("wh_per_km") and cost_per_kwh_added else None,
            "gross_wh_per_km": gross.get("wh_per_km"),
        },
        "ac_dc": {k: {**fin(v), "cost_per_kwh": _div(v["cost"], v["priced_kwh"], 3)}
                  for k, v in split.items()},
        "top_stations_by_energy": [fin(v) for v in sorted(stations.values(), key=lambda x: -x["added_kwh"])[:10]],
        "top_stations_by_cost": [fin(v) for v in sorted(stations.values(), key=lambda x: -x["cost"])[:10]
                                 if v["cost"] > 0],
        "dc_curve": [{"soc": r["soc"], "kw": _f(r["kw"], 1), "samples": r["n"]} for r in dc_curve],
        "schedule": await learned_schedule(car_id),
    }


# ======================================================================== 能耗

async def _events(car_id: int, lo: datetime, hi: datetime, pr: str) -> list[dict]:
    """毛能耗用的事件序列：行程起止 + 充电起止（statistics/efficiency 共享的那条查询）。"""
    s_col, e_col, _ = _range_cols(pr)
    rows = await db.fetch(
        f"""
        SELECT 'drive_start' AS event, start_date AS date, {s_col} AS range, start_km AS odo,
               distance IS NULL AS incomplete
        FROM drives WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
        UNION ALL
        SELECT 'drive_end', COALESCE(end_date, start_date + interval '1 second'), {e_col}, end_km,
               distance IS NULL
        FROM drives WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
        UNION ALL
        SELECT 'charge_start', cp.start_date, cp.{s_col}, p.odometer, cp.end_date IS NULL
        FROM charging_processes cp JOIN positions p ON p.id = cp.position_id
        WHERE cp.car_id = $1 AND cp.start_date >= $2 AND cp.start_date < $3
        UNION ALL
        SELECT 'charge_end', COALESCE(cp.end_date, cp.start_date + interval '1 second'),
               cp.{e_col}, p.odometer, cp.end_date IS NULL
        FROM charging_processes cp JOIN positions p ON p.id = cp.position_id
        WHERE cp.car_id = $1 AND cp.start_date >= $2 AND cp.start_date < $3
        ORDER BY 2
        """,  # noqa: S608
        car_id, lo, hi,
    )
    return [dict(r) for r in rows]


def _gross_from_events(ev: list[dict]) -> list[tuple[datetime, float, float]]:
    """[(段起点时间, 距离, 续航损失)]。行驶段计带符号的损失，停放/充电段只计损失（≥0）。"""
    out = []
    for a, b in itertools.pairwise(ev):
        if a["incomplete"]:
            out.append((a["date"], 0.0, 0.0))
            continue
        if a["range"] is None or b["range"] is None:
            continue
        dist = (b["odo"] - a["odo"]) if (a["odo"] is not None and b["odo"] is not None) else 0.0
        loss = float(a["range"]) - float(b["range"])
        if a["event"] != "drive_start":
            loss = max(loss, 0.0)
        out.append((a["date"], float(dist), loss))
    return out


async def _gross(car_id: int, period: Period, pr: str) -> dict:
    eff = (await car_meta(car_id))["efficiency_kwh_per_km"]
    lo, hi = period.bounds()
    segs = _gross_from_events(await _events(car_id, lo, hi, pr))
    km = sum(s[1] for s in segs)
    loss = sum(s[2] for s in segs)
    kwh = loss * eff if eff else None
    return {"kwh": _f(kwh, 1), "km": _f(km, 1),
            "wh_per_km": _div(kwh, km, 1, 1000) if km > 1 and kwh is not None else None}


async def efficiency(car_id: int, period: Period, min_distance_km: float = 1.0) -> dict:
    """能耗 —— 对齐 Efficiency 面板（efficiency.json），并补上按速度分档。

    - 净能耗：行驶中损失的续航 × efficiency ÷ 距离（drives 里 range_diff ≥ 0.1 的行程）
    - 毛能耗：含停放/哨兵/空调的全部损失 ÷ 距离（共享的事件查询）；overhead = 1 - 净/毛
    - 按车外温度 5 °C 一档（Σ距离/Σ续航差、Σ续航差/Σ距离×efficiency）
    - 按速度 10 km/h 一档：把每段行程按里程切成 2 km 的小段，用完整位置行的续航差
      算每段的能耗（TeslaLogger「Speed Consumption」的做法），小段按平均速度分档
    """
    pr = await preferred_range()
    s_col, e_col, p_col = _range_cols(pr)
    car = await car_meta(car_id)
    eff = car["efficiency_kwh_per_km"]
    lo, hi = period.bounds()
    net = await db.fetchrow(
        f"""
        SELECT sum({s_col} - {e_col}) AS rd, sum(distance) AS km, count(*) AS n
        FROM drives
        WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
          AND distance IS NOT NULL AND {s_col} - {e_col} >= 0.1
        """,  # noqa: S608
        car_id, lo, hi,
    )
    net_kwh = (_f(net["rd"]) or 0) * eff if (eff and net["rd"] is not None) else None
    gross = await _gross(car_id, period, pr)
    by_temp = await db.fetch(
        f"""
        SELECT round(outside_temp_avg / 5.0) * 5 AS t,
               sum({s_col} - {e_col}) AS rd, sum(distance) AS km,
               sum(duration_min) AS minutes, count(*) AS n
        FROM drives
        WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
          AND distance IS NOT NULL AND distance >= $4
          AND outside_temp_avg IS NOT NULL AND {s_col} - {e_col} > 0.1
        GROUP BY 1 ORDER BY 1
        """,  # noqa: S608
        car_id, lo, hi, float(min_distance_km),
    )
    segs = await db.fetch(
        f"""
        WITH p AS (
            SELECT p.drive_id, p.date, p.odometer, p.{p_col} AS rr, p.outside_temp
            FROM positions p
            WHERE p.car_id = $1 AND p.ideal_battery_range_km IS NOT NULL
              AND p.date >= $2 AND p.date < $3
              AND p.drive_id IS NOT NULL AND p.odometer IS NOT NULL
        ), d AS (
            SELECT drive_id, outside_temp,
                   odometer - lag(odometer) OVER w AS dk,
                   lag(rr) OVER w - rr AS dr,
                   EXTRACT(EPOCH FROM (date - lag(date) OVER w)) AS dt,
                   floor((odometer - first_value(odometer) OVER w) / 2) AS seg
            FROM p WINDOW w AS (PARTITION BY drive_id ORDER BY date)
        )
        SELECT drive_id, seg, sum(dk) AS km, sum(dr) AS rd, sum(dt) AS secs, count(*) AS n,
               avg(outside_temp) AS temp
        FROM d WHERE dk IS NOT NULL AND dt > 0 AND dt < 300
        GROUP BY 1, 2
        """,  # noqa: S608
        car_id, lo, hi, timeout_s=25,
    )
    speed: dict[int, dict] = {}
    for s in segs:
        km, secs = _f(s["km"]) or 0, _f(s["secs"]) or 0
        if km < 1.0 or secs <= 0 or (s["n"] or 0) < 2:
            continue
        v = km / secs * 3600
        if v < 9:
            continue
        b = speed.setdefault(int(v // 10) * 10, {"km": 0.0, "rd": 0.0, "segments": 0, "t": []})
        b["km"] += km
        b["rd"] += _f(s["rd"]) or 0
        b["segments"] += 1
        if s["temp"] is not None:
            b["t"].append(float(s["temp"]))
    derived = {}
    for kind in ("rated", "ideal"):
        rs = await db.fetch(
            f"""
            SELECT round((charge_energy_added / NULLIF(end_{kind}_range_km - start_{kind}_range_km, 0))::numeric, 3) AS f,
                   count(*) AS n
            FROM charging_processes
            WHERE car_id = $1 AND duration_min > 10 AND end_battery_level <= 95
              AND start_{kind}_range_km IS NOT NULL AND end_{kind}_range_km IS NOT NULL
              AND charge_energy_added > 0
            GROUP BY 1 ORDER BY 2 DESC LIMIT 3
            """,  # noqa: S608
            car_id,
        )
        derived[kind] = [{"kwh_per_km": _f(r["f"], 3), "count": r["n"]} for r in rs if r["f"] is not None]
    net_wh = _div(net_kwh, net["km"], 1, 1000)
    return {
        "car": car, "period": period.as_dict(), "preferred_range": pr,
        "rated_wh_per_km": _f(eff * 1000, 1) if eff else None,
        "net": {"kwh": _f(net_kwh, 1), "km": _f(net["km"], 1), "drives": net["n"], "wh_per_km": net_wh},
        "gross": gross,
        "overhead_pct": _f((1 - net_wh / gross["wh_per_km"]) * 100, 1)
        if net_wh and gross.get("wh_per_km") else None,
        "by_temperature": [
            {"temp_c": _f(r["t"], 0), "drives": r["n"], "km": _f(r["km"], 1),
             "efficiency_pct": _div(r["km"], r["rd"], 1, 100),
             "wh_per_km": _div((_f(r["rd"]) or 0) * (eff or 0), r["km"], 1, 1000) if eff else None,
             "avg_speed_kmh": _div(r["km"], r["minutes"], 1, 60)}
            for r in by_temp
        ],
        "by_speed": [
            {"speed_kmh": k, "speed_max_kmh": k + 10, "km": _f(v["km"], 1), "segments": v["segments"],
             "wh_per_km": _div(v["rd"] * (eff or 0), v["km"], 1, 1000) if eff else None,
             "efficiency_pct": _div(v["km"], v["rd"], 1, 100) if v["rd"] > 0 else None,
             "avg_temp_c": _f(sum(v["t"]) / len(v["t"]), 1) if v["t"] else None}
            for k, v in sorted(speed.items())
        ],
        "derived_efficiencies": derived,
    }


# ======================================================================== 电池

async def battery(car_id: int, trend_bucket: str = "month") -> dict:
    """电池健康 —— 对齐 Battery Health 面板（battery-health.json 的 $aux 查询）。

    容量 = 每条充电采样的 rated_range × (kWh/100km) / usable_battery_level；
    现在 = 最近 100 条（来自 charge_energy_added ≥ aux 效率的已结束场次）的平均；
    新车 = 用户填写的出厂容量，否则每场最后一条采样的最大值。
    另给 100% 续航趋势（Projected Range 面板的算法）与按里程的容量曲线。
    """
    if trend_bucket not in ("week", "month"):
        trend_bucket = "month"
    car = await car_meta(car_id)
    tz = local_tz_name()
    aux = await db.fetchrow(
        """
        SELECT COALESCE(derived, car_eff) AS eff FROM (
            SELECT ROUND((cp.charge_energy_added / NULLIF(cp.end_rated_range_km - cp.start_rated_range_km, 0))::numeric, 3) * 100 AS derived,
                   count(*) AS n, c.efficiency * 100 AS car_eff
            FROM cars c LEFT JOIN charging_processes cp ON c.id = cp.car_id
                 AND cp.duration_min > 10 AND cp.end_battery_level <= 95
                 AND cp.start_rated_range_km IS NOT NULL AND cp.end_rated_range_km IS NOT NULL
                 AND cp.charge_energy_added > 0
            WHERE c.id = $1 GROUP BY 1, 3 ORDER BY 2 DESC LIMIT 1
        ) e
        """,
        car_id,
    )
    aux_eff = _f(aux["eff"]) if aux else None      # kWh / 100 km（rated）
    cur = mx = None
    by_mileage = []
    if aux_eff:
        cur = await db.fetchval(
            """
            SELECT avg(cap) FROM (
                SELECT c.rated_battery_range_km * $2::float8 / c.usable_battery_level AS cap
                FROM charging_processes cp JOIN charges c ON c.charging_process_id = cp.id
                WHERE cp.car_id = $1 AND cp.end_date IS NOT NULL
                  AND cp.charge_energy_added >= $2::float8 AND c.usable_battery_level > 0
                ORDER BY cp.end_date DESC, c.date DESC LIMIT 100
            ) x
            """,
            car_id, aux_eff,
        )
        lastc = await db.fetch(
            """
            SELECT cp.id, cp.end_date, p.odometer,
                   c.rated_battery_range_km * $2::float8 / c.usable_battery_level AS cap
            FROM charging_processes cp
            JOIN LATERAL (SELECT rated_battery_range_km, usable_battery_level FROM charges
                          WHERE charging_process_id = cp.id AND usable_battery_level > 0
                          ORDER BY date DESC LIMIT 1) c ON TRUE
            LEFT JOIN positions p ON p.id = cp.position_id
            WHERE cp.car_id = $1 AND cp.end_date IS NOT NULL AND cp.charge_energy_added >= $2::float8
            ORDER BY cp.end_date
            """,
            car_id, aux_eff,
        )
        caps = [_f(r["cap"]) for r in lastc if r["cap"] is not None]
        mx = max(caps) if caps else None
        days: dict[str, list] = {}
        for r in lastc:
            if r["cap"] is None:
                continue
            k = r["end_date"].replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).date().isoformat()
            days.setdefault(k, []).append((r["odometer"], float(r["cap"])))
        by_mileage = [
            {"date": k, "odometer_km": _f(_stats.mean(o for o, _ in v if o is not None), 0)
             if any(o is not None for o, _ in v) else None,
             "kwh": _f(_stats.mean(c for _, c in v), 1)}
            for k, v in sorted(days.items())
        ]
    pr = await preferred_range()
    _, _, p_col = _range_cols(pr)
    now_row = await db.fetchrow(
        f"""
        SELECT * FROM (
          (SELECT date, {p_col} AS r, usable_battery_level AS u FROM positions
           WHERE car_id = $1 AND ideal_battery_range_km IS NOT NULL AND usable_battery_level > 0
           ORDER BY date DESC LIMIT 1)
          UNION ALL
          (SELECT c.date, c.{p_col}, c.usable_battery_level FROM charges c
           JOIN charging_processes p ON p.id = c.charging_process_id
           WHERE p.car_id = $1 AND c.usable_battery_level > 0 ORDER BY c.date DESC LIMIT 1)
        ) x ORDER BY date DESC LIMIT 1
        """,  # noqa: S608
        car_id,
    )
    max_range = await db.fetchval(
        f"""
        SELECT CASE WHEN sum(usable_battery_level) = 0 THEN sum({p_col}) * 100
                    ELSE sum({p_col}) / sum(usable_battery_level) * 100 END AS r
        FROM charges c JOIN charging_processes p ON p.id = c.charging_process_id
        WHERE p.car_id = $1 AND usable_battery_level IS NOT NULL
        GROUP BY floor(extract(epoch FROM date) / 86400) ORDER BY 1 DESC NULLS LAST LIMIT 1
        """,  # noqa: S608
        car_id,
    )
    from ..alerts.store import store

    override = (await store.get_setting("battery_baseline", {}) or {}).get(str(car_id)) or {}
    kwh_new = _f(override.get("kwh")) or mx
    range_new = _f(override.get("range_km")) or _f(max_range)
    cur = _f(cur)
    degradation = (max(0.0, 100.0 - cur * 100.0 / kwh_new) if (cur and kwh_new) else None)
    range_now = (_f(now_row["r"]) * 100.0 / now_row["u"]) if (now_row and now_row["r"] is not None and now_row["u"]) else None
    totals = await db.fetchrow(
        """
        SELECT count(*) AS n, sum(charge_energy_added) AS added,
               sum(GREATEST(charge_energy_added, charge_energy_used)) AS used
        FROM charging_processes WHERE car_id = $1 AND charge_energy_added > 0.01
        """,
        car_id,
    )
    acdc = await db.fetch(
        """
        WITH d AS (
            SELECT cp.id, GREATEST(cp.charge_energy_added, cp.charge_energy_used) AS e,
                   CASE WHEN NULLIF(mode() WITHIN GROUP (ORDER BY c.charger_phases), 0) IS NULL
                        THEN 'DC' ELSE 'AC' END AS k
            FROM charging_processes cp JOIN charges c ON c.charging_process_id = cp.id
            WHERE cp.car_id = $1 AND cp.charge_energy_added > 0.01
            GROUP BY cp.id
        ) SELECT k, sum(e) AS e FROM d GROUP BY k
        """,
        car_id,
    )
    trend = await range_trend(car_id, bucket=trend_bucket)
    cov = await odometer_coverage(car_id)
    added = _f(totals["added"]) if totals else None
    return {
        "car": car, "preferred_range": pr, "tz": tz,
        "efficiency_kwh_per_100km": aux_eff,
        "capacity_kwh": {"now": _f(cur, 1), "new": _f(kwh_new, 1),
                         "new_source": "user" if override.get("kwh") else "observed_max",
                         "difference": _f(cur - kwh_new, 1) if (cur and kwh_new) else None},
        "degradation_pct": _f(degradation, 1),
        "health_pct": _f(min(100.0, 100.0 - degradation), 1) if degradation is not None else None,
        "range_100_km": {"now": _f(range_now, 1), "max": _f(range_new, 1),
                         "max_source": "user" if override.get("range_km") else "observed_best_day",
                         "lost": _f(range_new - range_now, 1) if (range_now and range_new) else None},
        "soc_now": now_row["u"] if now_row else None,
        "stored_kwh_now": _f(now_row["u"] * cur / 100, 1) if (now_row and cur) else None,
        "charges": int(totals["n"] or 0) if totals else 0,
        "kwh_added_total": _f(added, 1),
        "kwh_used_total": _f(totals["used"], 1) if totals else None,
        "charging_efficiency_pct": _div(added, totals["used"], 1, 100) if totals else None,
        "cycles": int(added // kwh_new) if (added and kwh_new) else None,
        "ac_dc_kwh": {r["k"]: _f(r["e"], 1) for r in acdc},
        "mileage": cov,
        "capacity_by_mileage": by_mileage,
        "range_trend": trend["points"],
    }


async def range_trend(car_id: int, period: Period | None = None, bucket: str = "day") -> dict:
    """100% 续航（Projected Range 面板）：Σ range / Σ COALESCE(usable, level) × 100，
    完整位置行 + 充电采样，按本地日/周/月分桶；另给同桶的平均里程与车外温度。"""
    if bucket not in ("hour", "day", "week", "month"):
        bucket = "day"
    pr = await preferred_range()
    _, _, p_col = _range_cols(pr)
    tz = local_tz_name()
    lo, hi = (period.bounds() if period else (EARLIEST, utcnow() + timedelta(days=1)))
    rows = await db.fetch(
        f"""
        WITH data AS (
            SELECT date, battery_level, usable_battery_level, {p_col} AS r, outside_temp, odometer
            FROM positions WHERE car_id = $1 AND date >= $2 AND date < $3
              AND ideal_battery_range_km IS NOT NULL
            UNION ALL
            SELECT c.date, c.battery_level, COALESCE(c.usable_battery_level, c.battery_level),
                   c.{p_col}, c.outside_temp, NULL
            FROM charges c JOIN charging_processes p ON p.id = c.charging_process_id
            WHERE p.car_id = $1 AND c.date >= $2 AND c.date < $3
        )
        SELECT date_trunc('{bucket}', date AT TIME ZONE 'UTC' AT TIME ZONE $4) AS b,
               sum(r) / NULLIF(sum(COALESCE(usable_battery_level, battery_level)), 0) * 100 AS r100,
               avg(odometer) AS odo, avg(outside_temp) AS temp,
               avg(COALESCE(usable_battery_level, battery_level)) AS soc, count(*) AS n
        FROM data GROUP BY 1 HAVING sum(COALESCE(usable_battery_level, battery_level)) > 0
        ORDER BY 1
        """,  # noqa: S608 - bucket 已过白名单
        car_id, lo, hi, tz,
    )
    pts = [{"date": r["b"].date().isoformat(), "range_100_km": _f(r["r100"], 1),
            "odometer_km": _f(r["odo"], 0), "outside_temp_c": _f(r["temp"], 1),
            "soc": _f(r["soc"], 0), "samples": r["n"]} for r in rows]
    vals = [p["range_100_km"] for p in pts if p["range_100_km"]]
    return {"bucket": bucket, "tz": tz, "preferred_range": pr, "points": pts,
            "now_km": vals[-1] if vals else None, "max_km": max(vals) if vals else None}


async def odometer_coverage(car_id: int) -> dict:
    """里程表跨度 vs 记录到的行程距离。跨度取 positions（不只 drives）：
    本机 1 号车 08-29 之后 TeslaMate 没记行程，但 positions 里里程表走了 1000+ km。"""
    r = await db.fetchrow(
        """
        SELECT (SELECT odometer FROM positions WHERE car_id = $1 AND ideal_battery_range_km IS NOT NULL
                  AND odometer IS NOT NULL ORDER BY date LIMIT 1) AS first_pos,
               (SELECT odometer FROM positions WHERE car_id = $1 AND ideal_battery_range_km IS NOT NULL
                  AND odometer IS NOT NULL ORDER BY date DESC LIMIT 1) AS last_pos,
               (SELECT min(start_km) FROM drives WHERE car_id = $1) AS first_drive,
               (SELECT max(end_km) FROM drives WHERE car_id = $1) AS last_drive,
               (SELECT COALESCE(sum(distance), 0) FROM drives WHERE car_id = $1) AS logged
        """,
        car_id,
    )
    firsts = [v for v in (r["first_pos"], r["first_drive"]) if v is not None]
    lasts = [v for v in (r["last_pos"], r["last_drive"]) if v is not None]
    if not firsts or not lasts:
        return {"odometer_km": None, "span_km": None, "logged_km": 0.0, "unlogged_km": None,
                "coverage_pct": None, "drives_span_km": None}
    first, last = min(firsts), max(lasts)
    span = last - first
    logged = float(r["logged"] or 0)
    dspan = (r["last_drive"] - r["first_drive"]) if (r["last_drive"] is not None and r["first_drive"] is not None) else None
    return {
        "odometer_km": _f(last, 1),
        "span_km": _f(span, 1),
        "logged_km": _f(logged, 1),
        "unlogged_km": _f(max(span - logged, 0.0), 1),
        "coverage_pct": _f(logged / span * 100, 1) if span > 0 else None,
        "drives_span_km": _f(dspan, 1),       # TeslaMate Battery Health "Mileage" 的口径
    }


# ======================================================================== 停放耗电

async def vampire(car_id: int, period: Period, min_idle_h: float = 6.0) -> dict:
    """停放耗电 —— 对齐 Vampire Drain 面板（vampire-drain.json，4.3.0 修正后的 standby）。

    一个停放窗口 = 上一件事（行程或充电）结束 → 下一件事开始；充电本身也是边界，
    所以"停车→充电→停车"会切成两段，而不是整段扔掉。过滤：时长 > min_idle_h、
    续航没有变多、里程表没走（< 1 km，否则那是没记上的行车）。
    standby = 窗口内 asleep+offline 状态的重叠时长占比（"睡着的比例"）。
    结束时电池偏冷（usable < level）的窗口不给 kWh（续航读数不可信）。
    """
    pr = await preferred_range()
    s_col, e_col, _ = _range_cols(pr)
    car = await car_meta(car_id)
    eff = car["efficiency_kwh_per_km"]
    lo, hi = period.bounds()
    rows = await db.fetch(
        f"""
        WITH merge AS (
            SELECT c.start_date, c.end_date, c.{s_col} AS start_range, c.{e_col} AS end_range,
                   c.start_battery_level AS s_bl, c.end_battery_level AS e_bl,
                   p.usable_battery_level AS s_ubl, NULL::smallint AS e_ubl,
                   p.odometer AS start_km, p.odometer AS end_km
            FROM charging_processes c JOIN positions p ON c.position_id = p.id
            WHERE c.car_id = $1 AND c.start_date >= $2 AND c.start_date < $3
            UNION
            SELECT d.start_date, d.end_date, d.{s_col}, d.{e_col},
                   sp.battery_level, ep.battery_level, sp.usable_battery_level, ep.usable_battery_level,
                   d.start_km, d.end_km
            FROM drives d
            JOIN positions sp ON d.start_position_id = sp.id
            JOIN positions ep ON d.end_position_id = ep.id
            WHERE d.car_id = $1 AND d.start_date >= $2 AND d.start_date < $3
        ), v AS (
            SELECT lag(t.end_date) OVER w AS start_date, t.start_date AS end_date,
                   lag(t.end_range) OVER w AS start_range, t.start_range AS end_range,
                   lag(t.end_km) OVER w AS start_km, t.start_km AS end_km,
                   EXTRACT(EPOCH FROM (t.start_date - lag(t.end_date) OVER w)) AS duration,
                   lag(t.e_bl) OVER w AS start_soc, t.s_bl AS end_soc,
                   t.s_bl > COALESCE(t.s_ubl, t.s_bl) AS reduced
            FROM merge t WINDOW w AS (ORDER BY t.start_date)
        )
        SELECT v.*, COALESCE(sb.sleep, 0) AS sleep_s
        FROM v, LATERAL (
            SELECT EXTRACT(EPOCH FROM sum(LEAST(s.end_date, v.end_date)
                                          - GREATEST(s.start_date, v.start_date))) AS sleep
            FROM states s
            WHERE s.state IN ('asleep', 'offline') AND s.car_id = $1
              AND s.start_date < v.end_date AND (s.end_date IS NULL OR s.end_date > v.start_date)
        ) sb
        WHERE v.duration > $4 * 3600 AND v.start_range - v.end_range >= 0
          AND v.end_km - v.start_km < 1
        ORDER BY v.start_date
        """,  # noqa: S608
        car_id, lo, hi, float(min_idle_h),
    )
    windows = []
    tot_h = tot_kwh = tot_km = sleep_s = kwh_h = 0.0
    for r in rows:
        dur = float(r["duration"])
        hours = dur / 3600
        loss = float(r["start_range"]) - float(r["end_range"])
        kwh = None if (r["reduced"] or not eff) else loss * eff
        standby = min(float(r["sleep_s"] or 0) / dur, 1.0) if dur > 0 else None
        windows.append({
            "start": iso(r["start_date"]), "end": iso(r["end_date"]), "hours": round(hours, 1),
            "standby_pct": _f(standby * 100, 1) if standby is not None else None,
            "start_soc": r["start_soc"], "end_soc": r["end_soc"],
            "soc_diff": -max((r["start_soc"] or 0) - (r["end_soc"] or 0), 0)
            if r["start_soc"] is not None and r["end_soc"] is not None else None,
            "range_lost_km": _f(loss, 1),
            "range_lost_per_hour_km": _f(loss / hours, 2) if hours else None,
            "kwh": _f(kwh, 2),
            "avg_watts": _f(kwh / hours * 1000, 0) if (kwh is not None and hours) else None,
            "kwh_per_day": _f(kwh / hours * 24, 2) if (kwh is not None and hours) else None,
            "reduced_range": bool(r["reduced"]),
        })
        tot_h += hours
        tot_km += loss
        sleep_s += float(r["sleep_s"] or 0)
        if kwh is not None:
            tot_kwh += kwh
            kwh_h += hours
    return {
        "car": car, "period": period.as_dict(), "preferred_range": pr, "min_idle_h": min_idle_h,
        "windows": windows,
        "summary": {
            "windows": len(windows),
            "hours": round(tot_h, 1),
            "kwh": _f(tot_kwh, 2),
            "kwh_per_day": _f(tot_kwh / kwh_h * 24, 2) if kwh_h else None,
            "avg_watts": _f(tot_kwh / kwh_h * 1000, 0) if kwh_h else None,
            "km_per_day": _f(tot_km / tot_h * 24, 1) if tot_h else None,
            "asleep_pct": _f(sleep_s / (tot_h * 3600) * 100, 1) if tot_h else None,
        },
    }


# ======================================================================== 状态时间线

_PRIORITY = {"updating": 6, "driving": 5, "charging": 4, "online": 3, "asleep": 2, "offline": 1}


async def states_timeline(car_id: int, period: Period, max_segments: int = 5000) -> dict:
    """状态时间线 —— 对齐 States 面板（states.json 的 state-timeline）。

    底色是 states 表（online/asleep/offline），上面叠 行程(driving) / 充电(charging) /
    更新(updating)。未关闭的 states 行截到"现在"（归档的车截到 last_seen —— 不把
    一辆早已离开账号的车当成"连续在线 68 天"）；未关闭的行程/充电截到最后一个采样点。
    没有任何 states 覆盖的时间（记录器停机）单独报 uncovered，不算进任何状态。
    """
    car = await car_meta(car_id)
    cap = await car_registry.open_state_cap(car_id)
    lo, hi = period.bounds()
    hi = min(hi, max(cap, lo))
    rows = await db.fetch(
        """
        SELECT 'state' AS src, state::text AS kind, start_date AS s, COALESCE(end_date, $4) AS e
        FROM states WHERE car_id = $1 AND start_date < $3 AND COALESCE(end_date, $4) > $2
        UNION ALL
        SELECT 'act', 'driving', d.start_date,
               COALESCE(d.end_date, (SELECT max(p.date) FROM positions p WHERE p.drive_id = d.id
                                     AND p.date >= d.start_date), d.start_date)
        FROM drives d WHERE d.car_id = $1 AND d.start_date < $3
          AND (d.end_date IS NULL OR d.end_date > $2)
        UNION ALL
        SELECT 'act', 'charging', cp.start_date,
               COALESCE(cp.end_date, (SELECT max(c.date) FROM charges c WHERE c.charging_process_id = cp.id),
                        cp.start_date)
        FROM charging_processes cp WHERE cp.car_id = $1 AND cp.start_date < $3
          AND (cp.end_date IS NULL OR cp.end_date > $2)
        UNION ALL
        SELECT 'act', 'updating', u.start_date, COALESCE(u.end_date, u.start_date)
        FROM updates u WHERE u.car_id = $1 AND u.start_date < $3
          AND (u.end_date IS NULL OR u.end_date > $2)
        """,
        car_id, lo, hi, cap,
    )
    ivals = []
    for r in rows:
        s, e = max(r["s"], lo), min(r["e"], hi)
        if e > s:
            ivals.append((s, e, r["kind"]))
    points = sorted({lo, hi, *(s for s, _, _ in ivals), *(e for _, e, _ in ivals)})
    segs: list[list] = []
    starts = sorted(ivals, key=lambda x: x[0])
    active: list[tuple] = []
    j = 0
    for a, b in itertools.pairwise(points):
        while j < len(starts) and starts[j][0] <= a:
            active.append(starts[j])
            j += 1
        active = [x for x in active if x[1] > a]
        cur = [x for x in active if x[0] <= a < x[1]]
        kind = max((x[2] for x in cur), key=lambda k: _PRIORITY.get(k, 0)) if cur else "unknown"
        if segs and segs[-1][2] == kind and segs[-1][1] == a:
            segs[-1][1] = b
        else:
            segs.append([a, b, kind])
    totals: dict[str, float] = {}
    for a, b, k in segs:
        totals[k] = totals.get(k, 0.0) + (b - a).total_seconds() / 60
    covered = sum(v for k, v in totals.items() if k != "unknown")
    last = await db.fetchrow(
        "SELECT state::text AS state, start_date FROM states WHERE car_id = $1"
        " ORDER BY start_date DESC LIMIT 1", car_id)
    truncated = len(segs) > max_segments
    return {
        "car": car, "period": {**period.as_dict(), "effective_to": iso(hi)},
        "segments": [{"state": k, "start": iso(a), "end": iso(b),
                      "minutes": round((b - a).total_seconds() / 60, 1)}
                     for a, b, k in segs[-max_segments:]],
        "truncated": truncated,
        "totals_min": {k: round(v) for k, v in totals.items()},
        "pct": {k: _f(v / covered * 100, 1) for k, v in totals.items() if k != "unknown" and covered},
        "parked_pct": _f((1 - totals.get("driving", 0) / covered) * 100, 1) if covered else None,
        "covered_min": round(covered),
        "uncovered_min": round(totals.get("unknown", 0)),
        "current": {"state": last["state"], "since": iso(last["start_date"]),
                    "stale": car["archived"]} if last else None,
    }


# ======================================================================== 统计

_PERIODS = ("day", "week", "month", "year", "total")
# total 桶的键：与 SQL 里的 TIMESTAMP '1970-01-01' 对应（naive）
_EPOCH = datetime.fromtimestamp(0, UTC).replace(tzinfo=None)


async def statistics(car_id: int, period: Period, bucket: str = "month") -> dict:
    """周期统计 —— 对齐 Statistics 面板（statistics.json，含 4.3.0 的 total）。

    每桶：行程数、时长、距离（里程表跨度 max(end_km)-min(start_km) 与 Σdistance 都给）、
    平均车外温度、效率（Σ距离/Σ续航差）、净/毛能耗与 overhead、充电次数、
    Σ电池端/Σ墙端电量、费用（唯一真源）、每 kWh 成本、每 100 km 成本。
    桶按本地时区切（date_trunc(bucket, 本地时间)）。
    """
    if bucket not in _PERIODS:
        raise HTTPException(400, f"period must be one of {', '.join(_PERIODS)}")
    pr = await preferred_range()
    s_col, e_col, _ = _range_cols(pr)
    car = await car_meta(car_id)
    eff = car["efficiency_kwh_per_km"]
    tz = local_tz_name()
    lo, hi = period.bounds()
    bexpr = ("TIMESTAMP '1970-01-01'" if bucket == "total"
             else f"date_trunc('{bucket}', {{col}} AT TIME ZONE 'UTC' AT TIME ZONE $4)")
    # total 桶不引用 $4：asyncpg 会核对参数个数，多传一个就报错
    args = [car_id, lo, hi] + ([] if bucket == "total" else [tz])
    drives = await db.fetch(
        f"""
        SELECT {bexpr.format(col='d.start_date')} AS b, count(*) AS n,
               sum(d.duration_min) AS minutes, max(d.end_km) - min(d.start_km) AS span_km,
               sum(d.distance) AS km, avg(d.outside_temp_avg) AS temp,
               sum(d.{s_col} - d.{e_col}) AS rd,
               sum(d.distance) FILTER (WHERE d.{s_col} - d.{e_col} >= 0.1) AS km_net,
               sum(d.{s_col} - d.{e_col}) FILTER (WHERE d.{s_col} - d.{e_col} >= 0.1) AS rd_net,
               max(d.speed_max) AS vmax
        FROM drives d
        WHERE d.car_id = $1 AND d.start_date >= $2 AND d.start_date < $3
        GROUP BY 1
        """,  # noqa: S608 - bucket 已过白名单
        *args,
    )
    charges = await db.fetch(
        f"""
        SELECT cp.id, {bexpr.format(col='cp.start_date')} AS b,
               cp.charge_energy_added::float8 AS added,
               GREATEST(cp.charge_energy_added, cp.charge_energy_used)::float8 AS used,
               cp.end_date IS NULL AS open,
               -- 次数/电量按 TeslaMate 的过滤（added 为空或 > 0）；费用不过滤 ——
               -- 每一场的钱都要算进去，否则和 Energy/日历/汇总页的合计对不上
               (cp.charge_energy_added IS NULL OR cp.charge_energy_added > 0) AS counted
        FROM charging_processes cp
        WHERE cp.car_id = $1 AND cp.start_date >= $2 AND cp.start_date < $3
        """,  # noqa: S608
        *args,
    )
    costs = await cost_source.session_costs(car_id)
    ev = await _events(car_id, lo, hi, pr)
    z = ZoneInfo(tz)
    _tz = timezone

    def bucket_of(dt: datetime) -> datetime:
        if bucket == "total":
            return _EPOCH
        loc = dt.replace(tzinfo=_tz.utc).astimezone(z).replace(tzinfo=None)
        if bucket == "day":
            return loc.replace(hour=0, minute=0, second=0, microsecond=0)
        if bucket == "week":
            d0 = loc - timedelta(days=loc.weekday())
            return d0.replace(hour=0, minute=0, second=0, microsecond=0)
        if bucket == "month":
            return loc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return loc.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    gross: dict[datetime, list[float]] = {}
    for start, dist, loss in _gross_from_events(ev):
        g = gross.setdefault(bucket_of(start), [0.0, 0.0])
        g[0] += dist
        g[1] += loss
    out: dict[datetime, dict] = {}

    def row(b):
        return out.setdefault(b, {"b": b, "drives": 0, "duration_min": 0, "distance_km": None,
                                  "logged_km": 0.0, "temp": None, "rd": None, "km_net": None,
                                  "rd_net": None, "speed_max_kmh": None, "charges": 0,
                                  "added": 0.0, "used": 0.0, "cost": 0.0, "priced": 0,
                                  "open_charges": 0})

    for r in drives:
        x = row(r["b"])
        x.update(drives=r["n"], duration_min=int(r["minutes"] or 0), distance_km=_f(r["span_km"]),
                 logged_km=_f(r["km"]) or 0.0, temp=_f(r["temp"]), rd=_f(r["rd"]),
                 km_net=_f(r["km_net"]), rd_net=_f(r["rd_net"]), speed_max_kmh=r["vmax"])
    for r in charges:
        x = row(r["b"])
        if r["counted"]:
            x["charges"] += 1
            x["added"] += r["added"] or 0.0
            x["used"] += r["used"] or 0.0
            if r["open"]:
                x["open_charges"] += 1
        c = (costs.get(r["id"]) or {}).get("cost")
        if c is not None:
            x["cost"] += c
            x["priced"] += 1
    for b in gross:
        row(b)
    rows_out = []
    for b in sorted(out, reverse=True):
        x = out[b]
        g = gross.get(b, [0.0, 0.0])
        net_wh = _div((x["rd_net"] or 0) * eff, x["km_net"], 1, 1000) if eff and x["km_net"] else None
        gross_wh = _div(g[1] * eff, g[0], 1, 1000) if eff and g[0] > 1 else None
        cost_per_added = _div(x["cost"], x["added"]) if x["priced"] else None
        if bucket == "total":
            frm, to = period.start, period.end
            label = "total"
        else:
            nxt = {"day": timedelta(days=1), "week": timedelta(days=7)}.get(bucket)
            if nxt:
                end_local = b + nxt
            elif bucket == "month":
                end_local = (b.replace(day=28) + timedelta(days=4)).replace(day=1)
            else:
                end_local = b.replace(year=b.year + 1)
            frm = b.replace(tzinfo=z).astimezone(_tz.utc).replace(tzinfo=None)
            to = end_local.replace(tzinfo=z).astimezone(_tz.utc).replace(tzinfo=None)
            label = b.date().isoformat()
        rows_out.append({
            "date": label, "from": iso(frm), "to": iso(to),
            "drives": x["drives"], "duration_min": x["duration_min"],
            "distance_km": _f(x["distance_km"], 1), "logged_km": _f(x["logged_km"], 1),
            "outside_temp_avg_c": _f(x["temp"], 1),
            "efficiency_pct": _div(x["logged_km"], x["rd"], 1, 100) if (x["rd"] or 0) > 0 else None,
            "net_kwh": _f((x["rd_net"] or 0) * eff, 2) if eff and x["rd_net"] is not None else None,
            "consumption_net_wh_per_km": net_wh,
            "consumption_gross_wh_per_km": gross_wh,
            "overhead_pct": _f((1 - net_wh / gross_wh) * 100, 1) if (net_wh and gross_wh) else None,
            "speed_max_kmh": x["speed_max_kmh"],
            "charges": x["charges"], "open_charges": x["open_charges"],
            "energy_added_kwh": _f(x["added"], 2), "energy_used_kwh": _f(x["used"], 2),
            "avg_energy_per_charge_kwh": _div(x["used"], x["charges"], 2),
            "cost": _f(x["cost"], 2) if x["priced"] else None,
            "cost_per_kwh": _div(x["cost"], x["used"], 3) if x["priced"] and x["used"] else None,
            "cost_per_100km": _f(gross_wh / 10 * cost_per_added, 2)
            if (gross_wh and cost_per_added) else None,
        })
    return {"car": car, "period": period.as_dict(), "bucket": bucket, "tz": tz,
            "preferred_range": pr, "currency": await cost_source.currency(), "rows": rows_out}


# ======================================================================== 里程

async def mileage(car_id: int, period: Period, bucket: str = "day") -> dict:
    """里程（Mileage 面板）—— 但用 positions 的里程表，而不只是 drives 的起止。

    TeslaMate 的 Mileage 面板只画行程起止的里程表，记录器漏记的行程在图上是一段
    "平台"。这里按本地日/周/月取里程表最大值，增量 = 本桶最大值 - 上一桶最大值，
    并给出同桶 drives 记录到的距离；两者之差就是没记上的里程。
    """
    if bucket not in ("day", "week", "month", "year"):
        bucket = "day"
    tz = local_tz_name()
    car = await car_meta(car_id)
    lo, hi = period.bounds()
    odo = await db.fetch(
        f"""
        SELECT date_trunc('{bucket}', date AT TIME ZONE 'UTC' AT TIME ZONE $4) AS b,
               max(odometer) AS hi, min(odometer) AS lo
        FROM positions
        WHERE car_id = $1 AND date >= $2 AND date < $3
          AND ideal_battery_range_km IS NOT NULL AND odometer IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,  # noqa: S608 - bucket 已过白名单
        car_id, lo, hi, tz,
    )
    logged = {r["b"]: float(r["km"] or 0) for r in await db.fetch(
        f"""
        SELECT date_trunc('{bucket}', start_date AT TIME ZONE 'UTC' AT TIME ZONE $4) AS b,
               sum(distance) AS km
        FROM drives WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
        GROUP BY 1
        """,  # noqa: S608
        car_id, lo, hi, tz,
    )}
    before = await db.fetchval(
        "SELECT odometer FROM positions WHERE car_id = $1 AND date < $2"
        " AND ideal_battery_range_km IS NOT NULL AND odometer IS NOT NULL"
        " ORDER BY date DESC LIMIT 1",
        car_id, lo,
    )
    rows = []
    prev = _f(before)
    for r in odo:
        top = float(r["hi"])
        base = prev if prev is not None else float(r["lo"])
        km = max(top - base, 0.0)
        lg = logged.get(r["b"], 0.0)
        rows.append({"date": r["b"].date().isoformat(), "odometer_km": round(top, 1),
                     "km": round(km, 1), "logged_km": round(lg, 1),
                     "unlogged_km": round(max(km - lg, 0.0), 1)})
        prev = top
    total = sum(x["km"] for x in rows)
    lg_total = sum(x["logged_km"] for x in rows)
    return {
        "car": car, "period": period.as_dict(), "bucket": bucket, "tz": tz,
        "rows": rows,
        "start_odometer_km": _f(before if before is not None else (odo[0]["lo"] if odo else None), 1),
        "end_odometer_km": rows[-1]["odometer_km"] if rows else None,
        "total_km": round(total, 1), "logged_km": round(lg_total, 1),
        "unlogged_km": round(max(total - lg_total, 0.0), 1),
    }


# ======================================================================== 地点

async def locations(car_id: int, period: Period, limit: int = 20) -> dict:
    """地点 —— 对齐 Locations 面板 + 4.4.0 重做的 Visited 面板。

    城市一律 COALESCE(city, neighbourhood)（4.3.0 起 Nominatim 改按 rank 映射，
    小地方的名字落在 neighbourhood）；国家键优先 raw→address→country_code。
    常去地点按围栏优先聚合（同一个围栏里的不同门牌算一个地方），
    停留时长 = 到达后到下一次出发，里程表动了的不算（那是没记上的行车）。
    """
    tz = local_tz_name()
    car = await car_meta(car_id)
    lo, hi = period.bounds()
    counts = await db.fetchrow(
        """
        WITH ids AS (
            SELECT start_address_id AS id FROM drives WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
            UNION SELECT end_address_id FROM drives WHERE car_id = $1 AND end_date >= $2 AND end_date < $3
            UNION SELECT address_id FROM charging_processes WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
        )
        SELECT count(*) AS addresses, count(DISTINCT COALESCE(city, neighbourhood)) AS cities,
               count(DISTINCT state) AS states, count(DISTINCT country) AS countries
        FROM addresses WHERE id IN (SELECT id FROM ids)
        """,
        car_id, lo, hi,
    )
    places = await db.fetch(
        f"""
        WITH d AS (
            SELECT d.*, lead(d.start_date) OVER w AS nxt, lead(d.start_km) OVER w AS nxt_km
            FROM drives d WHERE d.car_id = $1
            WINDOW w AS (ORDER BY d.start_date)
        ), arrivals AS (
            SELECT COALESCE('g' || d.end_geofence_id::text, 'a' || d.end_address_id::text) AS k,
                   d.end_geofence_id AS gid, d.end_address_id AS aid, d.end_date AS at,
                   CASE WHEN d.nxt IS NOT NULL AND COALESCE(d.nxt_km - d.end_km, 0) <= 0.5
                        THEN LEAST(EXTRACT(EPOCH FROM (d.nxt - d.end_date)) / 60, 7 * 24 * 60)
                        ELSE 0 END AS dwell
            FROM d WHERE d.end_date IS NOT NULL AND d.end_date >= $2 AND d.end_date < $3
              AND (d.end_geofence_id IS NOT NULL OR d.end_address_id IS NOT NULL)
        ), ch AS (
            SELECT COALESCE('g' || geofence_id::text, 'a' || address_id::text) AS k, count(*) AS n
            FROM charging_processes WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
            GROUP BY 1
        ), agg AS (
            SELECT k, min(gid) AS gid, min(aid) AS aid, count(*) AS arrivals,
                   sum(GREATEST(dwell, 0)) AS dwell, max(at) AS last_at
            FROM arrivals GROUP BY k
        )
        SELECT agg.*, COALESCE(ch.n, 0) AS charges, {_place_sql('g', 'a')} AS label,
               g.name AS geofence, a.display_name AS address,
               COALESCE(a.city, a.neighbourhood) AS city,
               COALESCE(g.latitude, a.latitude)::float8 AS lat,
               COALESCE(g.longitude, a.longitude)::float8 AS lon
        FROM agg LEFT JOIN ch ON ch.k = agg.k
        LEFT JOIN geofences g ON g.id = agg.gid
        LEFT JOIN addresses a ON a.id = agg.aid
        ORDER BY agg.arrivals DESC, agg.dwell DESC LIMIT $4
        """,  # noqa: S608
        car_id, lo, hi, int(limit),
    )
    visits = await db.fetch(
        """
        WITH v AS (
            SELECT x.aid, x.ts FROM drives d
            CROSS JOIN LATERAL (VALUES (d.start_address_id, d.start_date),
                                       (d.end_address_id, d.end_date)) AS x(aid, ts)
            WHERE d.car_id = $1 AND d.start_date >= $2 AND d.start_date < $3 AND d.end_date IS NOT NULL
            UNION ALL
            SELECT address_id, start_date FROM charging_processes
            WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
        ), p AS (
            SELECT COALESCE(NULLIF(lower(a.raw #>> '{address,country_code}'), ''),
                            'name:' || lower(a.country)) AS cc,
                   a.country, COALESCE(a.city, a.neighbourhood) AS city,
                   COALESCE(NULLIF(a.state_district, ''), NULLIF(a.state, ''), '') AS region,
                   a.latitude, a.longitude, v.ts,
                   (v.ts AT TIME ZONE 'UTC' AT TIME ZONE $4)::date AS day
            FROM v JOIN addresses a ON a.id = v.aid
            WHERE a.country IS NOT NULL AND COALESCE(a.city, a.neighbourhood) IS NOT NULL
        )
        SELECT cc, min(country) AS country, city, region, min(ts) AS first, max(ts) AS last,
               count(DISTINCT day) AS days, count(*) AS visits,
               avg(latitude)::float8 AS lat, avg(longitude)::float8 AS lon
        FROM p GROUP BY cc, city, region ORDER BY days DESC, visits DESC
        """,
        car_id, lo, hi, tz,
    )
    countries: dict[str, dict] = {}
    for v in visits:
        c = countries.setdefault(v["cc"], {"country_code": v["cc"], "country": v["country"],
                                           "cities": 0, "visits": 0, "first": v["first"],
                                           "last": v["last"]})
        c["cities"] += 1
        c["visits"] += v["visits"]
        c["first"] = min(c["first"], v["first"])
        c["last"] = max(c["last"], v["last"])
    days_total = await db.fetchval(
        """
        SELECT count(DISTINCT (ts AT TIME ZONE 'UTC' AT TIME ZONE $4)::date) FROM (
            SELECT start_date AS ts FROM drives WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
            UNION ALL SELECT start_date FROM charging_processes
            WHERE car_id = $1 AND start_date >= $2 AND start_date < $3
        ) x
        """,
        car_id, lo, hi, tz,
    )
    return {
        "car": car, "period": period.as_dict(), "tz": tz,
        "counts": {k: int(counts[k] or 0) for k in ("addresses", "cities", "states", "countries")}
        if counts else {},
        "top_places": [
            {"label": p["label"], "geofence_id": p["gid"], "geofence": p["geofence"],
             "address": p["address"], "city": p["city"], "lat": p["lat"], "lon": p["lon"],
             "arrivals": p["arrivals"], "charges": p["charges"],
             "dwell_min": round(float(p["dwell"] or 0)), "last_visit": iso(p["last_at"])}
            for p in places
        ],
        "cities": [
            {"country_code": v["cc"], "country": v["country"], "city": v["city"],
             "region": v["region"] or None, "first_visit": iso(v["first"]),
             "last_visit": iso(v["last"]), "days": v["days"], "visits": v["visits"],
             "lat": v["lat"], "lon": v["lon"]}
            for v in visits
        ],
        "countries": sorted(
            [{**c, "first": iso(c["first"]), "last": iso(c["last"])} for c in countries.values()],
            key=lambda c: -c["visits"]),
        "summary": {"countries": len(countries), "cities": len(visits), "days": int(days_total or 0)},
    }


# ======================================================================== 足迹

_FOOTPRINT_CACHE: dict[tuple, tuple[float, dict]] = {}
_FOOTPRINT_TTL_S = 600


async def footprint(car_id: int, period: Period, cell_deg: float = 0.01) -> dict:
    """一生足迹：车开过的地方，按约 1 km 的格子点亮。

    格子 = (floor(lat / cell), floor(lon / cell))，每格带首次到达时间，前端按时间顺序逐格
    点亮。面积按每格自己的纬度算（经向乘 cos(lat)），高纬度的格子更小。城市、地区、国家
    沿用 locations() 的口径，另给一条「第一次」时间线：每座城市、每个地区第一次到达的时间。
    """
    key = (car_id, period.as_dict().get("from"), period.as_dict().get("to"), cell_deg)
    hit = _FOOTPRINT_CACHE.get(key)
    if hit and (datetime.now(UTC).timestamp() - hit[0]) < _FOOTPRINT_TTL_S:
        return hit[1]
    lo, hi = period.bounds()
    rows = await db.fetch(
        """
        SELECT floor(latitude / $4)::int AS y, floor(longitude / $4)::int AS x, min(date) AS first
        FROM positions
        WHERE car_id = $1 AND date >= $2 AND date < $3 AND latitude IS NOT NULL AND longitude IS NOT NULL
        GROUP BY 1, 2 ORDER BY 3
        """,
        car_id, lo, hi, cell_deg,
    )
    km = 111.32 * cell_deg
    area = sum(km * km * math.cos(math.radians((r["y"] + 0.5) * cell_deg)) for r in rows)
    loc = await locations(car_id, period, limit=12)
    regions: dict[tuple, dict] = {}
    for c in loc["cities"]:
        k = (c["country_code"], c["region"] or "")
        g = regions.setdefault(k, {"country_code": c["country_code"], "country": c["country"],
                                   "region": c["region"], "cities": 0, "visits": 0,
                                   "first_visit": c["first_visit"], "days": 0})
        g["cities"] += 1
        g["visits"] += c["visits"]
        g["days"] += c["days"]
        if c["first_visit"] and (not g["first_visit"] or c["first_visit"] < g["first_visit"]):
            g["first_visit"] = c["first_visit"]
    firsts = sorted(
        [{"kind": "city", "name": c["city"], "region": c["region"], "country": c["country"],
          "at": c["first_visit"]} for c in loc["cities"] if c["first_visit"]]
        + [{"kind": "region", "name": r["region"], "country": r["country"], "at": r["first_visit"]}
           for r in regions.values() if r["region"] and r["first_visit"]],
        key=lambda f: f["at"])
    ys = [r["y"] for r in rows]
    xs = [r["x"] for r in rows]
    out = {
        "car": loc["car"], "period": period.as_dict(), "cell_deg": cell_deg,
        "cells": [[r["y"], r["x"], int(r["first"].replace(tzinfo=UTC).timestamp())] for r in rows],
        "bounds": ([round(v, 6) for v in (min(ys) * cell_deg, min(xs) * cell_deg,
                                          (max(ys) + 1) * cell_deg, (max(xs) + 1) * cell_deg)]
                   if rows else None),
        "explored_km2": round(area, 1),
        "counts": {"cells": len(rows), "cities": len(loc["cities"]),
                   "regions": sum(1 for r in regions.values() if r["region"]),
                   "countries": len(loc["countries"])},
        "regions": sorted(regions.values(), key=lambda r: -r["days"]),
        "countries": loc["countries"],
        "cities": loc["cities"],
        "top_places": loc["top_places"],
        "firsts": firsts,
        "first_drive": iso(await db.fetchval(
            "SELECT min(start_date) FROM drives WHERE car_id = $1 AND start_date >= $2 AND start_date < $3",
            car_id, lo, hi)),
    }
    _FOOTPRINT_CACHE[key] = (datetime.now(UTC).timestamp(), out)
    if len(_FOOTPRINT_CACHE) > 32:
        _FOOTPRINT_CACHE.pop(next(iter(_FOOTPRINT_CACHE)))
    return out


# ======================================================================== 软件更新

async def updates(car_id: int) -> dict:
    """软件更新史 —— 对齐 Updates 面板（updates.json）。

    每个版本：安装耗时、在这个版本上待了几天、期间充了几次电、期间的平均 100% 续航
    （判断"这次更新有没有动续航"）。另给中位更新间隔。
    """
    pr = await preferred_range()
    s_col, e_col, _ = _range_cols(pr)
    car = await car_meta(car_id)
    ups = await db.fetch(
        "SELECT id, start_date, end_date, version FROM updates WHERE car_id = $1 ORDER BY start_date",
        car_id,
    )
    rng = await db.fetch(
        f"""
        SELECT date, r, u, is_charge FROM (
            SELECT d.start_date AS date, d.{s_col} AS r, p.usable_battery_level AS u, false AS is_charge
            FROM drives d JOIN positions p ON p.id = d.start_position_id
            WHERE d.car_id = $1 AND p.usable_battery_level > 0
            UNION ALL
            SELECT end_date, {e_col}, end_battery_level, true FROM charging_processes
            WHERE car_id = $1 AND end_date IS NOT NULL AND end_battery_level > 0
        ) x ORDER BY date
        """,  # noqa: S608
        car_id,
    )
    now = utcnow()
    out = []
    gaps = []
    for i, u in enumerate(ups):
        nxt = ups[i + 1]["start_date"] if i + 1 < len(ups) else now
        sel = [x for x in rng if u["start_date"] <= x["date"] < nxt and x["r"] is not None and x["u"]]
        r100 = (sum(float(x["r"]) for x in sel) / sum(float(x["u"]) for x in sel) * 100) if sel else None
        if i:
            gaps.append((u["start_date"] - ups[i - 1]["start_date"]).total_seconds() / 86400)
        ver = (u["version"] or "").split(" ")
        out.append({
            "id": u["id"], "version": ver[0] or None, "build": ver[1] if len(ver) > 1 else None,
            "start": iso(u["start_date"]), "end": iso(u["end_date"]),
            "install_min": _f((u["end_date"] - u["start_date"]).total_seconds() / 60, 0)
            if u["end_date"] else None,
            # 开始=结束：TeslaMate 补记的"错过的更新"（没赶上看到安装过程）
            "observed_install": bool(u["end_date"] and u["end_date"] > u["start_date"]),
            "days_on_version": _f((nxt - u["start_date"]).total_seconds() / 86400, 1),
            "charges": sum(1 for x in sel if x["is_charge"]),
            "range_100_km_avg": _f(r100, 1),
            "current": i == len(ups) - 1,
        })
    return {"car": car, "preferred_range": pr, "count": len(out),
            "median_days_between": _f(_stats.median(gaps), 1) if gaps else None,
            "current_version": out[-1]["version"] if out else None,
            "updates": list(reversed(out))}


# ======================================================================== 电量曲线

_BUCKETS_S = (120, 300, 900, 1800, 3600, 7200, 14400, 43200, 86400)


async def charge_level(car_id: int, period: Period, max_points: int = 600) -> dict:
    """电量历史 —— 对齐 Charge Level 面板（charge-level.json）。

    完整位置行按时间分桶平均 battery_level / usable_battery_level；桶宽自动选，
    让点数不超过 max_points。另给窗口内的分位数（7.5 / 50 / 92.5%，面板同款）与
    在 80% 以上 / 20% 以下停留的时间占比（按点数近似）。
    """
    car = await car_meta(car_id)
    lo, hi = period.bounds()
    span = ((period.end or utcnow()) - (period.start or lo)).total_seconds()
    width = next((b for b in _BUCKETS_S if span / b <= max_points), _BUCKETS_S[-1])
    rows = await db.fetch(
        """
        SELECT to_timestamp(floor(extract(epoch FROM date) / $4) * $4) AT TIME ZONE 'UTC' AS t,
               avg(battery_level) AS soc, avg(usable_battery_level) AS usable, count(*) AS n
        FROM positions
        WHERE car_id = $1 AND date >= $2 AND date < $3 AND ideal_battery_range_km IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        car_id, lo, hi, float(width),
    )
    q = await db.fetchrow(
        """
        SELECT percentile_cont(0.075) WITHIN GROUP (ORDER BY battery_level) AS p7,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY battery_level) AS p50,
               percentile_cont(0.925) WITHIN GROUP (ORDER BY battery_level) AS p92,
               avg(battery_level) AS avg,
               avg((battery_level > 80)::int) AS above80, avg((battery_level < 20)::int) AS below20
        FROM positions
        WHERE car_id = $1 AND date >= $2 AND date < $3 AND ideal_battery_range_km IS NOT NULL
        """,
        car_id, lo, hi,
    )
    return {
        "car": car, "period": period.as_dict(), "bucket_s": width,
        "limits": {"lower": 20, "upper": 100 if car["lfp_battery"] else 80},
        "points": [{"t": iso(r["t"]), "soc": _f(r["soc"], 1), "usable_soc": _f(r["usable"], 1)}
                   for r in rows],
        "stats": {"avg": _f(q["avg"], 1), "p7_5": _f(q["p7"], 1), "median": _f(q["p50"], 1),
                  "p92_5": _f(q["p92"], 1),
                  "above_80_pct": _f((q["above80"] or 0) * 100, 1) if q["avg"] is not None else None,
                  "below_20_pct": _f((q["below20"] or 0) * 100, 1) if q["avg"] is not None else None}
        if q else {},
    }
