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

    # ── 以下为补齐的指令。选取标准是"车主真的会用、且语义无歧义"，
    #    不是把 Fleet API 的 74 条全塞进来 —— 那里面有一半是给车队管理和
    #    第三方司机用的（valet PIN、guest mode、驾驶员邀请），
    #    对单车主是噪声，而每多一条就多一条要维护和翻译的东西。
    "seat_heater_left":   {"ep": "command/remote_seat_heater_request", "risky": False,
                           "key": "cmd.seat_heater_left", "icon": "🔥",
                           "param": "level", "body": {"heater": 0}},
    "seat_heater_right":  {"ep": "command/remote_seat_heater_request", "risky": False,
                           "key": "cmd.seat_heater_right", "icon": "🔥",
                           "param": "level", "body": {"heater": 1}},
    "steering_heater_on": {"ep": "command/remote_steering_wheel_heater_request", "risky": False,
                           "key": "cmd.steering_heater_on", "icon": "🔥", "body": {"on": True}},
    "steering_heater_off": {"ep": "command/remote_steering_wheel_heater_request", "risky": False,
                            "key": "cmd.steering_heater_off", "icon": "🔥", "body": {"on": False}},
    "defrost_on":         {"ep": "command/set_preconditioning_max", "risky": False,
                           "key": "cmd.defrost_on", "icon": "🌨️", "body": {"on": True}},
    "defrost_off":        {"ep": "command/set_preconditioning_max", "risky": False,
                           "key": "cmd.defrost_off", "icon": "🌨️", "body": {"on": False}},
    "set_charge_amps":    {"ep": "command/set_charging_amps", "risky": False,
                           "key": "cmd.set_charge_amps", "icon": "🔋", "param": "charging_amps"},
    "dog_mode_on":        {"ep": "command/set_climate_keeper_mode", "risky": False,
                           "key": "cmd.dog_mode_on", "icon": "🐕", "body": {"climate_keeper_mode": 2}},
    "camp_mode_on":       {"ep": "command/set_climate_keeper_mode", "risky": False,
                           "key": "cmd.camp_mode_on", "icon": "🏕️", "body": {"climate_keeper_mode": 3}},
    "keeper_off":         {"ep": "command/set_climate_keeper_mode", "risky": False,
                           "key": "cmd.keeper_off", "icon": "🌡️", "body": {"climate_keeper_mode": 0}},
    "cabin_overheat_on":  {"ep": "command/set_cabin_overheat_protection", "risky": False,
                           "key": "cmd.cabin_overheat_on", "icon": "☀️",
                           "body": {"on": True, "fan_only": False}},
    "cabin_overheat_off": {"ep": "command/set_cabin_overheat_protection", "risky": False,
                           "key": "cmd.cabin_overheat_off", "icon": "☀️",
                           "body": {"on": False, "fan_only": False}},
    "media_toggle":       {"ep": "command/media_toggle_playback", "risky": False,
                           "key": "cmd.media_toggle", "icon": "⏯️"},
    "media_next":         {"ep": "command/media_next_track", "risky": False,
                           "key": "cmd.media_next", "icon": "⏭️"},
    "media_prev":         {"ep": "command/media_prev_track", "risky": False,
                           "key": "cmd.media_prev", "icon": "⏮️"},
    "boombox":            {"ep": "command/remote_boombox", "risky": True,
                           "key": "cmd.boombox", "icon": "📻", "body": {"sound": 0}},
    "schedule_charge":    {"ep": "command/set_scheduled_charging", "risky": False,
                           "key": "cmd.schedule_charge", "icon": "🕐",
                           "param": "time", "body": {"enable": True}},
    "schedule_charge_off": {"ep": "command/set_scheduled_charging", "risky": False,
                            "key": "cmd.schedule_charge_off", "icon": "🕐",
                            "body": {"enable": False, "time": 0}},
}


# 带参指令的取值范围。超出范围 Tesla 会回 400，先在本地挡住并说明白。
_PARAM_RANGE = {
    "percent": (50, 100),        # 充电上限：低于 50% 特斯拉不接受
    "driver_temp": (15, 28),     # 车内空调设定温度（摄氏）
    "level": (0, 3),             # 座椅加热档位：0 关，1-3 由弱到强
    "charging_amps": (0, 48),    # 充电电流。0 = 暂停；上限随车型和线缆而定，
                                 # 给到 48 是家用壁挂充电桩的常见上限
    "time": (0, 1439),           # 定时充电：从当天 00:00 起的分钟数
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
    need = spec.get("param")
    if need:
        # 缺参数就直接说清楚。以前是"没传就发空 body"，Tesla 回 400，
        # 用户看到的是一句莫名其妙的 502。
        if not params or params.get(need) is None:
            raise CommandError(t("cmd.error.missing_param", loc, param=need))
        try:
            value = float(params[need])
        except (TypeError, ValueError):
            raise CommandError(t("cmd.error.missing_param", loc, param=need)) from None
        lo, hi = _PARAM_RANGE.get(need, (None, None))
        if lo is not None and not (lo <= value <= hi):
            raise CommandError(
                t("cmd.error.param_range", loc, param=need, lo=lo, hi=hi))
        body[need] = int(value) if float(value).is_integer() else value
    if params:
        for k, v in params.items():
            if k != need and k in (spec.get("body") or {}):
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
        low = detail.lower()
        # 2021+ 的车（含 Cybertruck）拒绝未签名指令。Tesla 有时给一句
        # "unsigned command"，但**更多时候只回一个光秃秃的 403**，报文里
        # 什么都不说。没配签名代理时把 403 直接当成这件事来解释 ——
        # 否则用户看到的就只是 "HTTP 403"，完全不知道该去做什么。
        if (
            "unsigned" in low
            or "signed command" in low
            or (resp.status_code == 403 and not settings.tesla_command_proxy)
        ):
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
