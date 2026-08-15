"""保养提醒。

特斯拉没有固定保养周期，但轮胎换位、空调滤芯、刹车油、雨刮这些还是按
里程或时间来的。车主通常靠记忆，然后忘掉。

设计上刻意做得很笨：一条提醒 = {名称, 每多少公里, 每多少个月, 上次做的里程/日期}。
到期就发一条 info 告警，做完点一下"已完成"就把基准推到当前。
不猜车型、不内置厂商保养表 —— 那些各地区各年份都不一样，猜错比不提醒更糟。
"""

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone

from .store import store

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 6 * 60 * 60      # 保养是以月计的事，六小时查一次绰绰有余
STARTUP_DELAY_S = 120

# 出厂预置几条常见项，用户可删可改。间隔取特斯拉手册里的通用建议。
DEFAULT_ITEMS = [
    {"id": "tires", "name_en": "Tire rotation", "name_zh": "轮胎换位",
     "every_km": 10000, "every_months": None},
    {"id": "cabin_filter", "name_en": "Cabin air filter", "name_zh": "空调滤芯",
     "every_km": None, "every_months": 24},
    {"id": "brake_fluid", "name_en": "Brake fluid check", "name_zh": "刹车油检查",
     "every_km": None, "every_months": 24},
    {"id": "wipers", "name_en": "Wiper blades", "name_zh": "雨刮片",
     "every_km": None, "every_months": 12},
]


async def get_items() -> list[dict]:
    saved = await store.get_setting("maintenance", None)
    if saved is None:
        return [dict(i, last_km=None, last_date=None) for i in DEFAULT_ITEMS]
    return saved


async def set_items(items: list[dict]) -> list[dict]:
    await store.set_setting("maintenance", items)
    return items


async def mark_done(item_id: str, odometer_km: float | None) -> list[dict]:
    """做完了：把基准推到现在。"""
    items = await get_items()
    for it in items:
        if it.get("id") == item_id:
            it["last_km"] = round(float(odometer_km), 1) if odometer_km else it.get("last_km")
            it["last_date"] = datetime.now(timezone.utc).date().isoformat()
    await set_items(items)
    return items


def _due(item: dict, odometer_km: float | None) -> dict | None:
    """算这条到期没有。返回 None 表示还没到。"""
    reasons = []
    if item.get("every_km") and odometer_km and item.get("last_km"):
        driven = odometer_km - float(item["last_km"])
        if driven >= float(item["every_km"]):
            reasons.append({"kind": "km", "value": round(driven)})
    if item.get("every_months") and item.get("last_date"):
        try:
            last = datetime.fromisoformat(str(item["last_date"])).date()
            months = (datetime.now(timezone.utc).date() - last).days / 30.44
            if months >= float(item["every_months"]):
                reasons.append({"kind": "months", "value": round(months)})
        except ValueError:
            pass
    return reasons[0] if reasons else None


async def status(odometer_km: float | None) -> list[dict]:
    """每条的到期情况，供设置页显示。"""
    out = []
    for it in await get_items():
        d = _due(it, odometer_km)
        out.append({**it, "due": bool(d), "due_reason": d})
    return out


class MaintenanceWatcher:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._fire = None

    def bind(self, fire_callable) -> None:
        self._fire = fire_callable

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="maintenance")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        await asyncio.sleep(STARTUP_DELAY_S)
        while True:
            try:
                await self._check()
            except Exception:  # noqa: BLE001
                log.exception("maintenance check failed")
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def _check(self) -> None:
        from ..i18n.prefs import get as get_prefs
        from ..livestate import live

        if self._fire is None:
            return
        car_id = next((k for k in live.cars if k.isdigit()), None)
        if car_id is None:
            return
        try:
            odo = float(live.cars[car_id].get("odometer") or 0)
        except (TypeError, ValueError):
            return

        loc = (await get_prefs())["locale"]
        # 已经提醒过的不再重复，直到用户点"已完成"
        notified = set(await store.get_setting("maintenance_notified", []) or [])
        changed = False
        for it in await status(odo or None):
            if not it["due"]:
                notified.discard(it["id"])      # 做完了就允许下一轮再提醒
                changed = True
                continue
            if it["id"] in notified:
                continue
            name = it.get("name_zh" if loc == "zh" else "name_en") or it["id"]
            await self._fire(car_id, "info",
                             "🔧 " + name,
                             name, rule="maintenance_due")
            notified.add(it["id"])
            changed = True
        if changed:
            await store.set_setting("maintenance_notified", sorted(notified))


watcher = MaintenanceWatcher()
