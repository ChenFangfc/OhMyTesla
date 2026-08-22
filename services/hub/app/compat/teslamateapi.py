"""TeslaMateApi 兼容层。

为什么要有它：MateDroid、T-Buddy、Tesstats、各种 Scriptable 组件和桌面小屏，
全都把「先部署 tobiasehlert/teslamateapi」写进了自己的 Requirements。那套
JSON 形状事实上已经是移动端的 API 契约。实现它，那些现成的 App 就能直接连
OhmyTesla，不用我们自己写客户端 —— 这是整份竞品调研里性价比最高的一条。

**形状是照着 teslamateapi v1.25.0 的 Go 结构体逐字段抄的**（tag 名、嵌套层级、
`data` 包裹），不是照着文档猜的。字段少一个，客户端那边就是一个 null。

不实现的部分，以及为什么：
- `POST /command/*` 与 `wake_up`：teslamateapi 自己走的是已经死掉的 Owner API，
  维护者在 issue #415 里已经说要废弃。OhmyTesla 有自己的指令通道
  （/api/tesla/command，带风险确认与流水），不在这里开第二个口子。
- `PUT /logging/*`：暂停/恢复采集要写 TeslaMate 的内部状态，而它把设置缓存在
  GenServer 里，裸写库不生效。
两者都返回 501 并说明原因，而不是假装成功。

鉴权：沿用全站的 Basic 认证；另外接受 `Authorization: Bearer <API_TOKEN>` 或
`X-Api-Token`，因为移动端 App 只会发这两种。token 由 TESLAMATEAPI_TOKEN 配置，
不配就只能走 Basic。
"""

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request

from .. import db
from ..config import settings
from ..insights import queries
from ..livestate import live

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["teslamateapi-compat"])


async def _units() -> dict:
    """teslamateapi 用短代号表示单位偏好，跟着门户的偏好走。"""
    from ..i18n.prefs import get as _get_prefs

    p = await _get_prefs()
    return {
        "unit_of_length": "mi" if p.get("length") == "mi" else "km",
        "unit_of_temperature": "F" if p.get("temp") == "F" else "C",
        "unit_of_pressure": {"psi": "psi", "kpa": "kPa"}.get(p.get("pressure"), "bar"),
    }


