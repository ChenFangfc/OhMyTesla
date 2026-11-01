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

单位：**数值按 units 里声明的单位给出**（与上游一致）。TeslaMate 库里永远是
km / °C / bar；上游 teslamateapi 在 unit_of_length == "mi" 时对每个距离/速度/续航
调 kilometersToMiles，unit_of_temperature == "F" 时调 celsiusToFahrenheit，
unit_of_pressure == "psi" 时调 barToPsi。我们声明的单位来自门户偏好，所以换算也
按门户偏好来。以前声明了 mi/F/psi 却原样发 km/°C/bar（审计 F1）：客户端显示
"44,521 mi"、"22.4 °F"、"3.45 psi"。
唯一与上游不同的一处：consumption_net（Wh/km → Wh/mi 应当 ×1.609344，上游用的是
kilometersToMiles，结果小了 2.6 倍）。这里按物理换算。

鉴权（现状，见 insights/NEEDS.md）：全站 Basic 认证中间件（main.py）在请求到达
这里之前就会拒掉没有 Basic 凭据的请求，所以**只带** `Authorization: Bearer` 或
`X-Api-Token` 的 App 目前连不上（审计 F14）。可用的组合：
  1. Basic（用户 ohmytesla + 门户密码）—— 任何 App 都能这么配；
  2. Basic + Bearer/X-Api-Token —— 配了 TESLAMATEAPI_TOKEN 时必须两样都带。
