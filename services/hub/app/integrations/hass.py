"""Home Assistant MQTT 自动发现 —— 只发 **OhmyTesla 自己算出来的** 实体。

分工要说清楚：TeslaMate v4.1.0 起自带 HA 自动发现，车辆本身的那 60 来个
实体（电量、位置、车门、胎压……）由它负责，这里**不重复发**，否则 HA 里
会出现两套一模一样的实体。

这里只发它没有、而本项目算出来的东西：
  - 哨兵会话状态（进行中 / 空闲）与最近一次事件
  - 数据管道是否中断（看门狗）
  - 停放耗电（kWh/天）
  - 待办的保养项数量

有了这些，HA 那边就能写「哨兵触发 → 打开车库灯 + 响蜂鸣器」这类自动化，
而这正是自托管相对商业 App 的意义所在。
"""

import asyncio
import contextlib
import json
import logging
import time

from ..alerts.store import store
from ..config import settings
from ..livestate import live

log = logging.getLogger(__name__)

PUBLISH_INTERVAL_S = 60
DISCOVERY_INTERVAL_S = 900      # 周期重发，HA 重启后不用等我们重启

# HA 的发现前缀，绝大多数安装都是 homeassistant
DISCOVERY_PREFIX = "homeassistant"
STATE_BASE = "ohmytesla"

DEVICE = {
    "identifiers": ["ohmytesla_hub"],
    "name": "OhmyTesla",
    "manufacturer": "OhmyTesla",
    "model": "Self-hosted Tesla hub",
}

# HA 用它在实体页上标出"这些实体是谁发的"，排障时很有用
ORIGIN = {"name": "OhmyTesla", "sw_version": "1.0"}

# (对象 id, 名称, 组件类型, device_class/None, 单位/None, 图标)
#
# 组件类型必须跟载荷对得上：data_stale 发的是 ON/OFF，声明成 sensor 的话
# HA 里会得到一个字符串实体，写不了 `state: 'on'` 的自动化条件，
# 而这恰恰是它唯一的用途。
ENTITIES = [
    ("sentry_state",    "Sentry activity",         "sensor",        None,      None,   "mdi:shield-car"),
    ("last_alert",      "Last alert",              "sensor",        None,      None,   "mdi:bell-alert"),
    ("data_stale",      "Data collection stalled", "binary_sensor", "problem", None,   "mdi:database-alert"),
    ("vampire_kwh_day", "Vampire drain",           "sensor",        "energy",  "kWh",  "mdi:battery-minus"),
    ("maintenance_due", "Maintenance due",         "sensor",        None,      None,   "mdi:wrench-clock"),
    # 下面几个是这一轮新算出来的，HA 侧没有任何来源
    ("sleep_score",     "Sleep score",             "sensor",        None,      "%",    "mdi:sleep"),
    ("logging_coverage", "Logging coverage",       "sensor",        None,      "%",    "mdi:database-check"),
    ("fsd_share",       "Autonomous share",        "sensor",        None,      "%",    "mdi:steering"),
]

# 旧版逐实体发现留下的 retained 主题。不主动撤销的话，HA 里会同时挂着
# 新旧两套实体，旧的那套值永远冻在最后一次上报 —— 显示"哨兵：空闲"
# 却再也不更新，比没有更糟。
_LEGACY_OBJECTS = ["sentry_state", "last_alert", "data_stale",
                   "vampire_kwh_day", "maintenance_due"]