def _check_token(request: Request) -> None:
    """移动端 App 只会发 Bearer / X-Api-Token。

    全站中间件已经放行了（能走到这里说明 Basic 认证过了，或者用户没设密码
    —— 后者根本进不来，中间件会 503）。这里再校一次是给「Basic 密码给了家人、
    但不想让他们的 App 拿到 API token」这种分权场景用的：配了
    TESLAMATEAPI_TOKEN 就必须带对，没配则跳过。
    """
    want = getattr(settings, "teslamateapi_token", "") or ""
    if not want:
        return
    got = request.headers.get("x-api-token", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        got = auth[7:].strip()
    if not got or not secrets.compare_digest(got.encode(), want.encode()):
        raise HTTPException(401, "invalid API token")


def _dur(minutes) -> str:
    """teslamateapi 的 duration_str 是 "1h30min" 这种形状。"""
    m = int(minutes or 0)
    return f"{m // 60}h{m % 60}min" if m >= 60 else f"{m}min"


def _f(v):
    return float(v) if v is not None else None


async def _car_row(car_id: int):
    row = await db.fetchrow(
        """
        SELECT c.id, c.name, c.eid, c.vid, c.vin, c.model, c.trim_badging, c.efficiency,
               c.exterior_color, c.spoiler_type, c.wheel_type, c.inserted_at, c.updated_at,
               s.suspend_min, s.suspend_after_idle_min, s.req_not_unlocked,
               s.free_supercharging, s.use_streaming_api
        FROM cars c LEFT JOIN car_settings s ON s.id = c.settings_id
        WHERE c.id = $1
        """,
        car_id,
    )
    if row is None:
        raise HTTPException(404, "car not found")
    return row


def _car_stub(row) -> dict:
    return {"car_id": row["id"], "car_name": row["name"] or f"Car {row['id']}"}


# ---------------------------------------------------------------- 车辆

@router.get("/v1/cars")
async def cars_v1(request: Request):
    _check_token(request)
    rows = await db.fetch(
        """
        SELECT c.*, s.suspend_min, s.suspend_after_idle_min, s.req_not_unlocked,
               s.free_supercharging, s.use_streaming_api,
               (SELECT count(*) FROM charging_processes WHERE car_id = c.id) AS n_charges,
               (SELECT count(*) FROM drives             WHERE car_id = c.id) AS n_drives,
               (SELECT count(*) FROM updates            WHERE car_id = c.id) AS n_updates
        FROM cars c LEFT JOIN car_settings s ON s.id = c.settings_id
        ORDER BY c.id
        """
    )
    return {"data": {"cars": [
        {
            "car_id": r["id"],
            "name": r["name"] or f"Car {r['id']}",
            "car_details": {
                "eid": r["eid"], "vid": r["vid"], "vin": r["vin"],
                "model": r["model"], "trim_badging": r["trim_badging"],
                "efficiency": _f(r["efficiency"]),
            },
            "car_exterior": {
                "exterior_color": r["exterior_color"],
                "spoiler_type": r["spoiler_type"],
                "wheel_type": r["wheel_type"],
            },
            "car_settings": {
                "suspend_min": r["suspend_min"],
                "suspend_after_idle_min": r["suspend_after_idle_min"],
                "req_not_unlocked": r["req_not_unlocked"],
                "free_supercharging": r["free_supercharging"],
                "use_streaming_api": r["use_streaming_api"],
            },
            "teslamate_details": {
                "inserted_at": queries._iso(r["inserted_at"]),
                "updated_at": queries._iso(r["updated_at"]),
            },
            "teslamate_stats": {
                "total_charges": r["n_charges"],
                "total_drives": r["n_drives"],
                "total_updates": r["n_updates"],
            },
        }
        for r in rows
    ]}}


@router.get("/v1/cars/{car_id}")
async def car_v1(car_id: int, request: Request):
    _check_token(request)
    all_cars = await cars_v1(request)
    hit = [c for c in all_cars["data"]["cars"] if c["car_id"] == car_id]
    if not hit:
        raise HTTPException(404, "car not found")
    return {"data": {"cars": hit}}


# ---------------------------------------------------------------- 实时状态

def _b(v):
    """MQTT 载荷里的布尔是字符串 "true"/"false"。"""
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    return str(v).lower() == "true"


def _n(v):
    if v is None or v == "":
        return None
    try:
        return float(v) if "." in str(v) else int(v)
    except (TypeError, ValueError):
        return v


@router.get("/v1/cars/{car_id}/status")
async def status_v1(car_id: int, request: Request):
    """实时状态。数据来自 MQTT retained 快照，和 teslamateapi 同源。"""
    _check_token(request)
    row = await _car_row(car_id)
    c = live.cars.get(str(car_id)) or {}
    if not c:
        raise HTTPException(503, "no live state for this car yet")

    g = c.get
    return {"data": {
        "car": _car_stub(row),
        "status": {
            "display_name": g("display_name") or row["name"],
            "state": g("state"),
            "state_since": g("since"),
            "odometer": _n(g("odometer")),
            "car_status": {
                "healthy": _b(g("healthy")), "locked": _b(g("locked")),
                "sentry_mode": _b(g("sentry_mode")),
                "windows_open": _b(g("windows_open")), "doors_open": _b(g("doors_open")),
                "driver_front_door_open": _b(g("driver_front_door_open")),
                "driver_rear_door_open": _b(g("driver_rear_door_open")),
                "passenger_front_door_open": _b(g("passenger_front_door_open")),
                "passenger_rear_door_open": _b(g("passenger_rear_door_open")),
                "trunk_open": _b(g("trunk_open")), "frunk_open": _b(g("frunk_open")),
                "is_user_present": _b(g("is_user_present")),
                "center_display_state": _n(g("center_display_state")),
            },
            "car_details": {"model": row["model"], "trim_badging": row["trim_badging"]},
            "car_exterior": {
                "exterior_color": row["exterior_color"],
                "spoiler_type": row["spoiler_type"], "wheel_type": row["wheel_type"],
            },
            "car_geodata": {
                "geofence": g("geofence"), "location": g("location"),
                "latitude": _n(g("latitude")), "longitude": _n(g("longitude")),
            },
            "car_versions": {
                "version": g("version"), "update_available": _b(g("update_available")),
                "update_version": g("update_version"),
            },
            "driving_details": {
                "active_route": g("active_route"),
                "active_route_destination": g("active_route_destination"),
                "active_route_latitude": _n(g("active_route_latitude")),
                "active_route_longitude": _n(g("active_route_longitude")),
                "shift_state": g("shift_state"), "power": _n(g("power")),
                "speed": _n(g("speed")), "heading": _n(g("heading")),
                "elevation": _n(g("elevation")),
            },
            "climate_details": {
                "is_climate_on": _b(g("is_climate_on")),
                "inside_temp": _n(g("inside_temp")), "outside_temp": _n(g("outside_temp")),
                "is_preconditioning": _b(g("is_preconditioning")),
                "climate_keeper_mode": g("climate_keeper_mode"),
            },
            "battery_details": {
                "est_battery_range": _n(g("est_battery_range_km")),
                "rated_battery_range": _n(g("rated_battery_range_km")),
                "ideal_battery_range": _n(g("ideal_battery_range_km")),
                "battery_level": _n(g("battery_level")),
                "usable_battery_level": _n(g("usable_battery_level")),
            },
            "charging_details": {
                "plugged_in": _b(g("plugged_in")), "charging_state": g("charging_state"),
                "charge_energy_added": _n(g("charge_energy_added")),
                "charge_limit_soc": _n(g("charge_limit_soc")),
                "charge_port_door_open": _b(g("charge_port_door_open")),
                "charger_actual_current": _n(g("charger_actual_current")),
                "charger_phases": _n(g("charger_phases")),
                "charger_power": _n(g("charger_power")),
                "charger_voltage": _n(g("charger_voltage")),
                "charge_current_request": _n(g("charge_current_request")),
                "charge_current_request_max": _n(g("charge_current_request_max")),
                "scheduled_charging_start_time": g("scheduled_charging_start_time"),
                "time_to_full_charge": _n(g("time_to_full_charge")),
            },
            "tpms_details": {
                "tpms_pressure_fl": _n(g("tpms_pressure_fl")),
                "tpms_pressure_fr": _n(g("tpms_pressure_fr")),
                "tpms_pressure_rl": _n(g("tpms_pressure_rl")),
                "tpms_pressure_rr": _n(g("tpms_pressure_rr")),
                "tpms_soft_warning_fl": _b(g("tpms_soft_warning_fl")),
                "tpms_soft_warning_fr": _b(g("tpms_soft_warning_fr")),
                "tpms_soft_warning_rl": _b(g("tpms_soft_warning_rl")),
                "tpms_soft_warning_rr": _b(g("tpms_soft_warning_rr")),
            },
        },
        "units": await _units(),
    }}


# ---------------------------------------------------------------- 电池健康

@router.get("/v1/cars/{car_id}/battery-health")
async def battery_health_v1(car_id: int, request: Request):
    _check_token(request)
    row = await _car_row(car_id)
    h = await queries.battery_health(car_id)
    eff = _f(row["efficiency"])
    # 键名以 insights.queries.battery_health 的实际返回为准
    cur_range = h.get("current_range_at_100")
    max_range = h.get("max_range_at_100")
    return {"data": {
        "car": _car_stub(row),
        "battery_health": {
            "max_range": max_range,
            "current_range": cur_range,
            "max_capacity": round(max_range * eff, 2) if (max_range and eff) else None,
            "current_capacity": h.get("current_capacity_kwh")
                                or (round(cur_range * eff, 2) if (cur_range and eff) else None),
            "rated_efficiency": eff,
            "battery_health_percentage": (
                round(100 - h["degradation_pct"], 2) if h.get("degradation_pct") is not None else None
            ),
        },
        "units": {k: v for k, v in (await _units()).items() if k != "unit_of_pressure"},
    }}


# ---------------------------------------------------------------- 行程

@router.get("/v1/cars/{car_id}/drives")
async def drives_v1(car_id: int, request: Request, limit: int = 100):
    _check_token(request)
    row = await _car_row(car_id)
    eff = _f(row["efficiency"]) or 0
    rows = await db.fetch(
        """
        SELECT d.id, d.start_date, d.end_date, d.duration_min, d.distance,
               d.speed_max, d.power_max, d.power_min,
               d.start_km, d.end_km, d.outside_temp_avg, d.inside_temp_avg,
               d.start_ideal_range_km, d.end_ideal_range_km,
               d.start_rated_range_km, d.end_rated_range_km,
               sa.display_name AS start_address, ea.display_name AS end_address,
               ps.battery_level AS s_bl, ps.usable_battery_level AS s_ubl,
               pe.battery_level AS e_bl, pe.usable_battery_level AS e_ubl
        FROM drives d
        LEFT JOIN addresses sa ON sa.id = d.start_address_id
        LEFT JOIN addresses ea ON ea.id = d.end_address_id
        LEFT JOIN positions ps ON ps.id = d.start_position_id
        LEFT JOIN positions pe ON pe.id = d.end_position_id
        WHERE d.car_id = $1 ORDER BY d.start_date DESC LIMIT $2
        """,
        car_id, int(limit),
    )
    drives = []
    for r in rows:
        dist = _f(r["distance"]) or 0
        rated_used = max((_f(r["start_rated_range_km"]) or 0) - (_f(r["end_rated_range_km"]) or 0), 0)
        kwh = round(rated_used * eff, 2) if eff else None
        drives.append({
            "drive_id": r["id"],
            "start_date": queries._iso(r["start_date"]),
            "end_date": queries._iso(r["end_date"]),
            "start_address": r["start_address"], "end_address": r["end_address"],
            "odometer_details": {
                "odometer_start": _f(r["start_km"]), "odometer_end": _f(r["end_km"]),
                "odometer_distance": round(dist, 2),
            },
            "duration_min": r["duration_min"], "duration_str": _dur(r["duration_min"]),
            "speed_max": r["speed_max"],
            "speed_avg": round(dist / (r["duration_min"] / 60), 1)
                         if r["duration_min"] else None,
            "power_max": r["power_max"], "power_min": r["power_min"],
            "battery_details": {
                "start_usable_battery_level": r["s_ubl"], "start_battery_level": r["s_bl"],
                "end_usable_battery_level": r["e_ubl"], "end_battery_level": r["e_bl"],
                # teslamate 用这两个标记「起点电量被冷保护压低了」
                "reduced_range": bool(r["s_ubl"] is not None and r["s_bl"] is not None
                                      and r["s_ubl"] < r["s_bl"]),
                "is_sufficiently_precise": bool(r["s_bl"] is not None and r["e_bl"] is not None),
            },
            "range_ideal": {
                "start_range": _f(r["start_ideal_range_km"]),
                "end_range": _f(r["end_ideal_range_km"]),
                "range_diff": round((_f(r["start_ideal_range_km"]) or 0)
                                    - (_f(r["end_ideal_range_km"]) or 0), 2),
            },
            "range_rated": {
                "start_range": _f(r["start_rated_range_km"]),
                "end_range": _f(r["end_rated_range_km"]),
                "range_diff": round(rated_used, 2),
            },
            "outside_temp_avg": _f(r["outside_temp_avg"]),
            "inside_temp_avg": _f(r["inside_temp_avg"]),
            "energy_consumed_net": kwh,
            "consumption_net": round(kwh * 1000 / dist, 1) if (kwh and dist > 0) else None,
        })
    return {"data": {"car": _car_stub(row), "drives": drives,
                     "units": {k: v for k, v in (await _units()).items() if k != "unit_of_pressure"}}}


@router.get("/v1/cars/{car_id}/drives/{drive_id}")
async def drive_detail_v1(car_id: int, drive_id: int, request: Request):
    _check_token(request)
    all_d = await drives_v1(car_id, request, limit=100000)
    hit = [d for d in all_d["data"]["drives"] if d["drive_id"] == drive_id]
    if not hit:
        raise HTTPException(404, "drive not found")
    return {"data": {**all_d["data"], "drives": hit}}


# ---------------------------------------------------------------- 充电

async def _charges(car_id: int, limit: int, only_open: bool = False) -> list[dict]:
    rows = await db.fetch(
        f"""
        SELECT cp.id, cp.start_date, cp.end_date, cp.duration_min,
               cp.charge_energy_added, cp.charge_energy_used, cp.cost,
               cp.start_battery_level, cp.end_battery_level,
               cp.start_ideal_range_km, cp.end_ideal_range_km,
               cp.start_rated_range_km, cp.end_rated_range_km,
               cp.outside_temp_avg,
               a.display_name AS address, p.odometer, p.latitude, p.longitude
        FROM charging_processes cp
        LEFT JOIN addresses a ON a.id = cp.address_id
        LEFT JOIN positions p ON p.id = cp.position_id
        WHERE cp.car_id = $1 {"AND cp.end_date IS NULL" if only_open else ""}
        ORDER BY cp.start_date DESC LIMIT $2
        """,  # noqa: S608 - only_open 是本函数的布尔参数，非用户输入
        car_id, int(limit),
    )
    return [
        {
            "charge_id": r["id"],
            "start_date": queries._iso(r["start_date"]),
            "end_date": queries._iso(r["end_date"]),
            "address": r["address"],
            "charge_energy_added": _f(r["charge_energy_added"]),
            "charge_energy_used": _f(r["charge_energy_used"]),
            "cost": _f(r["cost"]),
            "duration_min": r["duration_min"], "duration_str": _dur(r["duration_min"]),
            "battery_details": {
                "start_battery_level": r["start_battery_level"],
                "end_battery_level": r["end_battery_level"],
            },
            "range_ideal": {"start_range": _f(r["start_ideal_range_km"]),
                            "end_range": _f(r["end_ideal_range_km"])},
            "range_rated": {"start_range": _f(r["start_rated_range_km"]),
                            "end_range": _f(r["end_rated_range_km"])},
            "outside_temp_avg": _f(r["outside_temp_avg"]),
            "odometer": _f(r["odometer"]),
            "latitude": _f(r["latitude"]), "longitude": _f(r["longitude"]),
        }
        for r in rows
    ]


@router.get("/v1/cars/{car_id}/charges")
async def charges_v1(car_id: int, request: Request, limit: int = 100):
    _check_token(request)
    row = await _car_row(car_id)
    return {"data": {"car": _car_stub(row), "charges": await _charges(car_id, limit),
                     "units": {k: v for k, v in (await _units()).items() if k != "unit_of_pressure"}}}


@router.get("/v1/cars/{car_id}/charges/current")
async def charge_current_v1(car_id: int, request: Request):
    """正在进行的充电。没有就返回空列表而不是 404 —— 客户端会轮询它。"""
    _check_token(request)
    row = await _car_row(car_id)
    return {"data": {"car": _car_stub(row), "charges": await _charges(car_id, 1, only_open=True),
                     "units": {k: v for k, v in (await _units()).items() if k != "unit_of_pressure"}}}


@router.get("/v1/cars/{car_id}/charges/{charge_id}")
async def charge_detail_v1(car_id: int, charge_id: int, request: Request):
    _check_token(request)
    row = await _car_row(car_id)
    hit = [c for c in await _charges(car_id, 100000) if c["charge_id"] == charge_id]
    if not hit:
        raise HTTPException(404, "charge not found")
    return {"data": {"car": _car_stub(row), "charges": hit,
                     "units": {k: v for k, v in (await _units()).items() if k != "unit_of_pressure"}}}


# ---------------------------------------------------------------- 固件更新

@router.get("/v1/cars/{car_id}/updates")
async def updates_v1(car_id: int, request: Request, limit: int = 100):
    _check_token(request)
    row = await _car_row(car_id)
    rows = await db.fetch(
        "SELECT id, start_date, end_date, version FROM updates"
        " WHERE car_id = $1 ORDER BY start_date DESC LIMIT $2",
        car_id, int(limit),
    )
    return {"data": {"car": _car_stub(row), "updates": [
        {"update_id": r["id"], "start_date": queries._iso(r["start_date"]),
         "end_date": queries._iso(r["end_date"]), "version": r["version"]}
        for r in rows
    ]}}


# ---------------------------------------------------------------- 其它

@router.get("/v1/globalsettings")
async def globalsettings_v1(request: Request):
    _check_token(request)
    return {"data": {"settings": await _units()}}


@router.get("/ping")
async def ping():
    return {"message": "pong"}


@router.get("/readyz")
async def readyz():
    """就绪探针：数据库连得上才算就绪。"""
    try:
        await db.fetchval("SELECT 1")
    except db.DbUnavailable as e:
        raise HTTPException(503, f"database not ready: {e}") from e
    return {"status": "ready"}


_NOT_IMPLEMENTED = (
    "OhmyTesla does not proxy vehicle commands through the TeslaMateApi surface. "
    "teslamateapi routes these to the deprecated Owner API (upstream issue #415). "
    "Use POST /api/tesla/command instead — it has risk confirmation and an audit log."
)


@router.post("/v1/cars/{car_id}/command/{command}")
async def command_not_implemented(car_id: int, command: str):
    raise HTTPException(501, _NOT_IMPLEMENTED)


@router.post("/v1/cars/{car_id}/wake_up")
async def wake_not_implemented(car_id: int):
    raise HTTPException(501, _NOT_IMPLEMENTED)
