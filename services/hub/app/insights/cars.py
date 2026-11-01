"""车辆名册：哪些车还"活着"、最后一次见到是什么时候、默认该用哪一辆。

为什么需要它：TeslaMate 不会删除已经离开账号的车。本机账号里的 2 号、3 号车
早就不在 Tesla 账号里了（TeslaMate 只跑 1 号车，没有它们的 MQTT），但
`states` 表里它们各自还挂着一条没关闭的 `online` —— 按 `COALESCE(end_date, now())`
算，它们"已经连续在线 40～68 天"，睡眠评分 0。MCP 默认取"第一辆"车，
在这个三车账号上会把"开锁"发给一辆根本不存在的车。

判定规则（API.md 有完整说明）：
- last_seen  = TeslaMate 最后一次为这辆车记下任何东西的时刻：
               max(完整 positions 行的时间, states 的起止时间)。
- mqtt_live  = 本进程启动以来收到过这辆车的 MQTT 消息（TeslaMate 只给它
               正在跟踪的车发 MQTT，睡着的车也有 retained 消息）。
- archived   = car_settings.enabled 为 false，或者
               (last_seen 早于 ARCHIVE_AFTER_DAYS 天 且 没有 MQTT)。
  一辆在机场停了三周的车不会被误判：它仍在账号里，TeslaMate 仍在发它的 MQTT。
"""

import logging
import time
from datetime import timedelta

from .. import db
from .period import iso, utcnow

log = logging.getLogger(__name__)

ARCHIVE_AFTER_DAYS = 14

# VIN 第 4 位（下标 3）是车型线。TeslaMate 4.2 之前不认 Cybertruck，
# 老库里的 CT 行 model 是 NULL，要等它下一次醒来才会被改写 —— 已离开账号的车
# 永远等不到那一天（本机 2 号车就是这样），所以要能从 VIN 推。
_VIN_MODEL = {"S": "S", "3": "3", "X": "X", "Y": "Y", "C": "Cybertruck"}

# 坡度修正能耗用的整备质量（kg）。TeslaMate 的面板对所有车硬编码 2100 kg，
# Cybertruck 实际约 3,100 kg（AWD 3,104 / Cyberbeast 3,107），用 2100 会把
# 爬坡能耗低估三分之一。其它车型保持与 TeslaMate 一致，避免两边数字对不上。
_MASS_KG = {"Cybertruck": 3100}
DEFAULT_MASS_KG = 2100

_cache: tuple[float, list[dict]] | None = None
_CACHE_S = 30.0


def model_of(model: str | None, vin: str | None) -> str | None:
    if model:
        return model
    v = (vin or "").strip().upper()
    if len(v) >= 4 and not v.startswith("__LEGACY"):
        return _VIN_MODEL.get(v[3])
    return None


def mass_kg(model: str | None) -> int:
    return _MASS_KG.get(model or "", DEFAULT_MASS_KG)


def label(car: dict | None) -> str:
    """给人看的车名：名字 > 车型 > "Car N"，并带上 id，免得两辆同名车分不清。"""
    if not car:
        return "unknown car"
    name = (car.get("name") or "").strip()
    model = car.get("model") or ""
    base = name or model or f"Car {car.get('id')}"
    if name and model and model.lower() not in name.lower():
        base = f"{name} ({model})"
    return f"{base} #{car.get('id')}"


def _mqtt_age(car_id: int) -> float | None:
    try:
        from ..livestate import live

        ts = live.last_update.get(str(car_id))
    except Exception:  # noqa: BLE001
        return None
    return None if ts is None else max(0.0, time.time() - ts)


async def list_cars(*, fresh: bool = False) -> list[dict]:
    """全部车辆 + last_seen / archived。30 秒缓存（首页、MCP 每次都要问）。"""
    global _cache
    if not fresh and _cache and time.monotonic() - _cache[0] < _CACHE_S:
        rows = _cache[1]
    else:
        rows = [dict(r) for r in await db.fetch(
            """
            SELECT c.id, c.name, c.model, c.marketing_name, c.trim_badging, c.vin,
                   c.efficiency, c.exterior_color, c.wheel_type, c.display_priority,
                   s.lfp_battery, s.enabled, s.free_supercharging,
                   GREATEST(
                     (SELECT max(p.date) FROM positions p
                       WHERE p.car_id = c.id AND p.ideal_battery_range_km IS NOT NULL),
                     (SELECT max(GREATEST(st.start_date, COALESCE(st.end_date, st.start_date)))
                        FROM states st WHERE st.car_id = c.id)
                   ) AS last_seen
            FROM cars c
            LEFT JOIN car_settings s ON s.id = c.settings_id
            ORDER BY c.display_priority, c.id
            """
        )]
        _cache = (time.monotonic(), rows)
    now = utcnow()
    out = []
    for r in rows:
        last = r.get("last_seen")
        age_days = (now - last).total_seconds() / 86400 if last else None
        mqtt_age = _mqtt_age(r["id"])
        enabled = r.get("enabled") is not False
        stale = age_days is None or age_days > ARCHIVE_AFTER_DAYS
        archived = (not enabled) or (stale and mqtt_age is None)
        model = model_of(r.get("model"), r.get("vin"))
        out.append({
            **{k: v for k, v in r.items() if k != "last_seen"},
            "model": r.get("model"),
            "model_line": model,          # model，老库/已离开的车用 VIN 补
            "last_seen": iso(last),
            "days_since_seen": round(age_days, 1) if age_days is not None else None,
            "mqtt_live": mqtt_age is not None,
            "enabled": enabled,
            "archived": archived,
            "label": label({**r, "model": model}),
        })
    return out


async def get_car(car_id: int) -> dict | None:
    for c in await list_cars():
        if c["id"] == car_id:
            return c
    return None


async def default_car_id() -> int | None:
    """没指定车时用哪一辆：最近活跃、且没归档的那辆。

    排序键是 last_seen（不是 id，也不是 live.cars 的插入顺序 —— 后者在三车账号上
    会选中一辆已经不存在的车）。全部归档时退回最近见过的那辆，总比报错强。
    """
    try:
        cars = await list_cars()
    except db.DbUnavailable:
        cars = []
    if not cars:
        try:
            from ..livestate import live

            ids = sorted(int(k) for k in live.last_update if k.isdigit())
            return ids[0] if ids else None
        except Exception:  # noqa: BLE001
            return None
    live_ = [c for c in cars if not c["archived"]] or cars
    live_.sort(key=lambda c: (c["mqtt_live"], c["last_seen"] or ""), reverse=True)
    return live_[0]["id"]


async def resolve(car_id: int | None) -> int | None:
    return car_id or await default_car_id()


async def open_state_cap(car_id: int):
    """未关闭的 states 行该截到哪：活着的车截到现在，归档的车截到 last_seen。"""
    c = await get_car(car_id)
    if c and c["archived"] and c["last_seen"]:
        from datetime import datetime

        return datetime.fromisoformat(c["last_seen"]).replace(tzinfo=None)
    return utcnow()


def is_recent(dt, minutes: int = 30) -> bool:
    return dt is not None and utcnow() - dt < timedelta(minutes=minutes)
