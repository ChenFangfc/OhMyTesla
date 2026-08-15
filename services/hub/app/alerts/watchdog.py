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
        self._alerted = False
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
        """最近一次收到任何车辆数据的时间；从未收到过则为 0。"""
        return max(live.last_update.values(), default=0.0)

    # 车处于这些状态时不上报是正常的，不是管道故障
    QUIET_STATES = ("asleep", "offline", "suspended", "unavailable")

    def expected_to_report(self) -> bool:
        """车现在是否应该在上报数据。"""
        states = [
            (v.get("state") or "").lower()
            for v in live.cars.values() if v.get("state")
        ]
        if not states:
            return True          # 不知道状态时按"应该上报"处理，宁可多查一次
        return any(st not in self.QUIET_STATES for st in states)

    def state(self) -> dict:
        last = self.last_seen()
        age = time.time() - last if last else None
        quiet = not self.expected_to_report()
        return {
            "last_data_at": last or None,
            "seconds_since_data": round(age) if age is not None else None,
            "stale": bool(last and age and age > settings.data_stale_seconds and not quiet),
            "vehicle_quiet": quiet,
            "never_received": not last,
            "threshold_seconds": settings.data_stale_seconds,
            "uptime_seconds": round(time.time() - self._started_at),
        }

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            try:
                await self._check()
            except Exception:  # noqa: BLE001
                log.exception("data watchdog check failed")

    async def _check(self) -> None:
        last = self.last_seen()
        if not last:
            return  # 还没登录/首次启动，不是"中断"
        age = time.time() - last

        # 车在睡觉/离线时没有数据是正常的，不报警（但也不清除已有的报警状态，
        # 否则车一睡就会误发一条"已恢复"）
        if not self.expected_to_report():
            return

        if age > settings.data_stale_seconds and not self._alerted:
            self._alerted = True
            mins = int(age // 60)
            log.error("data collection stalled: no MQTT for %d minutes", mins)
            if self._fire:
                loc = (await get_prefs())["locale"]
                await self._fire(
                    next(iter(live.cars), "0"), "critical",
                    t("watchdog.stalled_title", loc),
                    t("watchdog.stalled_body", loc, n=mins),
                    rule="data_stale",
                )
        elif age <= settings.data_stale_seconds and self._alerted:
            self._alerted = False
            log.info("data collection resumed")
            if self._fire:
                loc = (await get_prefs())["locale"]
                await self._fire(
                    next(iter(live.cars), "0"), "info",
                    t("watchdog.resumed_title", loc),
                    t("watchdog.resumed_body", loc), rule="data_resumed",
                )


watchdog = DataWatchdog()
