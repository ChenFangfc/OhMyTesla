"""ABRP（A Better Routeplanner）实时遥测中继。

ABRP 规划路线时默认用车型的平均能耗模型。把真实的电量、车速、位置、功率喂
给它，规划就按**你这台车**的实际状态来算——生态里安装量最大的一座桥，
社区实现是 fetzu/teslamate-abrp（MIT，独立容器）。这里做成内置模块，
省掉一个容器，也顺带解决那个实现没有的两件事：

1. **休眠时闭嘴。** 车睡着时状态不变，仍按固定间隔推的话纯属浪费；
   这里按 行驶 / 充电 / 停放 三档节流，停放且无变化时干脆不发。
2. **失败可见。** 中继挂掉的表现是"ABRP 里的数据不动了"，在本项目里
   看不出任何异常。`/api/integrations/abrp/status` 给出最后一次成功、
   最后一次错误、累计发送数。

隐私：`ABRP_SKIP_LOCATION=true` 时不发经纬度（ABRP 仍能用 SOC 做估算）。
"""

import asyncio
import contextlib
import logging
import time

import httpx

from ..config import settings
from ..livestate import live

log = logging.getLogger(__name__)

ENDPOINT = "https://api.iternio.com/1/tlm/send"

# 各状态下的最小上报间隔（秒）。ABRP 只在导航中需要密集数据，
# 停放时一分钟一条足够它知道"车还在那儿、电量多少"。
INTERVAL_DRIVING = 15
INTERVAL_CHARGING = 60
INTERVAL_PARKED = 600


def _f(v):
    try:
        return None if v in (None, "", "nil") else float(v)
    except (TypeError, ValueError):
        return None


def _is(v) -> bool:
    return str(v).lower() == "true"


class AbrpRelay:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._last_sent: dict[str, float] = {}
        self.sent = 0
        self.failed = 0
        self.last_ok: float | None = None
        self.last_error: str = ""

    # ---------- 生命周期 ----------

    def enabled(self) -> bool:
        return bool(settings.abrp_enabled and settings.abrp_token)

    def start(self) -> None:
        if not self.enabled():
            log.info("ABRP relay disabled (set ABRP_ENABLED=true and ABRP_TOKEN)")
            return
        self._task = asyncio.create_task(self._run(), name="abrp-relay")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def status(self) -> dict:
        return {
            "enabled": self.enabled(),
            "configured": bool(settings.abrp_token),
            "sent": self.sent,
            "failed": self.failed,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "skip_location": settings.abrp_skip_location,
        }

    # ---------- 组包 ----------

    def _interval_for(self, car: dict) -> int:
        if car.get("shift_state") in ("D", "R", "N") or _f(car.get("speed")):
            return INTERVAL_DRIVING
        if car.get("charging_state") == "Charging":
            return INTERVAL_CHARGING
        return INTERVAL_PARKED

    def build(self, car: dict) -> dict | None:
        """按 ABRP 的 tlm schema 组一份。缺 SOC 就不发——那是它唯一的必需字段。"""
        soc = _f(car.get("usable_battery_level")) or _f(car.get("battery_level"))
        if soc is None:
            return None

        tlm: dict = {"utc": int(time.time()), "soc": soc}

        speed = _f(car.get("speed"))
        if speed is not None:
            tlm["speed"] = speed                      # km/h，与 ABRP 一致
        power = _f(car.get("power"))
        if power is not None:
            tlm["power"] = power                      # kW，放电为正
        elev = _f(car.get("elevation"))
        if elev is not None:
            tlm["elevation"] = elev
        ext = _f(car.get("outside_temp"))
        if ext is not None:
            tlm["ext_temp"] = ext
        rng = _f(car.get("rated_battery_range_km"))
        if rng is not None:
            tlm["est_battery_range"] = rng

        charging = car.get("charging_state") == "Charging"
        tlm["is_charging"] = charging
        if charging:
            # 充电时 power 应为负（进电），TeslaMate 的 charger_power 是正值
            cp = _f(car.get("charger_power"))
            if cp:
                tlm["power"] = -cp
            v = _f(car.get("charger_voltage"))
            if v and v > 90:
                # 电压高于 90V 基本可判直流快充；ABRP 用它区分交流/直流曲线
                tlm["is_dcfc"] = v > 300
        tlm["is_parked"] = car.get("shift_state") in (None, "", "P") and not speed

        if not settings.abrp_skip_location:
            lat, lon = _f(car.get("latitude")), _f(car.get("longitude"))
            if lat is not None and lon is not None:
                tlm["lat"], tlm["lon"] = lat, lon
            heading = _f(car.get("heading"))
            if heading is not None:
                tlm["heading"] = heading
        return tlm

    # ---------- 发送 ----------

    async def _post(self, client: httpx.AsyncClient, tlm: dict) -> None:
        import json as _json

        try:
            r = await client.post(
                ENDPOINT,
                params={"api_key": settings.abrp_api_key, "token": settings.abrp_token},
                data={"tlm": _json.dumps(tlm)},
            )
            if r.status_code == 200:
                self.sent += 1
                self.last_ok = time.time()
                self.last_error = ""
                return
            self.failed += 1
            self.last_error = f"HTTP {r.status_code}: {r.text[:120]}"
            log.warning("ABRP rejected telemetry: %s", self.last_error)
        except httpx.HTTPError as e:
            self.failed += 1
            self.last_error = str(e)[:160]
            log.warning("ABRP send failed: %s", self.last_error)

    async def _run(self) -> None:
        log.info("ABRP relay started")
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                await asyncio.sleep(5)
                try:
                    now = time.time()
                    for car_id, car in list(live.cars.items()):
                        if not car_id.isdigit():
                            continue          # fleet 占位键，不是真车
                        # live.cars 是"最后已知状态"，不等于"当前状态"：
                        # seed_from_db() 会用数据库里的旧值把它填满，TeslaMate
                        # 停了之后这些值也会一直停在那儿。照发的话，ABRP 会拿到
                        # 一个盖着当前时间戳的陈旧位置和电量，然后据此规划路线。
                        # 只发真正新鲜的数据。
                        age = live.seed_age_seconds(car_id)
                        if age is not None and age > settings.abrp_max_age_seconds:
                            continue
                        last = live.last_update.get(car_id, 0)
                        if not last or now - last > settings.abrp_max_age_seconds:
                            continue
                        if now - self._last_sent.get(car_id, 0) < self._interval_for(car):
                            continue
                        tlm = self.build(car)
                        if tlm is None:
                            continue
                        self._last_sent[car_id] = now
                        await self._post(client, tlm)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 中继出错不该拖垮进程
                    log.exception("ABRP relay loop error")


relay = AbrpRelay()
