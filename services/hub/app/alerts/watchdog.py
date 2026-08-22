"""数据源静默看门狗。

安防告警产品最危险的失败模式不是崩溃，而是**安静地不工作**：
TeslaMate 容器停了以后，本套件的页面全部正常、健康检查照样通过、
实时状态卡片永远停在最后一刻的"在线"——但告警管道已经彻底失效。
车主可能一周收不到任何推送，还以为是车位很太平。

原有的 `teslamate_unhealthy` 规则指望 MQTT 上收到 healthy=false，
这要求已经死掉的容器自己发讣告，永远不可能发生。

这里改成主动检测：静默超过阈值就报警（严重级，穿透静默时段），恢复后再报一条。

**但车睡着时本来就没有数据**。TeslaMate 在车休眠后会正确地停止轮询，
一停就是几个小时。如果照直报警，这个看门狗每天晚上都要喊一次狼来了 ——
恰恰是整套会话模型在防的告警疲劳。所以只有在「车应该在上报」的时候
（online / driving / charging）静默才算异常。
"""

import asyncio
import contextlib
import logging
import time

from ..config import settings
from ..i18n.prefs import get as get_prefs
from ..i18n.router import t
from ..livestate import live

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 60


class DataWatchdog:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        # 按车维护告警状态。以前是一个全局布尔量，表达不了"1 号车断了、
        # 2 号车好着"，也就没法在恢复时只对断掉的那辆发"已恢复"。
        self._alerted_cars: set[str] = set()
        self._fire = None
        self._started_at = time.time()

    def bind(self, fire_callable) -> None:
        self._fire = fire_callable

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="data-watchdog")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def last_seen(self) -> float:
        """最近一次收到**任何**车辆数据的时间；从未收到过则为 0。"""
        return max(live.last_update.values(), default=0.0)

    # 车处于这些状态时不上报是正常的，不是管道故障
    QUIET_STATES = ("asleep", "offline", "suspended", "unavailable")

    def _car_quiet(self, car_id: str) -> bool:
        st = ((live.cars.get(car_id) or {}).get("state") or "").lower()
        return bool(st) and st in self.QUIET_STATES

    def expected_to_report(self) -> bool:
        """至少有一辆车现在应该在上报数据。"""
        states = [
            (v.get("state") or "").lower()
            for v in live.cars.values() if v.get("state")
        ]
        if not states:
            return True          # 不知道状态时按"应该上报"处理，宁可多查一次
        return any(st not in self.QUIET_STATES for st in states)

    def per_car(self) -> dict[str, dict]:
        """逐车的新鲜度。

        全局 max() 会掩盖问题：两辆车时只要有一辆还在发，整体 last_seen
        就一直是新的，另一辆的管道彻底死掉也永远不会报警 —— 而"没有告警"
        正是这个产品最危险的失败方式。
        """
        now = time.time()
        out: dict[str, dict] = {}
        for car_id, last in live.last_update.items():
            age = now - last if last else None
            quiet = self._car_quiet(car_id)
            out[car_id] = {
                "last_data_at": last or None,
                "seconds_since_data": round(age) if age is not None else None,
                "quiet": quiet,
                "stale": bool(last and age and age > settings.data_stale_seconds and not quiet),
                "name": (live.cars.get(car_id) or {}).get("display_name"),
            }
        return out

    def state(self) -> dict:
        last = self.last_seen()
        age = time.time() - last if last else None
        quiet = not self.expected_to_report()
        cars = self.per_car()
        return {
            "last_data_at": last or None,
            "seconds_since_data": round(age) if age is not None else None,
            # 任何一辆车的管道断了，横幅就该出现，而不是等到全部都断
            "stale": any(c["stale"] for c in cars.values())
                     or bool(last and age and age > settings.data_stale_seconds and not quiet),
            "vehicle_quiet": quiet,
            "never_received": not last,
            "threshold_seconds": settings.data_stale_seconds,
            "uptime_seconds": round(time.time() - self._started_at),
            "cars": cars,
        }

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            try:
                await self._check()
            except Exception:
                log.exception("data watchdog check failed")

    async def _check(self) -> None:
        """逐车判定。

        原来是拿全车 max(last_update) 跟阈值比：三辆车里有一辆还在正常上报，
        另外两辆的管道全死了也一个字都不会说。告警状态也是一个全局布尔量，
        没法表达"1 号车断了、2 号车好着"。
        """
        if not live.last_update:
            return  # 还没登录/首次启动，不是"中断"

        now = time.time()
        for car_id, last in list(live.last_update.items()):
            if not last:
                continue
            age = now - last
            # 车在睡觉/离线时没有数据是正常的（也不清除已有告警状态，
            # 否则车一睡就会误发一条"已恢复"）
            if self._car_quiet(car_id):
                continue

            name = (live.cars.get(car_id) or {}).get("display_name") or f"Car {car_id}"
            if age > settings.data_stale_seconds and car_id not in self._alerted_cars:
                self._alerted_cars.add(car_id)
                mins = int(age // 60)
                log.error("data collection stalled for %s: no MQTT for %d minutes", name, mins)
                if self._fire:
                    loc = (await get_prefs())["locale"]
                    await self._fire(
                        car_id, "critical",
                        t("watchdog.stalled_title", loc),
                        t("watchdog.stalled_body", loc, n=mins),
                        rule="data_stale",
                    )
            elif age <= settings.data_stale_seconds and car_id in self._alerted_cars:
                self._alerted_cars.discard(car_id)
                log.info("data collection resumed for %s", name)
                if self._fire:
                    loc = (await get_prefs())["locale"]
                    await self._fire(
                        car_id, "info",
                        t("watchdog.resumed_title", loc),
                        t("watchdog.resumed_body", loc), rule="data_resumed",
                    )


watchdog = DataWatchdog()
