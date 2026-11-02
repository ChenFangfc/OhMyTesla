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
import secrets
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import aiomqtt

from .config import settings

# Fleet Telemetry alerts that mean someone is at the car. Anything older than
# _FLEET_ALERT_FRESH_S by the car's own clock is a replay from a reconnect.
# One MQTT client id per process. A fixed id made every second hub on the same
# broker (a dev copy, a test run, a restart overlapping the old container)
# take the session over, and the broker then kicked the other one off —
# live, the production hub was disconnected ~8 times a minute and blind to
# Sentry for 5 s each time. Nothing relies on a persistent session (clean
# session, QoS 0 subscriptions, retained state re-sent on connect).
_MQTT_CLIENT_ID = f"ohmytesla-hub-{socket.gethostname()[:20]}-{secrets.token_hex(3)}"

_FLEET_ALERTS = ("alarmTriggered", "handlePullWithoutAuth")
_FLEET_ALERT_FRESH_S = 180
# Older than "fresh" still matters when the car was out of signal (a garage,
# a jammer, a tow): an alarm is delivered late, marked as such, for this long
# — or for as long as it's still sounding; a handle pull for half an hour.
_FLEET_ALARM_LATE_MAX_S = 6 * 3600
_FLEET_PULL_LATE_MAX_S = 30 * 60


def _age_seconds(stamp: str) -> float | None:
    """Seconds since an RFC 3339 timestamp, or None when there isn't one."""
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return time.time() - t.timestamp()

log = logging.getLogger(__name__)

_SENTRY_STATE_RE = re.compile(r"(Off|Idle|Armed|Aware|Panic|Quiet)")
# Fleet Telemetry 的 DisplayState 枚举（与 Owner API 的 center_display_state
# 不同编号，见 alerts/engine.py 里的说明）。只关心 Lock —— 有人碰了锁着的车。
_DISPLAY_STATE_RE = re.compile(
    r"(Off|Dim|Accessory|Driving|Charging|Lock|Sentry|Dog|Entertainment|On)")

TransitionHook = Callable[[str, str, str | None, str], Awaitable[None]]
# (car_id, key, old_value, new_value)
SnapshotHook = Callable[[str], None]
# (car_id): one complete reading of the car has been handled, see end_snapshot()
_SNAPSHOT = "\x00snapshot"     # a queue marker; no data source sends this key