class HassDiscovery:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._announced = False

    def enabled(self) -> bool:
        return bool(settings.hass_discovery)

    def start(self) -> None:
        if not self.enabled():
            # 关掉之后要主动撤销：发现报文和状态都是 retained 的，不撤的话
            # HA 里那几个实体会永远挂着，而且值冻在最后一次上报 ——
            # 一个显示"哨兵：空闲"却再也不会更新的实体比没有更糟。
            # 发空载荷（retained）就是 MQTT 里"删除这个主题"的标准做法。
            self._retract()
            return
        self._task = asyncio.create_task(self._run(), name="hass-discovery")

    def _retract(self) -> None:
        live.publish(f"{DISCOVERY_PREFIX}/device/ohmytesla_hub/config", "", retain=True)
        for obj in _LEGACY_OBJECTS:
            live.publish(f"{DISCOVERY_PREFIX}/sensor/ohmytesla/{obj}/config", "", retain=True)
        for obj, *_ in ENTITIES:
            live.publish(f"{STATE_BASE}/{obj}", "", retain=True)
        log.info("Home Assistant discovery retracted (HASS_DISCOVERY is off)")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ---------- 发现报文 ----------

    def _announce(self) -> None:
        """一条设备级发现报文带出全部实体。

        HA 2024.11 起支持 `homeassistant/device/<id>/config`，把整台设备的
        实体放在一个 components 映射里。上游 TeslaMate v4.1.0 已经迁过去了，
        这里跟上，理由不是赶时髦：逐实体格式下每加一个实体就是一条新的
        retained 报文，删实体要记得单独撤销，久了 HA 里会积一堆孤儿。
        """
        components = {}
        for obj, name, comp, dev_class, unit, icon in ENTITIES:
            cfg = {
                "platform": comp,
                "name": name,
                "unique_id": f"ohmytesla_{obj}",
                "state_topic": f"{STATE_BASE}/{obj}",
                "icon": icon,
            }
            if dev_class:
                cfg["device_class"] = dev_class
            if unit:
                cfg["unit_of_measurement"] = unit
                cfg["state_class"] = "measurement"
            if comp == "binary_sensor":
                cfg["payload_on"] = "ON"
                cfg["payload_off"] = "OFF"
            components[obj] = cfg

        live.publish(
            f"{DISCOVERY_PREFIX}/device/ohmytesla_hub/config",
            json.dumps({"device": DEVICE, "origin": ORIGIN, "components": components},
                       ensure_ascii=False),
            retain=True,
        )
        # 首次宣告时顺手撤掉旧格式，避免新旧两套实体并存
        if not self._announced:
            self._announced = True
            for obj in _LEGACY_OBJECTS:
                live.publish(f"{DISCOVERY_PREFIX}/sensor/ohmytesla/{obj}/config", "", retain=True)
        log.info("published device-level Home Assistant discovery (%d entities)", len(components))

    # ---------- 状态 ----------

    async def _publish_state(self) -> None:
        from ..alerts.sentry import tracker
        from ..alerts.watchdog import watchdog

        # 哨兵：有活动中的会话就是 active
        active = bool(getattr(tracker, "_sessions", None))
        live.publish(f"{STATE_BASE}/sentry_state", "active" if active else "idle", retain=True)

        alerts = await store.recent_alerts(1)
        live.publish(
            f"{STATE_BASE}/last_alert",
            (alerts[0].get("title") or "")[:250] if alerts else "none",
            retain=True,
        )

        st = watchdog.state()
        live.publish(f"{STATE_BASE}/data_stale",
                     "ON" if st.get("stale") else "OFF", retain=True)

        # 停放耗电偏贵（要跑几条 SQL），一小时算一次就够
        now = time.time()
        if now - getattr(self, "_vamp_at", 0) > 3600:
            self._vamp_at = now
            with contextlib.suppress(Exception):
                from ..insights import queries

                cid = next((int(k) for k in live.cars if k.isdigit()), None)
                if cid is not None:
                    v = await queries.vampire_drain(cid, 90)
                    if v.get("avg_kwh_per_day") is not None:
                        live.publish(f"{STATE_BASE}/vampire_kwh_day",
                                     str(v["avg_kwh_per_day"]), retain=True)

        with contextlib.suppress(Exception):
            from ..alerts import maintenance

            cid = next((k for k in live.cars if k.isdigit()), None)
            odo = None
            if cid:
                with contextlib.suppress(TypeError, ValueError):
                    odo = float(live.cars[cid].get("odometer") or 0) or None
            due = sum(1 for i in await maintenance.status(odo) if i["due"])
            live.publish(f"{STATE_BASE}/maintenance_due", str(due), retain=True)

        # 睡眠评分与采集覆盖率：这两个数字 HA 侧没有任何来源，而它们恰好是
        # "我的数据到底准不准"和"车该睡的时候睡了没有"的直接答案。
        # 跟停车耗电共用一小时的节流窗口，别为了两个百分比每分钟跑一遍 SQL。
        if now - getattr(self, "_slow_at", 0) > 3600:
            self._slow_at = now
            cid = next((int(k) for k in live.cars if k.isdigit()), None)
            if cid is not None:
                with contextlib.suppress(Exception):
                    from ..insights import journey

                    st = await journey.states_breakdown(cid, 30)
                    if st.get("sleep_score") is not None:
                        live.publish(f"{STATE_BASE}/sleep_score",
                                     str(st["sleep_score"]), retain=True)
                    cov = await journey.unlogged_km(cid)
                    if cov.get("coverage") is not None:
                        live.publish(f"{STATE_BASE}/logging_coverage",
                                     str(cov["coverage"]), retain=True)
            with contextlib.suppress(Exception):
                from ..dashcam import index as sei_index

                fsd = await sei_index.fsd_summary(90)
                if fsd.get("has_data") and fsd["share"].get("autonomous") is not None:
                    live.publish(f"{STATE_BASE}/fsd_share",
                                 str(fsd["share"]["autonomous"]), retain=True)

    async def _run(self) -> None:
        await asyncio.sleep(30)     # 等 MQTT 连上
        last_announce = 0.0
        while True:
            try:
                now = time.time()
                if now - last_announce > DISCOVERY_INTERVAL_S:
                    self._announce()
                    last_announce = now
                await self._publish_state()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("hass discovery publish failed")
            await asyncio.sleep(PUBLISH_INTERVAL_S)


discovery = HassDiscovery()