要让纯 Bearer 的客户端可用，需要中间件对 /api/v1/*、/api/ping、/api/readyz
在 token 校验通过时放行（已在 NEEDS.md 写明改法）；_check_token 本身已经支持。
"""

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request

from .. import db
from ..config import settings
from ..insights import queries
from ..insights.guard import http_error
from ..insights.period import clamp
from ..livestate import live

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["teslamateapi-compat"])


def _guard(fn):
    """数据库异常 → 准确的状态码（503 连不上 / 504 超时 / 503 schema / 500 SQL）。

    以前所有 PostgresError 都报"数据库尚未就绪"，连 `?limit=-1` 都是 503。
    TeslaMate 首次启动时客户端拿到的仍然是 503（表不存在），而不是 500 ——
    那看起来是"这个服务坏了"，用户会直接卸载。
    """
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            err = http_error(e)
            if err is None:
                raise
            raise err from e

    return wrapper


async def _units() -> dict:
    """teslamateapi 用短代号表示单位偏好，跟着门户的偏好走。"""
    from ..i18n.prefs import get as _get_prefs

    p = await _get_prefs()
    return {
        "unit_of_length": "mi" if p.get("length") == "mi" else "km",
        "unit_of_temperature": "F" if p.get("temp") == "F" else "C",
        "unit_of_pressure": {"psi": "psi", "kpa": "kPa"}.get(p.get("pressure"), "bar"),
    }


KM_PER_MI = 1.609344
PSI_PER_BAR = 14.503773773


class _Conv:
    """把 TeslaMate 的公制值换成 units 里声明的单位。None 原样返回。"""

    def __init__(self, u: dict):
        self.mi = u.get("unit_of_length") == "mi"
        self.f = u.get("unit_of_temperature") == "F"
        self.pressure = u.get("unit_of_pressure", "bar")

    def dist(self, km, nd: int = 2):
        if km is None:
            return None
        v = float(km) / KM_PER_MI if self.mi else float(km)
        return round(v, nd)

    def speed(self, kmh):
        """整数速度（上游 kilometersToMilesInteger）。"""
        if kmh is None:
            return None
        return round(float(kmh) / KM_PER_MI) if self.mi else round(float(kmh))

    def temp(self, c, nd: int = 1):
        if c is None:
            return None
        v = float(c) * 9 / 5 + 32 if self.f else float(c)
        return round(v, nd)

    def press(self, bar, nd: int = 2):
        if bar is None:
            return None
        if self.pressure == "psi":
            return round(float(bar) * PSI_PER_BAR, nd)
        if self.pressure == "kPa":
            return round(float(bar) * 100, 0)
        return round(float(bar), nd)

    def per_dist(self, wh_per_km, nd: int = 1):
        """Wh/km → Wh/mi：每英里的能耗是每公里的 1.609 倍。"""
        if wh_per_km is None:
            return None
        return round(float(wh_per_km) * KM_PER_MI if self.mi else float(wh_per_km), nd)


def _page(limit: int | None, page: int | None, show: int | None) -> tuple[int, int]:
    """(LIMIT, OFFSET)。上游用 page/show（默认 1/100）；本实现以前用 limit，两种都认。
    负数、零、超大值夹到合理区间，不再变成 SQL 错误。"""
    size = clamp(show if show is not None else (limit if limit is not None else 100), 1, 100000, 100)
    pg = clamp(page if page is not None else 1, 1, 10_000_000, 1)
    return size, (pg - 1) * size


def _date_param(v: str | None):
    """上游的 startDate/endDate（RFC3339 或 YYYY-MM-DD）→ naive UTC。"""
    if not v:
        return None
    from ..insights.period import parse_period

    p = parse_period(from_=v)
    return p.start


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
@_guard
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
@_guard
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
@_guard
async def status_v1(car_id: int, request: Request):
    """实时状态。数据来自 MQTT retained 快照，和 teslamateapi 同源。"""
    _check_token(request)
    row = await _car_row(car_id)
    c = live.cars.get(str(car_id)) or {}
    if not c:
        raise HTTPException(503, "no live state for this car yet")

    g = c.get
    units = await _units()
    cv = _Conv(units)
    return {"data": {
        "car": _car_stub(row),
        "status": {
            "display_name": g("display_name") or row["name"],
            "state": g("state"),
            "state_since": g("since"),
            "odometer": cv.dist(_n(g("odometer"))),
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
                "speed": cv.speed(_n(g("speed"))), "heading": _n(g("heading")),
                "elevation": _n(g("elevation")),
            },
            "climate_details": {
                "is_climate_on": _b(g("is_climate_on")),
                "inside_temp": cv.temp(_n(g("inside_temp"))),
                "outside_temp": cv.temp(_n(g("outside_temp"))),
                "is_preconditioning": _b(g("is_preconditioning")),
                "climate_keeper_mode": g("climate_keeper_mode"),
            },
            "battery_details": {
                "est_battery_range": cv.dist(_n(g("est_battery_range_km"))),
                "rated_battery_range": cv.dist(_n(g("rated_battery_range_km"))),
                "ideal_battery_range": cv.dist(_n(g("ideal_battery_range_km"))),
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
                "tpms_pressure_fl": cv.press(_n(g("tpms_pressure_fl"))),
                "tpms_pressure_fr": cv.press(_n(g("tpms_pressure_fr"))),
                "tpms_pressure_rl": cv.press(_n(g("tpms_pressure_rl"))),
                "tpms_pressure_rr": cv.press(_n(g("tpms_pressure_rr"))),
                "tpms_soft_warning_fl": _b(g("tpms_soft_warning_fl")),
                "tpms_soft_warning_fr": _b(g("tpms_soft_warning_fr")),
                "tpms_soft_warning_rl": _b(g("tpms_soft_warning_rl")),
                "tpms_soft_warning_rr": _b(g("tpms_soft_warning_rr")),
            },
        },
        "units": units,
    }}


# ---------------------------------------------------------------- 电池健康

@router.get("/v1/cars/{car_id}/battery-health")
@_guard
async def battery_health_v1(car_id: int, request: Request):
    """与上游同一算法（TeslaMate Battery Health 面板的 $aux 查询）：
    容量 = rated_range × (kWh/100km) / usable_battery_level；健康度 = 现在/新车。"""
    _check_token(request)
    row = await _car_row(car_id)
    from ..insights import analytics

    h = await analytics.battery(car_id)
    units = await _units()
    cv = _Conv(units)
    cap = h.get("capacity_kwh") or {}
    rng = h.get("range_100_km") or {}
    now_kwh, new_kwh = cap.get("now"), cap.get("new")
    return {"data": {
        "car": _car_stub(row),
        "battery_health": {
            "max_range": cv.dist(rng.get("max")),
            "current_range": cv.dist(rng.get("now")),
            "max_capacity": new_kwh,
            "current_capacity": now_kwh,
            # 上游这里给的是 kWh/100km（aux 效率），不是 cars.efficiency
            "rated_efficiency": h.get("efficiency_kwh_per_100km"),
            "battery_health_percentage": round(now_kwh / new_kwh * 100, 2)
            if (now_kwh and new_kwh) else None,
        },
        "units": {k: v for k, v in units.items() if k != "unit_of_pressure"},
    }}


# ---------------------------------------------------------------- 行程

@router.get("/v1/cars/{car_id}/drives")
@_guard
async def drives_v1(car_id: int, request: Request, limit: int | None = None,
                    page: int | None = None, show: int | None = None,
                    startDate: str | None = None, endDate: str | None = None):  # 上游参数名
    _check_token(request)
    size, offset = _page(limit, page, show)
    return await _drives(car_id, size, offset, _date_param(startDate), _date_param(endDate))


async def _drives(car_id: int, size: int, offset: int, lo=None, hi=None,
                  drive_id: int | None = None) -> dict:
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
        -- 上游只返回已结束的行程（end_date IS NOT NULL）：进行中/孤儿行程的字段
        -- 全是 NULL，客户端按非空解析会崩
        WHERE d.car_id = $1 AND d.end_date IS NOT NULL
          AND ($4::timestamp IS NULL OR d.start_date >= $4)
          AND ($5::timestamp IS NULL OR d.end_date <= $5)
          AND ($6::int IS NULL OR d.id = $6)
        ORDER BY d.start_date DESC LIMIT $2 OFFSET $3
        """,
        car_id, size, offset, lo, hi, drive_id,
    )
    units = await _units()
    cv = _Conv(units)
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
                "odometer_start": cv.dist(r["start_km"]), "odometer_end": cv.dist(r["end_km"]),
                "odometer_distance": cv.dist(dist),
            },
            "duration_min": r["duration_min"], "duration_str": _dur(r["duration_min"]),
            "speed_max": cv.speed(r["speed_max"]),
            "speed_avg": cv.dist(dist / (r["duration_min"] / 60), 1)
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
                "start_range": cv.dist(r["start_ideal_range_km"]),
                "end_range": cv.dist(r["end_ideal_range_km"]),
                "range_diff": cv.dist(max((_f(r["start_ideal_range_km"]) or 0)
                                          - (_f(r["end_ideal_range_km"]) or 0), 0)),
            },
            "range_rated": {
                "start_range": cv.dist(r["start_rated_range_km"]),
                "end_range": cv.dist(r["end_rated_range_km"]),
                "range_diff": cv.dist(rated_used),
            },
            "outside_temp_avg": cv.temp(r["outside_temp_avg"]),
            "inside_temp_avg": cv.temp(r["inside_temp_avg"]),
            "energy_consumed_net": kwh,
            "consumption_net": cv.per_dist(kwh * 1000 / dist) if (kwh and dist > 0) else None,
        })
    return {"data": {"car": _car_stub(row), "drives": drives,
                     "units": {k: v for k, v in units.items() if k != "unit_of_pressure"}}}


@router.get("/v1/cars/{car_id}/drives/{drive_id}")
@_guard
async def drive_detail_v1(car_id: int, drive_id: int, request: Request):
    _check_token(request)
    # 直接按 id 查，不再把这辆车的全部行程拉出来再筛
    one = await _drives(car_id, 1, 0, drive_id=drive_id)
    if not one["data"]["drives"]:
        raise HTTPException(404, "drive not found")
    return one


# ---------------------------------------------------------------- 充电

async def _charges(car_id: int, limit: int, only_open: bool = False, *, offset: int = 0,
                   lo=None, hi=None, charge_id: int | None = None) -> list[dict]:
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
          AND ($4::timestamp IS NULL OR cp.start_date >= $4)
          AND ($5::timestamp IS NULL OR cp.end_date <= $5)
          AND ($6::int IS NULL OR cp.id = $6)
        ORDER BY cp.start_date DESC LIMIT $2 OFFSET $3
        """,  # noqa: S608 - only_open 是本函数的布尔参数，非用户输入
        car_id, int(limit), int(offset), lo, hi, charge_id,
    )
    # 费用走唯一真源（TeslaMate 记录的 > 电价引擎算的）——与门户各页同一个数。
    # 上游只读 cost 列；本机那一列是空的，App 里会全是"—"。
    from ..insights import costs as cost_source

    costs = await cost_source.session_costs(car_id)
    cv = _Conv(await _units())
    return [
        {
            "charge_id": r["id"],
            "start_date": queries._iso(r["start_date"]),
            "end_date": queries._iso(r["end_date"]),
            "address": r["address"],
            "charge_energy_added": _f(r["charge_energy_added"]),
            "charge_energy_used": _f(r["charge_energy_used"]),
            "cost": (costs.get(r["id"]) or {}).get("cost"),
            "duration_min": r["duration_min"], "duration_str": _dur(r["duration_min"]),
            "battery_details": {
                "start_battery_level": r["start_battery_level"],
                "end_battery_level": r["end_battery_level"],
            },
            "range_ideal": {"start_range": cv.dist(r["start_ideal_range_km"]),
                            "end_range": cv.dist(r["end_ideal_range_km"])},
            "range_rated": {"start_range": cv.dist(r["start_rated_range_km"]),
                            "end_range": cv.dist(r["end_rated_range_km"])},
            "outside_temp_avg": cv.temp(r["outside_temp_avg"]),
            "odometer": cv.dist(r["odometer"]),
            "latitude": _f(r["latitude"]), "longitude": _f(r["longitude"]),
        }
        for r in rows
    ]


@router.get("/v1/cars/{car_id}/charges")
@_guard
async def charges_v1(car_id: int, request: Request, limit: int | None = None,
                     page: int | None = None, show: int | None = None,
                     startDate: str | None = None, endDate: str | None = None):  # 上游参数名
    _check_token(request)
    row = await _car_row(car_id)
    size, offset = _page(limit, page, show)
    rows = await _charges(car_id, size, offset=offset, lo=_date_param(startDate),
                          hi=_date_param(endDate))
    return {"data": {"car": _car_stub(row), "charges": rows,
                     "units": {k: v for k, v in (await _units()).items() if k != "unit_of_pressure"}}}


@router.get("/v1/cars/{car_id}/charges/current")
@_guard
async def charge_current_v1(car_id: int, request: Request):
    """正在进行的充电。没有就返回空列表而不是 404 —— 客户端会轮询它。"""
    _check_token(request)
    row = await _car_row(car_id)
    return {"data": {"car": _car_stub(row), "charges": await _charges(car_id, 1, only_open=True),
                     "units": {k: v for k, v in (await _units()).items() if k != "unit_of_pressure"}}}


@router.get("/v1/cars/{car_id}/charges/{charge_id}")
@_guard
async def charge_detail_v1(car_id: int, charge_id: int, request: Request):
    _check_token(request)
    row = await _car_row(car_id)
    hit = await _charges(car_id, 1, charge_id=charge_id)
    if not hit:
        raise HTTPException(404, "charge not found")
    return {"data": {"car": _car_stub(row), "charges": hit,
                     "units": {k: v for k, v in (await _units()).items() if k != "unit_of_pressure"}}}


# ---------------------------------------------------------------- 固件更新

@router.get("/v1/cars/{car_id}/updates")
@_guard
async def updates_v1(car_id: int, request: Request, limit: int | None = None,
                     page: int | None = None, show: int | None = None):
    _check_token(request)
    row = await _car_row(car_id)
    size, offset = _page(limit, page, show)
    rows = await db.fetch(
        "SELECT id, start_date, end_date, version FROM updates"
        " WHERE car_id = $1 ORDER BY start_date DESC LIMIT $2 OFFSET $3",
        car_id, size, offset,
    )
    return {"data": {"car": _car_stub(row), "updates": [
        {"update_id": r["id"], "start_date": queries._iso(r["start_date"]),
         "end_date": queries._iso(r["end_date"]), "version": r["version"]}
        for r in rows
    ]}}


# ---------------------------------------------------------------- 其它

@router.get("/v1/globalsettings")
@_guard
async def globalsettings_v1(request: Request):
    """上游形状（settings.setting_id / account_info / teslamate_units / teslamate_webgui /
    teslamate_urls），外加以前的三个平铺单位键（老调用方在用）。单位是门户偏好 ——
    与其它兼容接口里实际换算用的单位一致。"""
    _check_token(request)
    units = await _units()
    row = await db.fetchrow(
        "SELECT id, inserted_at, updated_at, preferred_range::text AS preferred_range,"
        " language, base_url, grafana_url FROM settings LIMIT 1")
    return {"data": {"settings": {
        "setting_id": row["id"] if row else None,
        "account_info": {"inserted_at": queries._iso(row["inserted_at"]) if row else None,
                         "updated_at": queries._iso(row["updated_at"]) if row else None},
        "teslamate_units": {k: v for k, v in units.items() if k != "unit_of_pressure"},
        "teslamate_webgui": {"preferred_range": row["preferred_range"] if row else "rated",
                             "language": row["language"] if row else None},
        "teslamate_urls": {"base_url": row["base_url"] if row else None,
                           "grafana_url": row["grafana_url"] if row else None},
        **units,
    }}}


@router.get("/ping")
@_guard
async def ping():
    return {"message": "pong"}


@router.get("/readyz")
@_guard
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
@_guard
async def command_not_implemented(car_id: int, command: str):
    raise HTTPException(501, _NOT_IMPLEMENTED)


@router.post("/v1/cars/{car_id}/wake_up")
@_guard
async def wake_not_implemented(car_id: int):
    raise HTTPException(501, _NOT_IMPLEMENTED)
