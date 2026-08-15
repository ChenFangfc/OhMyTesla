"""Tesla 车辆指令下发。

支持三种后端（由 TESLA_API_HOST 决定，与 TeslaMate 用同一个）：
- owner-api.teslamotors.com（旧接口，仍可用于部分老车）
- owner-api.vn.cloud.tesla.cn（中国大陆）
- 第三方代理 / Fleet API（fleet-api.prd.*.vn.cloud.tesla.com）

2021 年后的车经 Fleet API 下发指令需要"签名指令"，必须走
teslamotors/vehicle-command 代理（TESLA_COMMAND_PROXY），
否则会收到 "unsigned command" 类错误——UI 会如实显示这个错误。
"""

import logging

import httpx

from ..config import settings
from ..i18n.prefs import get as get_prefs
from ..i18n.router import t
from . import tokens

log = logging.getLogger(__name__)

# name -> (endpoint, risky?, catalog key, icon, optional body/param)
COMMANDS: dict[str, dict] = {
    "wake_up":            {"ep": "wake_up",                 "risky": False, "key": "cmd.wake_up", "icon": "☀️"},
    "honk_horn":          {"ep": "command/honk_horn",       "risky": False, "key": "cmd.honk_horn",     "icon": "📢"},
    "flash_lights":       {"ep": "command/flash_lights",    "risky": False, "key": "cmd.flash_lights",     "icon": "💡"},
    "door_lock":          {"ep": "command/door_lock",       "risky": False, "key": "cmd.door_lock",     "icon": "🔒"},
    "door_unlock":        {"ep": "command/door_unlock",     "risky": True,  "key": "cmd.door_unlock",     "icon": "🔓"},
    "climate_on":         {"ep": "command/auto_conditioning_start", "risky": False, "key": "cmd.climate_on", "icon": "❄️"},
    "climate_off":        {"ep": "command/auto_conditioning_stop",  "risky": False, "key": "cmd.climate_off", "icon": "🌡️"},
    "charge_start":       {"ep": "command/charge_start",    "risky": False, "key": "cmd.charge_start", "icon": "⚡"},
    "charge_stop":        {"ep": "command/charge_stop",     "risky": False, "key": "cmd.charge_stop", "icon": "⏹️"},
    "charge_port_open":   {"ep": "command/charge_port_door_open",  "risky": False, "key": "cmd.charge_port_open", "icon": "🔌"},
    "charge_port_close":  {"ep": "command/charge_port_door_close", "risky": False, "key": "cmd.charge_port_close", "icon": "🔌"},
    "set_charge_limit":   {"ep": "command/set_charge_limit","risky": False, "key": "cmd.set_charge_limit", "icon": "🎚️", "param": "percent"},
    "set_temps":          {"ep": "command/set_temps",       "risky": False, "key": "cmd.set_temps", "icon": "🌡️", "param": "driver_temp"},
    "vent_windows":       {"ep": "command/window_control",  "risky": False, "key": "cmd.vent_windows", "icon": "🪟",
                           "body": {"command": "vent", "lat": 0, "lon": 0}},
    "close_windows":      {"ep": "command/window_control",  "risky": False, "key": "cmd.close_windows", "icon": "🪟",
                           "body": {"command": "close", "lat": 0, "lon": 0}},
    "frunk":              {"ep": "command/actuate_trunk",   "risky": True,  "key": "cmd.frunk", "icon": "🧳",
                           "body": {"which_trunk": "front"}},
    "trunk":              {"ep": "command/actuate_trunk",   "risky": True,  "key": "cmd.trunk", "icon": "🧳",
                           "body": {"which_trunk": "rear"}},
    "sentry_on":          {"ep": "command/set_sentry_mode", "risky": False, "key": "cmd.sentry_on", "icon": "🛡️",
                           "body": {"on": True}},
    "sentry_off":         {"ep": "command/set_sentry_mode", "risky": True,  "key": "cmd.sentry_off", "icon": "🛡️",
                           "body": {"on": False}},
}


class CommandError(Exception):
    pass


def _api_base() -> str:
    host = (settings.tesla_command_proxy or settings.tesla_api_host).rstrip("/")
    if not host.startswith("http"):
        host = "https://" + host
    return host


def _is_fleet() -> bool:
    """Fleet API 与 vehicle-command 签名代理都按 VIN 寻址；只有旧 owner-api 用数字 id。"""
    base = _api_base()
    return "fleet-api" in base or bool(settings.tesla_command_proxy)


async def _vehicle_ref(car_id: int, loc: str = "en") -> str:
    from .. import db

    row = await db.fetchrow("SELECT eid, vid, vin FROM cars WHERE id = $1", car_id)
    if row is None:
        raise CommandError(t("cmd.error.car_missing", loc, car_id=car_id))
    if _is_fleet():
        if not row["vin"]:
            raise CommandError(t("cmd.error.no_vin", loc))
        return row["vin"]
    if not row["eid"]:
        raise CommandError(t("cmd.error.no_eid", loc))
    return str(row["eid"])


async def _last_location(car_id: int) -> tuple[float, float] | None:
    """车窗控制要求带当前坐标做就近校验，取 TeslaMate 最后已知位置。"""
    from .. import db

    row = await db.fetchrow(
        "SELECT latitude, longitude FROM positions WHERE car_id = $1"
        " AND latitude IS NOT NULL ORDER BY date DESC LIMIT 1",
        car_id,
    )
    if row is None:
        return None
    return float(row["latitude"]), float(row["longitude"])


async def send(car_id: int, command: str, params: dict | None = None) -> dict:
    loc = (await get_prefs())["locale"]
    spec = COMMANDS.get(command)
    if spec is None:
        raise CommandError(t("cmd.error.unknown_command", loc, command=command))

    token = await tokens.access_token()
    if not token:
        raise CommandError(t("cmd.error.no_token", loc))

    ref = await _vehicle_ref(car_id, loc)
    url = f"{_api_base()}/api/1/vehicles/{ref}/{spec['ep']}"
    body: dict = dict(spec.get("body") or {})
    if spec.get("param") and params:
        for k, v in params.items():
            body[k] = v
    if command == "set_temps" and "driver_temp" in body:
        body.setdefault("passenger_temp", body["driver_temp"])
    if spec["ep"].endswith("window_control"):
        # Tesla 要求 close 带上车辆当前坐标做就近校验，0,0 会被拒绝
        # 变量名不能叫 loc —— 那是语言代码，覆盖了会让后面所有报错的语言失效
        pos = await _last_location(car_id)
        if pos is None:
            raise CommandError(t("cmd.error.no_location", loc))
        body["lat"], body["lon"] = pos

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body or None, headers=headers)
    except httpx.HTTPError as e:
        raise CommandError(t("cmd.error.request_failed", loc, error=str(e))) from e

    if resp.status_code == 401:
        tokens.invalidate_cache()  # 强制下次重新读取 TeslaMate 刷新后的 token
        raise CommandError(t("cmd.error.token_expired", loc))
    if resp.status_code == 408:
        raise CommandError(t("cmd.error.asleep", loc))
    if resp.status_code >= 400:
        detail = resp.text[:300]
        if "unsigned" in detail.lower() or "signed command" in detail.lower():
            raise CommandError(t("cmd.error.unsigned", loc))
        raise CommandError(t("cmd.error.http", loc, status=resp.status_code, detail=detail))

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:200]}
    result = data.get("response") or {}
    if isinstance(result, dict) and result.get("result") is False:
        raise CommandError(result.get("reason") or t("cmd.error.rejected", loc))
    log.info("command %s sent to car %s", command, car_id)
    return {"ok": True, "response": result}