class LiveState:
    def __init__(self) -> None:
        self.cars: dict[str, dict[str, str]] = {}
        self.last_update: dict[str, float] = {}
        self._hooks: list[TransitionHook] = []
        self._snapshot_hooks: list[SnapshotHook] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._outbox: asyncio.Queue[tuple[str, str, bool]] = asyncio.Queue(maxsize=200)
        self._vin_map: dict[str, str] = {}
        self._vin_map_at: float = 0.0
        self._fleet_reconnect: dict[str, float] = {}
        # (VIN, alert name) -> StartedAt already delivered. One alert is
        # published again when it ends (same StartedAt, now with EndedAt).
        self._fleet_alert_seen: dict[tuple[str, str], str] = {}
        # 从数据库补出来的值（还没被 MQTT 证实过），以及它们真正成立的时刻
        self._seeded: set[str] = set()
        self.seeded_at: dict[str, float] = {}
        # Keys whose last message was TeslaMate 4.4's empty payload ("field
        # missing in this poll"): the last known value is kept but flagged.
        self._unknown: set[str] = set()
        # When each car last sent *live* data (not seeded, not replayed). A car
        # that never does is no longer in the account — the watchdog must not
        # call that an outage every night (audit F4).
        self.live_seen: dict[str, float] = {}
        # Hooks run in one ordered worker per car, off the MQTT reader: a slow
        # push or command must not stall every other car's messages (audit F12).
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        """向 MQTT 发布消息（如哨兵事件回发，供 HomeAssistant/Node-RED 联动）。

        retain=True 用于 Home Assistant 的自动发现与状态主题：不 retain 的话，
        HA 只有在我们**恰好发的那一刻**连着才看得到，重启一次实体就全没了。
        事件类消息（sentry_event）则必须不 retain —— 那是"刚刚发生了什么"，
        retain 会让新订阅者一连上就收到一条陈年旧事当成新告警。
        """
        try:
            self._outbox.put_nowait((topic, payload, retain))
        except asyncio.QueueFull:
            log.warning("MQTT outbox full, dropping %s", topic)

    # ---------- 消费端 ----------

    def add_transition_hook(self, hook: TransitionHook) -> None:
        self._hooks.append(hook)

    def add_snapshot_hook(self, hook: SnapshotHook) -> None:
        self._snapshot_hooks.append(hook)

    def end_snapshot(self, car_id: str) -> None:
        """One complete reading of this car has just been ingested (a fast-poll
        response). Snapshot hooks run after every transition it caused has been
        handled, so a listener waiting for the rest of a burst can decide now."""
        q = self._queues.get(car_id)
        if q is None or not self._snapshot_hooks:
            return
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait((_SNAPSHOT, None, None))

    def snapshot(self) -> dict:
        out = {}
        for car_id, values in self.cars.items():
            v = dict(values)
            age = self.seed_age_seconds(car_id)
            if age is not None:
                # 界面据此说明"这是最后已知状态"，而不是把旧数据当实时值显示
                v["_seed_age"] = str(int(age))
            seen = self.live_seen.get(car_id)
            v["_live"] = "1" if seen else "0"
            if seen:
                v["_live_age"] = str(int(time.time() - seen))
            unknown = [m.split("/", 1)[1] for m in self._unknown if m.startswith(f"{car_id}/")]
            if unknown:
                v["_unknown"] = ",".join(sorted(unknown))
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
        if at is None:
            return None
        # 补位之后实时数据已经在流了，这辆车就是"实时"的。不能等每个补过的键都被
        # 覆盖一遍：TeslaMate 的 display_name 是 ''、从不 retained，只重启 hub 的话
        # 那个标记永远清不掉，活着的车会被一直报成"数据库里的旧状态"（审计 F13），
        # ABRP 中继也会因此永久跳过这辆车。
        if self.live_seen.get(car_id, 0) > at:
            return None
        return max(0.0, time.time() - at)

    # ---------- MQTT 循环 ----------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mqtt-listener")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for w in self._workers.values():
            w.cancel()

    async def _run(self) -> None:
        prefix = "teslamate"
        if settings.mqtt_namespace:
            prefix = f"teslamate/{settings.mqtt_namespace}"
        topic_filter = f"{prefix}/cars/#"

        fleet_filter = f"{settings.fleet_topic_prefix}/#"

        while True:
            sender: asyncio.Task | None = None
            reader: asyncio.Task | None = None
            try:
                kwargs: dict = {}
                if settings.mqtt_username:
                    kwargs["username"] = settings.mqtt_username
                    kwargs["password"] = settings.mqtt_password or None
                async with aiomqtt.Client(
                    hostname=settings.mqtt_host,
                    port=settings.mqtt_port,
                    identifier=_MQTT_CLIENT_ID,
                    **kwargs,
                ) as client:
                    log.info("MQTT connected, subscribing %s + %s", topic_filter, fleet_filter)
                    await client.subscribe(topic_filter, qos=1)
                    await client.subscribe(fleet_filter, qos=1)

                    async def send_outbox():
                        while True:
                            topic, payload, retain = await self._outbox.get()
                            try:
                                await client.publish(topic, payload, qos=1, retain=retain)
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:  # noqa: BLE001
                                # 发布失败（断线瞬间）：塞回队列等重连后重发。
                                #
                                # 这里以前是 `raise`，后果很隐蔽：sender 任务当场死掉，
                                # 而外层 `async for message` 还在正常收消息，连接看起来
                                # 完全健康 —— 但从这一刻起**一条都发不出去**。哨兵事件
                                # 回发、HA 自动发现、ABRP 中继全部静默停摆，直到连接
                                # 因为别的原因断开才会恢复。/healthz 照样 200。
                                with contextlib.suppress(asyncio.QueueFull):
                                    self._outbox.put_nowait((topic, payload, retain))
                                log.warning("MQTT publish failed (%s); retrying", e)
                                await asyncio.sleep(1)

                    async def read_messages():
                        async for message in client.messages:
                            topic = str(message.topic)
                            if topic.startswith(f"{settings.fleet_topic_prefix}/"):
                                await self._handle_fleet(message)
                            else:
                                await self._handle(prefix, message)

                    sender = asyncio.create_task(send_outbox())
                    reader = asyncio.create_task(read_messages())
                    # 两个方向互相监督：任何一边退出（正常结束或抛异常）都把
                    # 整条连接回收重连，避免出现"只收不发"或"只发不收"的半死状态。
                    done, _ = await asyncio.wait(
                        {sender, reader}, return_when=asyncio.FIRST_COMPLETED)
                    reader.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await reader
                    for tsk in done:
                        if tsk.cancelled():
                            continue
                        exc = tsk.exception()
                        if exc is not None:
                            raise exc  # noqa: TRY301 - 就地 raise 是为了让紧跟其后的 except 统一收口，抽成函数反而绕
            except asyncio.CancelledError:
                raise
            except (aiomqtt.MqttError, OSError) as e:
                log.warning("MQTT connection lost (%s); retrying in 5s", e)
                await asyncio.sleep(5)
            except Exception:
                # 只接 MqttError/OSError 的话，_handle 里冒出来的任何别的异常
                # （载荷形状意外、某个 hook 抛错）都会把这个任务永久杀死：
                # 没人守护它、没人重启它，而 /healthz 照样返回 200 ——
                # 正是看门狗注释里说的那种"安静地不工作"。
                # 而且此时 last_update 也不再前进，连看门狗自己都失灵了。
                log.exception("MQTT listener crashed; restarting in 5s")
                await asyncio.sleep(5)
            finally:
                # 关停时外层任务被取消，上面的 reader.cancel() 走不到；不在这里收掉的话，
                # 客户端一关 reader 就带着 MqttError 结束，每次正常停机都记一条
                # "Task exception was never retrieved"，看着像崩溃。
                for tsk in (sender, reader):
                    if tsk:
                        tsk.cancel()
                        # 可能已带异常退出——不能让它的异常杀掉整个监听循环
                        with contextlib.suppress(Exception, asyncio.CancelledError):
                            await tsk

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

        if kind == "alerts":
            # Tesla vehicle alerts (needs `alert_types: ["service"]` in the car's
            # telemetry config). Topic {base}/{VIN}/alerts/{Name}/current|history;
            # the name is only in the topic, the payload is
            # {"StartedAt", "EndedAt"?, "Audiences"}. `history` repeats what
            # `current` already said, so only `current` counts.
            if field != "current" or len(parts) < n_prefix + 4:
                return
            name = parts[n_prefix + 2]
            if not any(k in name for k in _FLEET_ALERTS):
                return
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            started = str(data.get("StartedAt") or "") if isinstance(data, dict) else ""
            if started and self._fleet_alert_seen.get((vin, name)) == started:
                return                                   # the same alert, now ended
            if started:
                self._fleet_alert_seen[(vin, name)] = started
            age = _age_seconds(started)
            value = name
            if age is not None and age > _FLEET_ALERT_FRESH_S:
                # The car's own timestamp says this happened a while ago (it
                # was offline). Never drop an alarm for that: deliver it late,
                # marked, unless it's long over.
                alarm = "alarmTriggered" in name
                still_on = isinstance(data, dict) and not data.get("EndedAt")
                limit = _FLEET_ALARM_LATE_MAX_S if alarm else _FLEET_PULL_LATE_MAX_S
                if not ((alarm and still_on) or age <= limit):
                    log.info("fleet alert %s for VIN…%s is %ds old, not alerting", name, vin[-6:], int(age))
                    return
                value = f"{name}|late={int(age)}"
            elif age is None:
                gate = self._fleet_reconnect.get(vin, 0)
                if time.time() - gate < settings.fleet_replay_gate_seconds and "alarm" not in name:
                    return
            await self._ingest_fleet_field(vin, "fleet_alert", value, force=True)
            return

        if kind != "v":
            return
        # 只关心这两个会产生告警的字段
        if field not in ("SentryMode", "CenterDisplay", "DisplayState"):
            return

        # 重放门必须盖住**所有会触发告警的字段**。车辆断线重连后会把缓冲的
        # 几千条消息一次性重放，而 MQTT 派发器丢掉了 created_at/is_resend，
        # 分不出新旧。之前这道门只挡 SentryMode，CenterDisplay 的分支又写在
        # 它前面 —— 一次断网重连就会连发一串"有人碰了你锁着的车"。
        gate = self._fleet_reconnect.get(vin, 0)
        if time.time() - gate < settings.fleet_replay_gate_seconds and not (
                field == "SentryMode" and "Panic" in text):
            # An alarm (Panic) is never held back: a car that just reconnected
            # may be reconnecting *because* something is happening to it.
            log.info("suppressed %s during replay window for VIN…%s", field, vin[-6:])
            return

        # CenterDisplay = Lock 表示"有人碰了锁着的车"。哨兵没开时，这是唯一
        # 能察觉有人在动车的信号。注意它的枚举和 Owner API 的
        # center_display_state **不是一套编号**（那边 7=哨兵，这边 7=Lock），
        # 所以走单独的键名，绝不能混进同一个判断里。
        if field in ("CenterDisplay", "DisplayState"):
            m = _DISPLAY_STATE_RE.search(self._unwrap(text))
            if m:
                await self._ingest_fleet_field(vin, "fleet_display_state", m.group(1))
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

    @staticmethod
    def _unwrap(text: str) -> str:
        """字段载荷是裸 JSON 字符串（带引号），也可能是对象。取出里面的值。"""
        try:
            value = json.loads(text)
        except ValueError:
            return text
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for k in ("stringValue", "value", "state"):
                v = value.get(k)
                if isinstance(v, str):
                    return v
        return str(value)

    async def _ingest_fleet_field(self, vin: str, key: str, state: str, force: bool = False) -> None:
        """把一条遥测字段喂进引擎。MQTT 直连和托管中转桥接都走这里。

        `force`: deliver even when the value repeats (alerts are events, not state).
        """
        car_key = await self._car_key_for_vin(vin)
        if car_key.startswith("vin:"):
            # Unknown VIN (DB not ready, or a car that isn't ours): don't invent
            # a placeholder car that the watchdog would later call "stale".
            log.info("fleet %s for unknown VIN…%s ignored", key, vin[-6:])
            return
        car = self.cars.setdefault(car_key, {})
        old = car.get(key)
        if old == state and not force:
            return
        car[key] = state
        # live_seen, not last_update: last_update is TeslaMate's heartbeat
        # (the watchdog's "is logging alive"), which Fleet data must not mask.
        self.live_seen[car_key] = time.time()
        self._dispatch(car_key, key, old, state)

    async def ingest_fleet_sentry(self, vin: str, state: str) -> None:
        """哨兵状态入口。名字保留 —— fleet/bridge.py 也调它。"""
        await self._ingest_fleet_field(vin, "fleet_sentry_state", state)

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
        if key == "sentry_event" or not car_id.isdigit():
            # Our own re-published events (older hubs used teslamate/…) are not
            # TeslaMate data and must not count as a heartbeat (audit F3).
            return
        payload = message.payload
        value = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        # MQTT sets RETAIN only on messages replayed because we (re)subscribed.
        # Those are a snapshot of the past, not something happening now: after
        # a reconnect they used to arrive as fresh "changes" (audit F6).
        replay = bool(getattr(message, "retain", False))
        self.ingest(car_id, key, value, replay=replay)

    def ingest(self, car_id: str, key: str, value: str, *, replay: bool = False,
               source: str = "teslamate") -> None:
        """One value for one car — from TeslaMate MQTT, or from the fast sentry poll."""
        car = self.cars.setdefault(car_id, {})
        marker = f"{car_id}/{key}"
        was_seeded = marker in self._seeded
        self._seeded.discard(marker)

        # 心跳必须记在"消息到达"上，不能记在"值变了"上。TeslaMate 把 healthy
        # 放在 @do_not_retain 里，每个轮询周期无条件发一条（值恒为 true），
        # 这就是它给出的存活信号。
        # 只有 TeslaMate 的实时消息才算它的心跳：快速检查、重连时补发的
        # retained 快照都不能把一个已经停了的 TeslaMate 遮成"还活着"。
        now = time.time()
        if not replay:
            self.live_seen[car_id] = now
            if source == "teslamate":
                self.last_update[car_id] = now

        if value == "" and key != "geofence":
            # TeslaMate 4.4 publishes "" when the car omitted a field this poll
            # (and to clear the retained value). That means *unknown*, not a new
            # value: keep the last known one, flag it, fire nothing. Treating it
            # as a value made a real trunk opening invisible while sentry_mode
            # was "" and re-fired alerts when it came back (audit F2).
            if key in car:
                self._unknown.add(marker)
            return
        self._unknown.discard(marker)

        old = car.get(key)
        if old == value:
            return
        car[key] = value
        # 数据库补出来的旧值、以及重连时的保留消息回放，都不能当成"刚刚发生的变化"。
        if not (was_seeded or replay):
            self._dispatch(car_id, key, old, value)

        event = json.dumps({"car_id": car_id, "key": key, "value": value}, ensure_ascii=False)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(q)

    def emit(self, payload: dict) -> None:
        """Push a non-state event (sentry incident, alert) to SSE subscribers."""
        event = json.dumps(payload, ensure_ascii=False)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(q)

    def _dispatch(self, car_id: str, key: str, old: str | None, value: str) -> None:
        q = self._queues.get(car_id)
        if q is None:
            q = self._queues[car_id] = asyncio.Queue(maxsize=2000)
            self._workers[car_id] = asyncio.get_running_loop().create_task(
                self._worker(car_id, q), name=f"hooks-{car_id}")
        if q.full():
            log.error("hook queue for car %s is full; dropping %s", car_id, key)
            return
        q.put_nowait((key, old, value))

    async def _worker(self, car_id: str, q: asyncio.Queue) -> None:
        while True:
            key, old, value = await q.get()
            if key == _SNAPSHOT:
                for snap in self._snapshot_hooks:
                    try:
                        snap(car_id)
                    except Exception:
                        log.exception("snapshot hook failed for %s", car_id)
                continue
            for hook in self._hooks:
                try:
                    await hook(car_id, key, old, value)
                except Exception:
                    log.exception("transition hook failed for %s/%s", car_id, key)

    def is_active(self, car_id: str, max_age_s: float = 14 * 86400) -> bool:
        """Is this car still in the account? True once it has sent live data, or
        while its last known data is recent. Cars that only exist as old DB rows
        (sold, removed from the account) are not."""
        if car_id in self.live_seen:
            return True
        age = self.seed_age_seconds(car_id)
        return age is not None and age < max_age_s


live = LiveState()
