"""订阅 TeslaMate 的 MQTT 主题，维护每辆车的实时状态快照。

主题格式: teslamate/[namespace/]cars/{car_id}/{key}
所有值都是字符串；这里保留原始字符串，由消费方自行解析。

状态变化会派发给:
  1. 告警引擎 (on_transition 回调)
  2. SSE 订阅者 (门户页面实时刷新)
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import timezone

import aiomqtt

from .config import settings

log = logging.getLogger(__name__)

_SENTRY_STATE_RE = re.compile(r"(Off|Idle|Armed|Aware|Panic|Quiet)")

TransitionHook = Callable[[str, str, str | None, str], Awaitable[None]]
# (car_id, key, old_value, new_value)


class LiveState:
    def __init__(self) -> None:
        self.cars: dict[str, dict[str, str]] = {}
        self.last_update: dict[str, float] = {}
        self._hooks: list[TransitionHook] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._outbox: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=200)
        self._vin_map: dict[str, str] = {}
        self._vin_map_at: float = 0.0
        self._fleet_reconnect: dict[str, float] = {}
        # 从数据库补出来的值（还没被 MQTT 证实过），以及它们真正成立的时刻
        self._seeded: set[str] = set()
        self.seeded_at: dict[str, float] = {}

    def publish(self, topic: str, payload: str) -> None:
        """向 MQTT 发布消息（如哨兵事件回发，供 HomeAssistant/Node-RED 联动）。"""
        try:
            self._outbox.put_nowait((topic, payload))
        except asyncio.QueueFull:
            log.warning("MQTT outbox full, dropping %s", topic)

    # ---------- 消费端 ----------

    def add_transition_hook(self, hook: TransitionHook) -> None:
        self._hooks.append(hook)

    def snapshot(self) -> dict:
        out = {}
        for car_id, values in self.cars.items():
            v = dict(values)
            age = self.seed_age_seconds(car_id)
            if age is not None:
                # 界面据此说明"这是最后已知状态"，而不是把旧数据当实时值显示
                v["_seed_age"] = str(int(age))
            out[car_id] = v
        return out

    def get(self, car_id: str, key: str, default: str | None = None) -> str | None:
        return self.cars.get(car_id, {}).get(key, default)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def is_subscribed(self, q: asyncio.Queue) -> bool:
        return q in self._subscribers

    # ---------- 启动兜底 ----------

    async def seed_from_db(self) -> int:
        """用 TeslaMate 数据库里的最后已知状态填满快照。

        MQTT 是 retained 的，但车睡着时 broker 上可能一条都没有——重启后
        首页会一直空到车自己醒来，可能是好几个小时。这里从数据库补出
        最后已知的一组值，并记下它们成立的时刻，前端据此显示"最后一次
        看到是 X 之前"，而不是把两小时前的电量当作实时数据。

        补出来的值只用于显示：不写 last_update（那是看门狗判断"数据还在
        流"的依据），也不触发状态跳变回调（那会造成假哨兵告警）。
        """
        from . import db

        seeded_cars = 0
        try:
            cars = await db.fetch(
                "SELECT id, name, model, trim_badging, exterior_color, wheel_type,"
                " spoiler_type, marketing_name FROM cars ORDER BY id"
            )
        except Exception as e:  # noqa: BLE001 — 数据库还没起来就算了，MQTT 照常工作
            log.info("skipping DB seed: %s", e)
            return 0

        for car in cars:
            try:
                seeded_cars += await self._seed_one(car)
            except Exception as e:  # noqa: BLE001 — 一辆车补不出来不该拖垮启动
                log.warning("could not seed car %s: %s", car["id"], e)

        if seeded_cars:
            log.info("seeded last-known state for %d car(s) from the database", seeded_cars)
        return seeded_cars

    async def _seed_one(self, car) -> int:
        from . import db

        car_id = str(car["id"])
        vals: dict[str, str] = {}

        def put(key: str, value, *, digits: int | None = None) -> None:
            if value is None:
                return
            if isinstance(value, bool):
                vals[key] = "true" if value else "false"
            elif digits is not None:
                vals[key] = f"{float(value):.{digits}f}"
            else:
                vals[key] = str(value)

        put("display_name", car["name"] or car["marketing_name"] or car["model"])
        for k in ("model", "trim_badging", "exterior_color", "wheel_type", "spoiler_type"):
            put(k, car[k])

        pos = await db.fetchrow(
            "SELECT date, latitude, longitude, speed, power, odometer, battery_level,"
            " usable_battery_level, ideal_battery_range_km, est_battery_range_km,"
            " rated_battery_range_km, outside_temp, inside_temp, elevation, is_climate_on,"
            " tpms_pressure_fl, tpms_pressure_fr, tpms_pressure_rl, tpms_pressure_rr"
            " FROM positions WHERE car_id = $1 ORDER BY date DESC LIMIT 1",
            car["id"],
        )
        if pos is not None:
            for k in ("latitude", "longitude", "speed", "power", "battery_level",
                      "usable_battery_level", "outside_temp", "inside_temp", "elevation",
                      "is_climate_on", "tpms_pressure_fl", "tpms_pressure_fr",
                      "tpms_pressure_rl", "tpms_pressure_rr"):
                put(k, pos[k])
            put("odometer", pos["odometer"], digits=2)
            for k in ("ideal_battery_range_km", "est_battery_range_km", "rated_battery_range_km"):
                put(k, pos[k], digits=2)
            if pos["date"] is not None:
                self.seeded_at[car_id] = pos["date"].replace(tzinfo=timezone.utc).timestamp()

        st = await db.fetchrow(
            "SELECT state, start_date FROM states WHERE car_id = $1"
            " ORDER BY start_date DESC LIMIT 1",
            car["id"],
        )
        if st is not None:
            put("state", st["state"])
            if st["start_date"] is not None:
                put("since", st["start_date"].replace(tzinfo=timezone.utc).isoformat())

        upd = await db.fetchrow(
            "SELECT version FROM updates WHERE car_id = $1 AND end_date IS NOT NULL"
            " ORDER BY end_date DESC LIMIT 1",
            car["id"],
        )
        if upd is not None:
            put("version", upd["version"])

        # 只补空位：MQTT 已经送到的永远优先
        slot = self.cars.setdefault(car_id, {})
        for k, v in vals.items():
            if k not in slot:
                slot[k] = v
                self._seeded.add(f"{car_id}/{k}")
        return 1 if vals else 0

    def seed_age_seconds(self, car_id: str) -> float | None:
        """这辆车的显示数据有多旧（秒）。None = 数据是 MQTT 实时送来的。"""
        if not any(m.startswith(f"{car_id}/") for m in self._seeded):
            return None
        at = self.seeded_at.get(car_id)
        return None if at is None else max(0.0, time.time() - at)

    # ---------- MQTT 循环 ----------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mqtt-listener")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        prefix = "teslamate"
        if settings.mqtt_namespace:
            prefix = f"teslamate/{settings.mqtt_namespace}"
        topic_filter = f"{prefix}/cars/#"

        fleet_filter = f"{settings.fleet_topic_prefix}/#"

        while True:
            sender: asyncio.Task | None = None
            try:
                kwargs: dict = {}
                if settings.mqtt_username:
                    kwargs["username"] = settings.mqtt_username
                    kwargs["password"] = settings.mqtt_password or None
                async with aiomqtt.Client(
                    hostname=settings.mqtt_host,
                    port=settings.mqtt_port,
                    identifier="ohmytesla-hub",
                    **kwargs,
                ) as client:
                    log.info("MQTT connected, subscribing %s + %s", topic_filter, fleet_filter)
                    await client.subscribe(topic_filter, qos=1)
                    await client.subscribe(fleet_filter, qos=1)

                    async def send_outbox():
                        while True:
                            topic, payload = await self._outbox.get()
                            try:
                                await client.publish(topic, payload, qos=1)
                            except Exception:
                                # 发布失败（断线瞬间）：塞回队列等重连后重发
                                with contextlib.suppress(asyncio.QueueFull):
                                    self._outbox.put_nowait((topic, payload))
                                raise

                    sender = asyncio.create_task(send_outbox())
                    async for message in client.messages:
                        topic = str(message.topic)
                        if topic.startswith(f"{settings.fleet_topic_prefix}/"):
                            await self._handle_fleet(message)
                        else:
                            await self._handle(prefix, message)
            except (aiomqtt.MqttError, OSError) as e:
                log.warning("MQTT connection lost (%s); retrying in 5s", e)
                await asyncio.sleep(5)
            finally:
                if sender:
                    sender.cancel()
                    # sender 可能已带异常退出——不能让它的异常杀掉整个监听循环
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await sender

    async def _car_key_for_vin(self, vin: str) -> str:
        """VIN → TeslaMate car_id。

        必须映射：否则哨兵告警会挂在 "vin:xxx" 这个假车上，
        拿不到位置/地理围栏/电量——恰恰是入侵告警最需要的信息。
        """
        cached = self._vin_map.get(vin)
        if cached:
            return cached
        if time.time() - self._vin_map_at < 300:
            return f"vin:{vin}"  # 刚查过还没有，别反复打数据库
        try:
            from . import db

            rows = await db.fetch("SELECT id, vin FROM cars WHERE vin IS NOT NULL")
            self._vin_map = {r["vin"]: str(r["id"]) for r in rows}
            self._vin_map_at = time.time()
        except Exception:  # noqa: BLE001 - 数据库没就绪就先用占位键
            self._vin_map_at = time.time()
        return self._vin_map.get(vin, f"vin:{vin}")

    async def _handle_fleet(self, message) -> None:
        """官方 fleet-telemetry MQTT dispatcher 消息。

        主题: {topic_base}/{VIN}/v/{FieldName}，另有 {topic_base}/{VIN}/connectivity。
        字段载荷是**裸 JSON 字符串**（含引号），例如 "SentryModeStateAware"。
        目前关心 SentryMode（哨兵真实状态流）与 connectivity（断线重放门）。
        """
        topic = str(message.topic)
        parts = topic.split("/")
        n_prefix = len(settings.fleet_topic_prefix.split("/"))  # 前缀可含多级
        if len(parts) < n_prefix + 2:
            return
        vin = parts[n_prefix]
        kind = parts[n_prefix + 1] if len(parts) > n_prefix + 1 else ""
        field = parts[-1]

        payload = message.payload
        text = (
            payload.decode("utf-8", "replace")
            if isinstance(payload, (bytes, bytearray)) else str(payload)
        )

        # 连接状态：车辆重连后会重放最多数千条缓冲消息，且 MQTT 派发器
        # 丢掉了 created_at/is_resend，无法区分新旧。所以重连后短暂静默，
        # 否则一次断网就会连发一串"有人在你车旁"的假告警。
        if kind == "connectivity":
            status = ""
            try:
                data = json.loads(text)
                status = str(data.get("Status") or data.get("status") or "")
            except ValueError:
                status = text
            if "CONNECTED" in status.upper() and "DIS" not in status.upper():
                self._fleet_reconnect[vin] = time.time()
                log.info("fleet telemetry connected: VIN…%s (replay gate armed)", vin[-6:])
            return

        if kind != "v" or field != "SentryMode":
            return

        gate = self._fleet_reconnect.get(vin, 0)
        if time.time() - gate < settings.fleet_replay_gate_seconds:
            log.info("suppressed SentryMode during replay window for VIN…%s", vin[-6:])
            return

        try:
            value = json.loads(text)
            raw = value if isinstance(value, str) else str(value)
        except ValueError:
            raw = text
        m = _SENTRY_STATE_RE.search(raw)
        if not m:
            return
        state = m.group(1)

        await self.ingest_fleet_sentry(vin, state)

    async def ingest_fleet_sentry(self, vin: str, state: str) -> None:
        """把一条哨兵状态喂进引擎。MQTT 直连和托管中转桥接都走这里。"""
        car_key = await self._car_key_for_vin(vin)
        car = self.cars.setdefault(car_key, {"display_name": f"VIN…{vin[-6:]}"})
        old = car.get("fleet_sentry_state")
        if old == state:
            return
        car["fleet_sentry_state"] = state
        self.last_update[car_key] = time.time()
        for hook in self._hooks:
            try:
                await hook(car_key, "fleet_sentry_state", old, state)
            except Exception:  # noqa: BLE001
                log.exception("fleet transition hook failed for %s", vin)

    async def vins(self) -> list[str]:
        """TeslaMate 里已知的 VIN 列表（桥接器用来决定订阅哪些车）。"""
        try:
            from . import db

            rows = await db.fetch("SELECT vin FROM cars WHERE vin IS NOT NULL")
            return [r["vin"] for r in rows]
        except Exception:  # noqa: BLE001
            return []

    async def _handle(self, prefix: str, message) -> None:
        topic = str(message.topic)
        rest = topic[len(prefix) + 1 :]  # cars/{id}/{key...}
        parts = rest.split("/")
        if len(parts) < 3 or parts[0] != "cars":
            return
        car_id = parts[1]
        key = "/".join(parts[2:])
        payload = message.payload
        value = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)

        car = self.cars.setdefault(car_id, {})
        marker = f"{car_id}/{key}"
        was_seeded = marker in self._seeded
        self._seeded.discard(marker)

        old = car.get(key)
        if old == value:
            return
        # 数据库补出来的旧值不能当成"刚刚发生的变化"——否则每次重启，
        # 停机期间攒下的状态差异会在几秒内集中炸成一串哨兵告警。
        if was_seeded:
            old = None
        car[key] = value
        self.last_update[car_id] = time.time()

        for hook in self._hooks:
            try:
                await hook(car_id, key, old, value)
            except Exception:  # noqa: BLE001
                log.exception("transition hook failed for %s/%s", car_id, key)

        event = json.dumps(
            {"car_id": car_id, "key": key, "value": value}, ensure_ascii=False
        )
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(q)


live = LiveState()
