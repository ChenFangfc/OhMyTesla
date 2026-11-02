"""告警规则引擎。

监听 LiveState 的状态变化 (transition)，匹配规则后经 notifier 推送，
并写入告警历史。

设计要点:
- 启动时 MQTT 会重放 retained 消息，此时 old 为 None，一律不触发告警。
- 静默时段内只放行 critical 级别。
- 每 (车, 规则) 有冷却时间，防止告警轰炸。
- 规则可在界面上单独开关（存 SQLite settings: alert_rules）。
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import settings
from ..i18n.router import t
from ..livestate import live
from . import notifier
from .sentry import car_label, place_label
from .sentry import tracker as sentry_tracker
from .store import store

log = logging.getLogger(__name__)


def _tz():
    """配置的时区（无效或未配置时回退系统时区）。"""
    if settings.teslacam_tz:
        try:
            return ZoneInfo(settings.teslacam_tz)
        except Exception:  # noqa: BLE001
            log.warning("invalid TESLACAM_TZ %r; using system tz", settings.teslacam_tz)
    return datetime.now().astimezone().tzinfo


@dataclass
class Rule:
    id: str
    level: str  # info / warning / critical
    title_zh: str
    title_en: str
    default_enabled: bool = True
    cooldown_factor: float = 1.0
    # How it reaches you by default: "push" (phone), "app" (recorded and shown
    # in OhmyTesla, never pushed) or "off". The owner's rule: only Sentry and
    # genuinely major events push; routine diagnostics stay in the app.
    mode: str = "app"


MODES = ("push", "app", "off")
# The few that deserve a phone buzz by default.
_PUSH_BY_DEFAULT = frozenset({
    "sentry_aware", "sentry_panic", "possible_theft", "data_stale", "tpms_warning",
    "charging_interrupted",
})
# Someone at the car right now: these reach the phone time-sensitive on every
# channel (ntfy ≥ 4, Bark timeSensitive, Web Push Urgency high), so a Focus
# mode doesn't hold them back. Their level still decides the wording and colour.
_URGENT_RULES = frozenset({"sentry_activity", "possible_theft"})


RULES: dict[str, Rule] = {
    r.id: r
    for r in [
        # Owner-explained activity while Sentry was on (a key unlocked the car).
        # Recorded on the timeline; off by default — a break-in doesn't unlock the
        # car, a key does. Forced openings (car still locked) are alarms decided in
        # sentry.py, not these rules.
        Rule("sentry_door_opened", "info", "车门被打开（哨兵开启中）", "Door opened while Sentry was on", default_enabled=False),
        Rule("sentry_trunk_opened", "info", "后备箱被打开（哨兵开启中）", "Trunk opened while Sentry was on", default_enabled=False),
        Rule("sentry_frunk_opened", "info", "前备箱被打开（哨兵开启中）", "Frunk opened while Sentry was on", default_enabled=False),
        Rule("sentry_window_opened", "info", "车窗被打开（哨兵开启中）", "Window opened while Sentry was on", default_enabled=False),
        Rule("sentry_unlocked", "info", "车辆已解锁（哨兵开启中）", "Unlocked while Sentry was on", default_enabled=False),
        # Fleet Telemetry 哨兵真实事件流（需启用 fleet profile，见 docs/FLEET-TELEMETRY.md）
        Rule("sentry_aware", "warning", "👀 哨兵注意：检测到可疑活动", "👀 Sentry: suspicious activity (Aware)", cooldown_factor=0.05),
        Rule("sentry_panic", "critical", "🚨 哨兵警报拉响！", "🚨 Sentry PANIC alarm!", cooldown_factor=0.05),
        # 录像目录监视（teslausb 自动同步 / 手动拷入后触发）
        Rule("new_sentry_clip", "warning", "🎬 新的哨兵录像", "🎬 New sentry clip synced"),
        Rule("sentry_armed", "info", "🛡️ 哨兵模式已开启", "🛡️ Sentry mode armed", default_enabled=False),
        Rule("sentry_disarmed", "info", "🛡️ 哨兵模式已关闭", "🛡️ Sentry mode disarmed", default_enabled=False),
        # 到达/离开某个地点。TeslaMate 把当前所在的地理围栏名发在
        # teslamate/cars/{id}/geofence 上，进出即为该键的一次跳变。
        # 默认关闭：通勤族每天进出好几次，默认开会很吵。
        Rule("geofence_arrive", "info", "📍 车辆已到达", "📍 Vehicle arrived", default_enabled=False),
        Rule("geofence_depart", "info", "📍 车辆已离开", "📍 Vehicle left", default_enabled=False),
        # 无人在车但车在跑，而且超过了设定车速。代客泊车/借车给别人时用得上；
        # 默认关，且需要设置里填一个阈值才会生效。
        Rule("speed_limit", "warning", "🚗 车速超过设定值", "🚗 Speed above your limit",
             default_enabled=False),
        # 超充占位费：充满还不拔枪会按分钟计费。
        Rule("idle_fee_risk", "warning", "💸 超充已充满，继续占位会产生费用",
             "💸 Charging finished at a Supercharger — idle fees may start",
             default_enabled=False),
        # 保养提醒（按里程或日期，规则在设置里配）
        Rule("maintenance_due", "info", "🔧 保养项目到期", "🔧 Maintenance due"),
        Rule("tpms_warning", "warning", "⚠️ 胎压异常", "⚠️ Tire pressure warning"),
        # Power lost mid-charge (breaker, outage, cable). A *stop* below the limit
        # is usually a schedule (8 of 11 live "interruptions" were the off-peak
        # window ending at 06:00), so it is its own, quiet rule.
        Rule("charging_interrupted", "warning", "⚠️ 充电时断电", "⚠️ Charging lost power"),
        Rule("charging_stopped_early", "info", "充电在上限前停止", "Charging stopped before the limit",
             default_enabled=False),
        Rule("charge_complete", "info", "🔋 充电完成", "🔋 Charging complete", default_enabled=False),
        Rule("low_battery", "warning", "🪫 电量过低", "🪫 Battery low", cooldown_factor=12),
        Rule("update_available", "info", "⬇️ 有新的软件更新", "⬇️ Software update available"),
        Rule("teslamate_unhealthy", "warning", "🩺 TeslaMate 数据采集异常", "🩺 TeslaMate unhealthy", cooldown_factor=6),
        # 数据源静默看门狗：告警产品最危险的失败是"什么都没发生"和"系统坏了"长得一样
        Rule("data_stale", "critical", "🔇 数据采集已中断 —— 哨兵告警当前失效！",
             "🔇 Data collection stopped — sentry alerts are DOWN!", cooldown_factor=12),
        Rule("data_resumed", "info", "✅ 数据采集已恢复", "✅ Data collection resumed"),
    ]
}

for _r in RULES.values():
    _r.mode = "off" if not _r.default_enabled else ("push" if _r.id in _PUSH_BY_DEFAULT else "app")

# 规则标题优先用字典（原生英文），字典没有的退回内置双语
from ..i18n.router import catalog as _catalog  # noqa: E402

_CATALOG_KEYS = set(_catalog("en"))


_TPMS_KEYS = {
    "tpms_soft_warning_fl": "tire.front_left",
    "tpms_soft_warning_fr": "tire.front_right",
    "tpms_soft_warning_rl": "tire.rear_left",
    "tpms_soft_warning_rr": "tire.rear_right",
}


def _is_true(v: str | None) -> bool:
    return v == "true"


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


class AlertEngine:
    # 哨兵类规则触发后先等一小段时间再确认：TeslaMate 同一批 Summary 的各键
    # 并发无序发布，车主解锁+关哨兵+开门可能以"开门先到"的顺序出现，
    # 延迟复查可消掉这类误报。
    CONFIRM_DELAY_S = 2.0
    # A charge that stops short is judged two minutes later: by then an owner
    # who ended it by hand has usually unplugged, and a session that paused
    # has resumed. Live, 11 of 11 "stopped early" alerts were false (the end
    # of the off-peak window, DC sessions, a top-up ended by unplugging).
    STOP_GRACE_S = 120.0

    def __init__(self) -> None:
        self._last_fired: dict[tuple[str, str], float] = {}
        self._enabled_cache: dict[str, bool] | None = None
        self._rule_lock = asyncio.Lock()
        # 本次充电过程中见过的最大功率/电压，用来判断是不是直流快充。
        # 不能在 charging_state=Complete 那一刻现取：那时功率已经归零。
        # 也不能用 fast_charger_present —— 它是 charges 表的列，**不在 MQTT 上**
        # （实测 teslamate/cars/1/# 只有 charger_power / voltage / phases 等）。
        self._charge_peak: dict[str, tuple[float, float]] = {}
        self._stop_checks: dict[str, asyncio.Task] = {}

    async def _judge_stop(self, car_id: str) -> None:
        """Charging → Stopped short of the limit: was it really interrupted?"""
        await asyncio.sleep(self.STOP_GRACE_S)
        car = live.cars.get(car_id, {})
        if not _is_true(car.get("plugged_in")) or car.get("charging_state") != "Stopped":
            return                      # unplugged by hand, resumed, or finished
        level = _num(car.get("battery_level"))
        limit = _num(car.get("charge_limit_soc"))
        if level is None or limit is None or level >= limit - 2:
            return
        try:
            from ..insights.charging_stops import classify_stop

            verdict = await classify_stop(
                int(car_id), end_soc=level, charge_limit_soc=limit, is_dc=None,
                scheduled_charging_start_time=car.get("scheduled_charging_start_time"),
                charging_state="Stopped")
        except Exception as e:  # noqa: BLE001 — without the DB, judge as before
            log.info("charge stop on car %s: no learned schedule (%s)", car_id, e)
            verdict = {"interrupted": True, "reason": "unexplained"}
        if not verdict.get("interrupted"):
            log.info("charge stop on car %s not alerted: %s", car_id, verdict.get("reason"))
            return
        await self._fire("charging_stopped_early", car_id, extra=f"{level:.0f}% / {limit:.0f}%")

    async def rule_modes(self) -> dict[str, str]:
        """push / app / off per rule. A rule someone switched off in the old
        on/off UI stays off; an old "on" does not promote a routine rule to
        push — that is exactly the noise the owner asked to stop."""
        modes = await store.get_setting("alert_modes", {}) or {}
        legacy = await store.get_setting("alert_rules", {}) or {}
        out = {}
        for rid, rule in RULES.items():
            m = modes.get(rid)
            if m not in MODES:
                m = "off" if legacy.get(rid) is False else rule.mode
            out[rid] = m
        return out

    async def rule_states(self) -> dict[str, bool]:
        return {rid: m != "off" for rid, m in (await self.rule_modes()).items()}

    async def set_mode(self, rule_id: str, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("mode must be push, app or off")
        async with self._rule_lock:
            saved = await store.get_setting("alert_modes", {}) or {}
            saved[rule_id] = mode
            await store.set_setting("alert_modes", saved)
            self._enabled_cache = None

    async def set_rule(self, rule_id: str, enabled: bool) -> None:
        """Old on/off API: on = the rule's default mode (or push if it was off)."""
        rule = RULES[rule_id]
        await self.set_mode(rule_id, (rule.mode if rule.mode != "off" else "push") if enabled else "off")

    async def _mode(self, rule_id: str) -> str:
        if self._enabled_cache is None:
            self._enabled_cache = await self.rule_modes()
        return self._enabled_cache.get(rule_id, "push")

    async def _enabled(self, rule_id: str) -> bool:
        return await self._mode(rule_id) != "off"

    # ---------- 临时静音 ----------

    async def snooze_state(self) -> dict:
        """{until: epoch|None, scope: 'all'|'noncritical'}。过期的自动当作没静音。"""
        s = await store.get_setting("alert_snooze", {}) or {}
        until = float(s.get("until") or 0)
        if until <= time.time():
            return {"until": None, "scope": s.get("scope", "noncritical"), "active": False}
        return {"until": until, "scope": s.get("scope", "noncritical"), "active": True,
                "seconds_left": int(until - time.time())}

    async def set_snooze(self, minutes: int, scope: str = "noncritical") -> dict:
        """静音 N 分钟。minutes<=0 = 立刻取消。

        scope='noncritical' 时**入侵级仍然会推** —— 这是有意的：
        「今晚别烦我」和「今晚别管我的车被撬」是两件事。想全静音要显式选 all。
        """
        if minutes <= 0:
            await store.set_setting("alert_snooze", {})
        else:
            await store.set_setting("alert_snooze", {
                "until": time.time() + minutes * 60,
                "scope": "all" if scope == "all" else "noncritical",
            })
        return await self.snooze_state()

    async def _snoozed(self, level: str) -> bool:
        s = await self.snooze_state()
        if not s["active"]:
            return False
        return s["scope"] == "all" or level != "critical"

    # ---------- 主入口: LiveState transition hook ----------

    async def handle(self, car_id: str, key: str, old: str | None, new: str) -> None:
        from ..i18n.prefs import get as get_prefs

        loc0 = (await get_prefs())["locale"]
        car = live.cars.get(car_id, {})

        # Every live transition passes through the sentry service first. It only
        # *collects* here; decisions are made once the message burst settles.
        sentry_tracker.on_change(car_id, key, old, new)

        # ---- Fleet Telemetry: authoritative, immediate ----
        if key == "fleet_sentry_state":
            if old != new:
                await sentry_tracker.fleet_sentry(car_id, new, await car_label(car_id))
            return
        if key == "fleet_display_state":
            # Lock = someone touched the locked car — the one signal that works
            # with Sentry *off*. (Not the Owner-API numbering: there 7 = Sentry.)
            if new == "Lock":
                await sentry_tracker.fleet_display_lock(car_id, await car_label(car_id))
            return
        if key == "fleet_alert":
            await sentry_tracker.fleet_alert(car_id, new, await car_label(car_id))
            return

        if key == "sentry_mode" and old is not None:
            await self._fire("sentry_armed" if new == "true" else "sentry_disarmed", car_id)
            return

        if key == "geofence":
            # 空字符串表示"不在任何已知地点"。只在两端有一端非空时才有意义，
            # 一次移动会先离开旧地点再到达新地点，两条都发得出去。
            # Retained replays never reach this hook (livestate filters them), so
            # old=None here is a live first sighting: treat it as "nowhere" so the
            # first arrival after a restart isn't dropped (audit F6).
            old = old or ""
            if old and not new:
                return await self._fire("geofence_depart", car_id, extra=old)
            if new:
                return await self._fire("geofence_arrive", car_id, extra=new)
            return None

        if old is None:
            return  # first value of this key since start: not a change

        if key == "speed":
            limit = _num(await store.get_setting("speed_limit_kmh"))
            spd = _num(new)
            # 不能要求 not user_present：is_user_present 的意思是"车里有人"，
            # 而借车/代客泊车时车里当然有人 —— 加这个条件正好把这条规则的
            # 唯一用途排除掉了。车在开就是有人在开，谁开都一样要提醒。
            if limit and limit > 0 and spd is not None and spd > limit:
                from ..i18n import prefs as _p

                mi = (await _p.get()).get("length") == "mi"
                f = 0.621371 if mi else 1.0
                unit = "mph" if mi else "km/h"
                return await self._fire("speed_limit", car_id,
                                        extra=f"{spd * f:.0f} {unit} (> {limit * f:.0f})")
            return None

        # 充电中持续记录峰值，供结束时判断交流还是直流
        if key in ("charger_power", "charger_voltage") and car.get("charging_state") == "Charging":
            pk_p, pk_v = self._charge_peak.get(car_id, (0.0, 0.0))
            self._charge_peak[car_id] = (
                max(pk_p, _num(car.get("charger_power")) or 0),
                max(pk_v, _num(car.get("charger_voltage")) or 0),
            )

        # 超充充满后继续占位会按分钟计费。只在直流快充时提 ——
        # 家里的交流慢充充满了没人收你钱。
        if key == "charging_state":
            if new == "Charging":
                self._charge_peak.pop(car_id, None)     # 新的一次充电，重新记
            elif new == "Complete":
                peak_kw, peak_v = self._charge_peak.pop(car_id, (0.0, 0.0))
                # 直流快充：功率高，或电压明显高于交流（家用 110-250V，
                # 超充 300-500V）。两个条件任一成立即可。
                if peak_kw >= 50 or peak_v >= 300:
                    await self._fire("idle_fee_risk", car_id)

        if key in _TPMS_KEYS and new == "true":
            return await self._fire("tpms_warning", car_id, extra=t(_TPMS_KEYS[key], loc0).lower())

        if key == "charging_state" and old == "Charging":
            if new == "NoPower" and _is_true(car.get("plugged_in")):
                return await self._fire("charging_interrupted", car_id)
            if new == "Stopped":
                level = _num(car.get("battery_level"))
                limit = _num(car.get("charge_limit_soc"))
                if level is not None and limit is not None and level < limit - 2:
                    old_task = self._stop_checks.get(car_id)
                    if old_task and not old_task.done():
                        old_task.cancel()
                    self._stop_checks[car_id] = asyncio.create_task(
                        self._judge_stop(car_id), name=f"charge-stop-{car_id}")
                return None

        if key == "charging_state" and new == "Complete":
            return await self._fire("charge_complete", car_id)

        if key == "battery_level":
            level = _num(new)
            prev = _num(old)
            if (
                level is not None and prev is not None
                and prev > 20 >= level
                and not _is_true(car.get("plugged_in"))
            ):
                return await self._fire("low_battery", car_id, extra=f"{level:.0f}%")

        if key == "update_available" and new == "true":
            version = car.get("update_version") or ""
            if not version:
                return None   # the version arrives in the same burst; an empty one is noise
            return await self._fire("update_available", car_id, extra=version)

        if key == "healthy" and new == "false":
            return await self._fire("teslamate_unhealthy", car_id)

    # ---------- 触发 ----------

    def _quiet_now(self) -> bool:
        spec = settings.alert_quiet_hours.strip()
        if not spec or "-" not in spec:
            return False
        try:
            start_s, end_s = spec.split("-")
            now = datetime.now(_tz()).time()
            start = datetime.strptime(start_s.strip(), "%H:%M").time()  # noqa: DTZ007 - 刻意解析无时区的本地时间（车机文件名 / HH:MM 时段），加 %z 反而是错的
            end = datetime.strptime(end_s.strip(), "%H:%M").time()  # noqa: DTZ007 - 刻意解析无时区的本地时间（车机文件名 / HH:MM 时段），加 %z 反而是错的
        except ValueError:
            return False
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end  # 跨午夜

    async def fire_custom(
        self, car_id: str, level: str, title: str, body: str,
        rule: str = "sentry_activity", gate: str | None = None, *,
        url: str | None = None, tag: str | None = None, image: str | None = None,
    ) -> bool:
        """Send an alert whose title/body the caller composed (sentry incidents,
        watchdog, maintenance, new dashcam clips).

        Returns True once the alert is *handled*: pushed to at least one
        channel, or recorded in-app when its mode is "app". False means it was
        switched off, suppressed (quiet hours / snooze) or every push failed —
        callers retry or keep state on False.

        `gate` separates "which rule it is filed under" from "which switch
        silences it": every sentry incident is filed as `sentry_activity`, but
        an Aware is governed by `sentry_aware` and an alarm by `sentry_panic`.
        """
        key = gate or rule
        mode = await self._mode(key) if key in RULES else "push"
        if mode == "off":
            return False
        push = mode == "push"
        if push and self._quiet_now() and level != "critical":
            push = False                     # quiet hours: keep it in the app
        if push and await self._snoozed(level):
            log.info("alert %s kept in-app by snooze", rule)
            push = False
        return await self._deliver(car_id, rule, level, title, body, push=push,
                                   url=url, tag=tag, image=image)

    async def _deliver(self, car_id: str, rule: str, level: str, title: str, body: str, *,
                       push: bool = True, url: str | None = None, tag: str | None = None,
                       image: str | None = None) -> bool:
        """Record the alert, and push it when asked.

        Push text is short by design: the title says what happened, the body
        says where and when, the tap opens the event. Everything else lives in
        the app."""
        from ..i18n import prefs as user_prefs

        up = await user_prefs.get()
        car = live.cars.get(car_id, {})
        car_name = await car_label(car_id)
        line = body.strip() if body else await self.where_when(car_id, up["locale"])
        delivered, channels = False, []
        if push:
            payload = {"rule": rule, "level": level, "car_id": car_id, "car_name": car_name, "tag": tag,
                       "urgent": rule in _URGENT_RULES}
            lat, lon = car.get("latitude"), car.get("longitude")
            if lat and lon:
                payload["map_url"] = (f"https://uri.amap.com/marker?position={lon},{lat}" if up.get("market") == "CN"
                                      else f"https://maps.google.com/?q={lat},{lon}")
            delivered, channels = await notifier.send(title, line, payload=payload, attach=image,
                                                      level=level, url=url)
        alert_id = None
        with contextlib.suppress(Exception):
            alert_id = await store.add_alert(car_id, car_name, rule, level, title, line, delivered,
                                             channels if push else ["app"])
        live.emit({"type": "alert", "id": alert_id, "ts": time.time(), "car_id": car_id,
                   "car_name": car_name, "rule": rule, "level": level, "title": title,
                   "body": line, "pushed": bool(delivered), "in_app_only": not push, "url": url})

        # Sentry events are re-published for Home Assistant / Node-RED. Under
        # our own prefix: publishing under teslamate/ made the hub hear its own
        # event as TeslaMate data and count it as a heartbeat (audit F3).
        if rule.startswith(("sentry_", "possible_theft", "new_sentry")):
            import json as _json

            live.publish(
                f"ohmytesla/cars/{car_id}/sentry_event",
                _json.dumps(
                    {"rule": rule, "level": level, "car": car_name, "car_id": car_id,
                     "title": title, "body": line, "geofence": car.get("geofence") or "",
                     "ts": time.time()},
                    ensure_ascii=False,
                ),
            )
        log.info("alert %s level=%s car=%s %s", rule, level, car_id,
                 f"pushed via {','.join(channels) or '-'} ok={delivered}" if push else "recorded in-app")
        return bool(delivered) if push else True

    async def where_when(self, car_id: str, loc: str) -> str:
        """'Home · 10:41 PM' — the one line a push body needs."""
        place = ""
        with contextlib.suppress(Exception):
            place, at_home = await sentry_tracker.place(live.cars.get(car_id, {}))
            place = place_label(place, at_home, loc)
        now = datetime.now(_tz())
        when = now.strftime("%H:%M") if loc == "zh" else now.strftime("%-I:%M %p")
        return " · ".join(x for x in (place, when) if x)

    async def _fire(
        self, rule_id: str, car_id: str, extra: str = "", attach: str | None = None,
        url: str | None = None,
    ) -> bool:
        rule = RULES.get(rule_id)
        if rule is None:
            return False
        mode = await self._mode(rule_id)
        if mode == "off":
            return False
        push = mode == "push"
        if push and self._quiet_now() and rule.level != "critical":
            push = False
        if push and await self._snoozed(rule.level):
            push = False

        now = time.time()
        cooldown = settings.alert_cooldown_seconds * rule.cooldown_factor
        last = self._last_fired.get((car_id, rule_id), 0)
        if now - last < cooldown:
            return False
        self._last_fired[(car_id, rule_id)] = now

        from ..i18n import prefs as user_prefs

        loc = (await user_prefs.get())["locale"]
        car_name = await car_label(car_id)
        # Short, specific titles: "⚠️ Cybertruck — low tire pressure · front left".
        key = f"push.{rule_id}"
        if key in _CATALOG_KEYS:
            title = t(key, loc, car=car_name, detail=extra or "").rstrip(" ·")
        else:
            title = t(f"rule.{rule_id}", loc) if f"rule.{rule_id}" in _CATALOG_KEYS else (
                rule.title_zh if loc == "zh" else rule.title_en)
        body = await self.where_when(car_id, loc)
        delivered = await self._deliver(car_id, rule_id, rule.level, title, body, push=push,
                                        url=url or "/security", image=attach)
        if push and not delivered:
            # 投递失败不占用冷却窗口，下一次状态变化可重试
            self._last_fired[(car_id, rule_id)] = last
        return delivered


engine = AlertEngine()
