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


RULES: dict[str, Rule] = {
    r.id: r
    for r in [
        Rule("sentry_door_opened", "critical", "🚨 哨兵警报：车门被打开", "🚨 Sentry: door opened"),
        Rule("sentry_trunk_opened", "critical", "🚨 哨兵警报：后备箱被打开", "🚨 Sentry: trunk opened"),
        Rule("sentry_frunk_opened", "critical", "🚨 哨兵警报：前备箱被打开", "🚨 Sentry: frunk opened"),
        Rule("sentry_window_opened", "critical", "🚨 哨兵警报：车窗被打开", "🚨 Sentry: window opened"),
        Rule("sentry_unlocked", "critical", "🚨 哨兵警报：车辆被解锁", "🚨 Sentry: vehicle unlocked"),
        Rule("possible_theft", "critical", "🚨 疑似盗窃：无人在车但车辆开始移动", "🚨 Possible theft: vehicle moving with no user present"),
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
        Rule("charging_interrupted", "warning", "⚠️ 充电意外中断", "⚠️ Charging interrupted"),
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

# 规则标题优先用字典（原生英文），字典没有的退回内置双语
from ..i18n.router import catalog as _catalog  # noqa: E402

_CATALOG_KEYS = set(_catalog("en"))

_BODY_LABELS = {
    "zh": {"vehicle": "车辆", "time": "时间", "location": "位置", "battery": "电量"},
    "en": {"vehicle": "Vehicle", "time": "Time", "location": "Location", "battery": "Battery"},
}

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

    def __init__(self) -> None:
        self._last_fired: dict[tuple[str, str], float] = {}
        self._enabled_cache: dict[str, bool] | None = None
        self._rule_lock = asyncio.Lock()
        # 本次充电过程中见过的最大功率/电压，用来判断是不是直流快充。
        # 不能在 charging_state=Complete 那一刻现取：那时功率已经归零。
        # 也不能用 fast_charger_present —— 它是 charges 表的列，**不在 MQTT 上**
        # （实测 teslamate/cars/1/# 只有 charger_power / voltage / phases 等）。
        self._charge_peak: dict[str, tuple[float, float]] = {}

    async def rule_states(self) -> dict[str, bool]:
        saved = await store.get_setting("alert_rules", {}) or {}
        return {
            rid: bool(saved.get(rid, rule.default_enabled))
            for rid, rule in RULES.items()
        }

    async def set_rule(self, rule_id: str, enabled: bool) -> None:
        async with self._rule_lock:
            saved = await store.get_setting("alert_rules", {}) or {}
            saved[rule_id] = enabled
            await store.set_setting("alert_rules", saved)
            self._enabled_cache = None

    async def _enabled(self, rule_id: str) -> bool:
        if self._enabled_cache is None:
            self._enabled_cache = await self.rule_states()
        return self._enabled_cache.get(rule_id, True)

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

        # ---- Fleet Telemetry 实时流（真实哨兵状态边沿）----
        # 不是 retained 回放，首条消息也要处理：第一条就是 Panic 时绝不能漏报
        if key == "fleet_sentry_state":
            sentry_tracker.note_fleet(car_id)
            if new in ("Aware", "Panic") and old != new:
                await sentry_tracker.observe(
                    car_id, car.get("display_name") or f"Car {car_id}",
                    source="fleet", state="panic" if new == "Panic" else "aware",
                    geofence=car.get("geofence") or "", soc=car.get("battery_level") or "",
                )
            return

        # ---- Fleet Telemetry 的 DisplayState（与上面 center_display_state 不同编号）----
        # Lock = 有人触碰了锁着的车。哨兵**没开**时这是唯一能察觉"有人在动我的车"
        # 的信号（SentryGuard 走的也是这条）。与 center_display_state 一样不是
        # retained 回放，首条也要处理。
        if key == "fleet_display_state":
            if new == "Lock" and not _is_true(car.get("is_user_present")):
                await sentry_tracker.observe(
                    car_id, car.get("display_name") or f"Car {car_id}",
                    source="fleet", state="aware",
                    geofence=car.get("geofence") or "", soc=car.get("battery_level") or "",
                    extra="body.touched_while_locked",
                )
            return

        if old is None:
            return  # 启动时 retained 消息回放，不算变化

        # ---- 轮询层：center_display_state = 7 表示哨兵已被触发、正在录像 ----
        # （不是"哨兵已开启"——开启但无事时屏幕是灭的，值为 0）
        # TeslaMate 停车+哨兵时固定 30 秒轮询一次，所以这一层可能漏掉短事件。
        #
        # ⚠️ 这个 7 只对 **Owner API / TeslaMate MQTT** 的编号成立：
        #     0 Off · 2 待机 · 3 充电 · 4 On · 5 大充电屏 · 6 待解锁 · 7 哨兵 · 8 狗狗 · 9 媒体
        # Fleet Telemetry 的 DisplayState 是**另一套编号**：
        #     1 Off · 2 Dim · 3 Accessory · 4 On · 5 Driving · 6 Charging · 7 **Lock** · 8 Sentry · 9 Dog · 10 Entertainment
        # 两套差一位。把遥测流的值喂进这里，7 会被当成哨兵，实际是"有人碰了锁着的车"——
        # 一个看起来能跑、但每次都报错事的 bug。遥测流请走 fleet_display_state 那条路。
        if key == "center_display_state":
            if (
                new == "7"
                and settings.sentry_polled_alerts
                and not sentry_tracker.has_fleet(car_id)  # 有实时流就不用这个糙的
                and not _is_true(car.get("is_user_present"))
            ):
                await sentry_tracker.observe(
                    car_id, car.get("display_name") or f"Car {car_id}",
                    source="polled", state="aware",
                    geofence=car.get("geofence") or "", soc=car.get("battery_level") or "",
                )
            return

        sentry_on = _is_true(car.get("sentry_mode"))
        user_present = _is_true(car.get("is_user_present"))
        guarding = sentry_on and not user_present

        if key == "sentry_mode":
            await self._fire("sentry_armed" if new == "true" else "sentry_disarmed", car_id)
            return

        if guarding:
            sentry_rules = {
                "doors_open": ("sentry_door_opened", "true"),
                "trunk_open": ("sentry_trunk_opened", "true"),
                "frunk_open": ("sentry_frunk_opened", "true"),
                "windows_open": ("sentry_window_opened", "true"),
                "locked": ("sentry_unlocked", "false"),
            }
            if key in sentry_rules and new == sentry_rules[key][1]:
                rule_id = sentry_rules[key][0]
                # 同时并入哨兵会话，让事件流里看到"有人靠近 → 开门"的完整过程
                rule = RULES.get(rule_id)
                if rule:
                    await sentry_tracker.note_intrusion(
                        car_id, car.get("display_name") or f"Car {car_id}",
                        f"rule.{rule_id}", car.get("geofence") or "",
                        car.get("battery_level") or "",
                    )
                return self._schedule_confirmed(rule_id, car_id, key, new)

        if key == "state" and new == "driving" and not user_present and sentry_on:
            return self._schedule_confirmed("possible_theft", car_id, key, new)

        if key == "geofence":
            # 空字符串表示"不在任何已知地点"。只在两端有一端非空时才有意义，
            # 一次移动会先离开旧地点再到达新地点，两条都发得出去。
            if old and not new:
                return await self._fire("geofence_depart", car_id,
                                        extra=t("body.geofence_left", loc0, place=old))
            if new:
                return await self._fire("geofence_arrive", car_id,
                                        extra=t("body.geofence_at", loc0, place=new))
            return None

        if key == "speed":
            limit = _num(await store.get_setting("speed_limit_kmh"))
            spd = _num(new)
            # 不能要求 not user_present：is_user_present 的意思是"车里有人"，
            # 而借车/代客泊车时车里当然有人 —— 加这个条件正好把这条规则的
            # 唯一用途排除掉了。车在开就是有人在开，谁开都一样要提醒。
            if limit and limit > 0 and spd is not None and spd > limit:
                return await self._fire(
                    "speed_limit", car_id,
                    extra=t("body.speed_over", loc0, speed=f"{spd:.0f}", limit=f"{limit:.0f}"))
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
                    await self._fire("idle_fee_risk", car_id,
                                     extra=t("body.idle_fee", loc0))

        if key in _TPMS_KEYS and new == "true":
            return await self._fire("tpms_warning", car_id, extra=t("body.tpms_position", loc0, corner=t(_TPMS_KEYS[key], loc0)))

        if key == "charging_state" and old == "Charging":
            if new == "NoPower":
                return await self._fire("charging_interrupted", car_id, extra=t("body.charging_no_power", loc0))
            if new == "Stopped":
                level = _num(car.get("battery_level"))
                limit = _num(car.get("charge_limit_soc"))
                if level is not None and limit is not None and level < limit - 2:
                    return await self._fire(
                        "charging_interrupted", car_id,
                        extra=t("body.charging_stopped_early", loc0,
                                level=f"{level:.0f}", limit=f"{limit:.0f}"),
                    )

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
                return await self._fire("low_battery", car_id, extra=t("body.low_battery_level", loc0, level=f"{level:.0f}"))

        if key == "update_available" and new == "true":
            version = car.get("update_version") or ""
            return await self._fire("update_available", car_id, extra=t("body.update_version", loc0, version=version))

        if key == "healthy" and new == "false":
            return await self._fire("teslamate_unhealthy", car_id)

    # ---------- 触发 ----------

    def _schedule_confirmed(self, rule_id: str, car_id: str, key: str, expected: str) -> None:
        """延迟复查后触发（消除同批 MQTT 消息乱序导致的误报）。"""

        async def confirm():
            await asyncio.sleep(self.CONFIRM_DELAY_S)
            car = live.cars.get(car_id, {})
            if not (_is_true(car.get("sentry_mode")) and not _is_true(car.get("is_user_present"))):
                return  # 车主已解除哨兵/已在车内，视为正常操作
            if car.get(key) != expected:
                return  # 状态已恢复（如门又关上且哨兵未响）
            await self._fire(rule_id, car_id)

        asyncio.get_running_loop().create_task(confirm())

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

    def _location_lines(self, car: dict[str, str], loc: str = "zh",
                        up: dict | None = None) -> list[str]:
        """位置行。地图链接按**地区**选而不是按语言——
        在北美的中文车主要的是 Google 地图，高德在那边没用。"""
        LBL = _BODY_LABELS.get(loc, _BODY_LABELS["zh"])
        lines = []
        geofence = car.get("geofence")
        if geofence:
            lines.append(f"{LBL['location']}: {geofence}")
        lat, lon = car.get("latitude"), car.get("longitude")
        if lat and lon:
            in_china = (up or {}).get("market") == "CN"
            if in_china:
                lines.append(t("body.map_amap", loc) + f": https://uri.amap.com/marker?position={lon},{lat}")
            else:
                lines.append(t("body.map_google", loc) + f": https://maps.google.com/?q={lat},{lon}")
        return lines

    async def fire_custom(
        self, car_id: str, level: str, title: str, body: str,
        rule: str = "sentry_activity", gate: str | None = None,
    ) -> bool:
        """哨兵会话专用发送通道：标题/正文由会话模型给出，不走规则表冷却。

        冷却由会话去重承担（15 秒窗口 + 升级阶梯），这里只做静默时段判断。

        但**开关还是要认**：`sentry_aware` / `sentry_panic` / `data_stale` /
        `data_resumed` 四条规则在界面上有开关（走 /api/alerts/rules），
        以前这条路径完全不查 `_enabled()`，于是关掉它们等于没关。

        `gate` 用来把"存哪条 rule"和"查哪个开关"分开：哨兵会话统一以
        `sentry_activity` 入库（历史与界面分组依赖它），但要按当次的严重程度
        分别去查 sentry_aware / sentry_panic 的开关 —— 用户想关掉聒噪的
        "有人靠近"而保留"警报拉响"，这是个合理且常见的诉求。
        """
        if (gate or rule) in RULES and not await self._enabled(gate or rule):
            return False
        if self._quiet_now() and level != "critical":
            return False
        if await self._snoozed(level):
            log.info("alert %s suppressed by snooze", rule)
            return False
        from ..i18n import prefs as user_prefs

        up = await user_prefs.get()
        loc = up["locale"]
        LBL = _BODY_LABELS[loc]
        car = live.cars.get(car_id, {})
        car_name = car.get("display_name") or f"Car {car_id}"
        tz = _tz()
        lines = [
            f"{LBL['vehicle']}: {car_name}",
            f"{LBL['time']}: {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}",
            body,
        ]
        lines += self._location_lines(car, loc, up)
        if car.get("battery_level"):
            lines.append(f"{LBL['battery']}: {car['battery_level']}%")
        full = "\n".join(x for x in lines if x)

        delivered, channels = await notifier.send(
            title, full,
            payload={"rule": rule, "level": level, "car_id": car_id, "car_name": car_name},
        )
        with contextlib.suppress(Exception):
            await store.add_alert(car_id, car_name, rule, level, title, full, delivered, channels)

        # 只有哨兵类事件才回发到 sentry_event 主题。fire_custom 现在也被
        # 看门狗和保养提醒复用，无条件回发的话，HA 那边一条
        # 「sentry_event → 开车库灯 + 响蜂鸣器」的自动化，会被"雨刮该换了"
        # 触发一次。与 _fire 里的判断保持一致。
        if rule.startswith(("sentry_", "possible_theft", "new_sentry")):
            import json as _json

            # 载荷里要带 geofence 和 body：自动化最常见的条件就是
            # "只在家里/不在家时才动作"，而正文里是"哪扇门被开了"这类细节。
            # 少这两项的话，订阅方只能拿到一句标题，做不了任何判断。
            live.publish(
                f"teslamate/cars/{car_id}/sentry_event",
                _json.dumps(
                    {"rule": rule, "level": level, "car": car_name, "car_id": car_id,
                     "title": title, "body": full,
                     "geofence": (live.cars.get(car_id, {}) or {}).get("geofence") or "",
                     "ts": time.time()},
                    ensure_ascii=False,
                ),
            )
        log.info("sentry alert fired: %s level=%s delivered=%s", rule, level, delivered)
        return True

    async def _fire(
        self, rule_id: str, car_id: str, extra: str = "", attach: str | None = None
    ) -> None:
        rule = RULES.get(rule_id)
        if rule is None or not await self._enabled(rule_id):
            return

        if self._quiet_now() and rule.level != "critical":
            return
        if await self._snoozed(rule.level):
            log.info("alert %s suppressed by snooze", rule_id)
            return

        now = time.time()
        cooldown = settings.alert_cooldown_seconds * rule.cooldown_factor
        last = self._last_fired.get((car_id, rule_id), 0)
        if now - last < cooldown:
            return
        self._last_fired[(car_id, rule_id)] = now

        car = live.cars.get(car_id, {})
        car_name = car.get("display_name") or f"Car {car_id}"
        from ..i18n import prefs as user_prefs

        up = await user_prefs.get()
        loc = up["locale"]
        title = t(f"rule.{rule_id}", loc) if f"rule.{rule_id}" in _CATALOG_KEYS else (
            rule.title_zh if loc == "zh" else rule.title_en)

        tz = _tz()
        LBL = _BODY_LABELS[loc]
        body_lines = [
            f"{LBL['vehicle']}: {car_name}",
            f"{LBL['time']}: {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if extra:
            body_lines.append(extra)
        body_lines += self._location_lines(car, loc, up)
        battery = car.get("battery_level")
        if battery:
            body_lines.append(f"{LBL['battery']}: {battery}%")
        body = "\n".join(body_lines)

        delivered, channels = await notifier.send(
            title, body,
            payload={"rule": rule_id, "level": rule.level, "car_id": car_id, "car_name": car_name},
            attach=attach,
        )
        if not delivered:
            # 投递失败不占用冷却窗口，下一次状态变化可重试
            self._last_fired[(car_id, rule_id)] = last

        # 哨兵类事件回发 MQTT，供 HomeAssistant / Node-RED 联动
        if rule_id.startswith(("sentry_", "possible_theft", "new_sentry")):
            import json

            live.publish(
                f"teslamate/cars/{car_id}/sentry_event",
                json.dumps(
                    {"rule": rule_id, "level": rule.level, "car": car_name, "car_id": car_id,
                     "title": title, "body": body,
                     "geofence": (live.cars.get(car_id, {}) or {}).get("geofence") or "",
                     "ts": now},
                    ensure_ascii=False,
                ),
            )
        try:
            await store.add_alert(car_id, car_name, rule_id, rule.level, title, body, delivered, channels)
        except Exception:
            log.exception("failed to persist alert")
        log.info("alert fired: %s car=%s delivered=%s", rule_id, car_id, delivered)


engine = AlertEngine()
