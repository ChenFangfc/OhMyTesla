"""首页需要的一切，一个接口给完。

设计文档要求首页在 2–3 秒内回答五个问题（§10），所以它不能靠前端串行
打五个接口拼出来。这里把车辆状态、今日能耗、充电、异常项一次算好。

只回原始值和稳定的状态码，不回成品文案 —— 文案由前端按语言取。
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from .. import db
from ..livestate import live

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["overview"])

# 胎压偏离多少算异常。Cybertruck 冷胎标准 ~50 psi，普通车 42 psi，
# 所以用「与四轮中位数的偏差」判断，而不是绝对阈值——避免误报。
TPMS_DEVIATION_BAR = 0.28   # ≈ 4 psi


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _car_state(c: dict) -> str:
    """把一堆布尔位收敛成一个状态码，前端只认这一个。"""
    if c.get("shift_state") in ("D", "R", "N") or _f(c.get("speed")):
        return "driving"
    if c.get("charging_state") == "Charging":
        return "charging"
    raw = (c.get("state") or "").lower()
    if raw in ("asleep", "suspended"):
        return "asleep"
    if raw == "offline":
        return "offline"
    if raw in ("online", "driving", "charging"):
        return "online"
    return raw or "unknown"


async def _today_energy(car_id: int, tz_offset_min: int) -> dict:
    """今天开了多远、用了多少电。按用户本地时区切一天，不是 UTC。"""
    now = datetime.now(timezone.utc)
    local_now = now + timedelta(minutes=tz_offset_min)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = (local_midnight - timedelta(minutes=tz_offset_min)).replace(tzinfo=None)

    row = await db.fetchrow(
        """
        SELECT coalesce(sum(distance), 0)                AS km,
               coalesce(sum(
                 CASE WHEN start_rated_range_km > end_rated_range_km
                      THEN (start_rated_range_km - end_rated_range_km) * c.efficiency
                      ELSE 0 END), 0)                    AS kwh,
               count(*)                                  AS drives
        FROM drives d JOIN cars c ON c.id = d.car_id
        WHERE d.car_id = $1 AND d.start_date >= $2 AND d.distance IS NOT NULL
        """,
        car_id, since,
    )
    km = _f(row["km"]) or 0.0
    kwh = _f(row["kwh"]) or 0.0
    return {
        "km": round(km, 1),
        "kwh": round(kwh, 2),
        "drives": int(row["drives"] or 0),
        # Wh/km —— 前端按单位偏好换成 Wh/mi
        "wh_per_km": round(kwh * 1000 / km, 1) if km > 0.5 and kwh > 0 else None,
    }


async def _charging(car_id: int, c: dict) -> dict | None:
    """在充就回实时功率与预计完成；不在充就回上一次会话。"""
    if c.get("charging_state") == "Charging":
        mins = _f(c.get("time_to_full_charge"))
        return {
            "active": True,
            "power_kw": _f(c.get("charger_power")),
            "limit_soc": _f(c.get("charge_limit_soc")),
            "minutes_remaining": round(mins * 60) if mins else None,
            "added_kwh": _f(c.get("charge_energy_added")),
        }
    row = await db.fetchrow(
        "SELECT id, end_date, charge_energy_added, charge_energy_used, end_battery_level,"
        " (SELECT name FROM geofences g WHERE g.id = cp.geofence_id) AS place"
        " FROM charging_processes cp WHERE car_id = $1 AND end_date IS NOT NULL"
        " ORDER BY end_date DESC LIMIT 1",
        car_id,
    )
    if row is None:
        return None
    # 费用走唯一真源（TeslaMate 记录的 > 电价引擎算的），与 Energy 页同一个数
    from ..insights import costs as cost_source

    ci = (await cost_source.session_costs(car_id)).get(row["id"]) or {}
    return {
        "active": False,
        "ended_at": row["end_date"].replace(tzinfo=timezone.utc).timestamp(),
        "added_kwh": _f(row["charge_energy_added"]),
        "used_kwh": _f(row["charge_energy_used"]),
        "cost": ci.get("cost"),
        "cost_source": ci.get("source"),
        "end_soc": _f(row["end_battery_level"]),
        "place": row["place"],
    }


def _health(c: dict) -> dict:
    """只报异常（§36）。一切正常时前端显示一行 Normal，不铺开每个传感器。"""
    issues = []

    corners = {k: _f(c.get(f"tpms_pressure_{k}")) for k in ("fl", "fr", "rl", "rr")}
    known = {k: v for k, v in corners.items() if v is not None and v > 0.5}
    if len(known) == 4:
        vals = sorted(known.values())
        median = (vals[1] + vals[2]) / 2
        for corner, bar in known.items():
            if median - bar >= TPMS_DEVIATION_BAR:
                issues.append({
                    "kind": "tpms_low", "corner": corner,
                    "value_bar": round(bar, 2), "expected_bar": round(median, 2),
                })

    for key, kind in (("tpms_soft_warning_fl", "fl"), ("tpms_soft_warning_fr", "fr"),
                      ("tpms_soft_warning_rl", "rl"), ("tpms_soft_warning_rr", "rr")):
        if str(c.get(key)).lower() == "true":  # noqa: SIM102 - 分开写是为了让两个条件各自带注释说明
            if not any(i.get("corner") == kind for i in issues):
                issues.append({"kind": "tpms_warning", "corner": kind})

    if str(c.get("update_available")).lower() == "true":
        issues.append({"kind": "update", "version": c.get("update_version") or ""})

    return {"ok": not issues, "issues": issues}


@router.get("/overview")
async def overview(car_id: int | None = None, tz_offset_min: int = 0):
    cars = live.snapshot()
    if not cars:
        return {"connected": False, "car": None}

    # livestate 在 VIN→car_id 映射还没建好时会造 "vin:XXX" 占位键
    # （见 livestate._car_key_for_vin）。直接 int() 会 ValueError → 500，
    # 而这条路正是 Fleet Telemetry 抢在车辆表可读之前到达时会走的。
    numeric = [k for k in cars if k.isdigit()]
    if not numeric:
        return {"connected": False, "car": None}
    if car_id is not None and str(car_id) in cars:
        key = str(car_id)
    else:
        # 没指定车时取"最近活跃、未归档"的那辆，而不是字典里的第一辆：
        # 三车账号里第一辆可能是早已离开账号、只剩数据库残影的车。
        key = numeric[0]
        try:
            from ..insights import cars as registry

            best = await registry.default_car_id()
            if best is not None and str(best) in cars:
                key = str(best)
        except Exception:  # 选不出来就用老办法，首页不能因此挂掉
            log.debug("default car lookup failed", exc_info=True)
    c = cars[key]
    cid = int(key)

    soc = _f(c.get("usable_battery_level")) or _f(c.get("battery_level"))
    seed_age = c.get("_seed_age")

    # 名字/车型在设置页改完后不会立刻从 MQTT 回来（TeslaMate 只在变化时发布），
    # 所以直接问数据库要一次，否则用户改了名首页要等下一次上报才更新
    name, model = c.get("display_name"), c.get("model")
    if not name or not model:
        try:
            row = await db.fetchrow(
                "SELECT name, model, marketing_name FROM cars WHERE id = $1", cid)
            if row:
                name = name or row["name"] or row["marketing_name"]
                model = model or row["model"]
        except Exception:
            log.debug("suppressed", exc_info=True)

    archived = None
    last_seen = None
    try:
        from ..insights import cars as registry

        meta = await registry.get_car(cid)
        if meta:
            archived, last_seen = meta["archived"], meta["last_seen"]
    except Exception:
        log.debug("car registry unavailable", exc_info=True)

    out = {
        "connected": True,
        "car_id": cid,
        # 已离开账号的车：界面应提示"历史数据"，而不是把数据库残影当实时状态
        "archived": archived,
        "last_seen": last_seen,
        "name": name or None,
        "model": model or None,
        "state": _car_state(c),
        # 数据是数据库补的还是 MQTT 实时的 —— 界面必须说清楚（§32）
        "stale_seconds": int(seed_age) if seed_age else None,
        "soc": soc,
        "range_km": _f(c.get("rated_battery_range_km")),
        "inside_temp": _f(c.get("inside_temp")),
        "outside_temp": _f(c.get("outside_temp")),
        "odometer_km": _f(c.get("odometer")),
        "place": c.get("geofence") or None,
        "latitude": _f(c.get("latitude")),
        "longitude": _f(c.get("longitude")),
        "since": c.get("since"),
        "version": c.get("version"),
        # 三态：true / false / null(不知道)。null 时界面必须隐藏而不是显示 false
        "locked": _tri(c.get("locked")),
        "sentry": _tri(c.get("sentry_mode")),
        "climate": _tri(c.get("is_climate_on")),
        "charge_port_open": _tri(c.get("charge_port_door_open")),
        "windows_open": _tri(c.get("windows_open")),
        "doors_open": _tri(c.get("doors_open")),
        "charging": None, "today": None, "health": {"ok": True, "issues": []},
    }

    try:
        out["charging"] = await _charging(cid, c)
        out["today"] = await _today_energy(cid, tz_offset_min)
    except db.DbUnavailable as e:
        log.info("overview: db unavailable (%s)", e)
    except Exception:
        log.exception("overview aggregation failed")

    out["health"] = _health(c)
    return out


def _tri(v) -> bool | None:
    if v is None:
        return None
    s = str(v).lower()
    if s in ("true", "1"):
        return True
    if s in ("false", "0"):
        return False
    return None
