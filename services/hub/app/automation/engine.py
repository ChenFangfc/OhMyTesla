"""自动化引擎：触发 → 条件 → 动作。

告警引擎回答"发生了什么，要不要通知我"；这里回答"发生了什么，要不要**做点什么**"。
两者共用同一条 MQTT transition 管道（livestate 的 transition hook），也共用
tesla/commands.py 那套指令下发，所以增量成本很小。

设计上的三条硬规矩，都是被真实故障逼出来的：

1. **动作必须幂等 + 有冷却。** MQTT 会重放 retained 消息，断线重连时一批
   transition 会连着到。没有冷却的话，"到家就开空调"会在重连的瞬间连发
   五次开空调——每一次都是一笔 API 费用，而且车会被反复唤醒。

2. **绝不在回放期间执行。** livestate 启动时会回放 retained 快照（old=None），
   那不是"刚刚发生"。回放期触发的规则会在每次重启时把昨天的状态当成新事件。

3. **默认全部停用，且高危动作要显式开。** 这个引擎能解锁车门。新建的规则
   一律 enabled=false，用户在界面上看过一遍才生效。
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from ..alerts.store import store
from ..livestate import live

log = logging.getLogger(__name__)

SETTINGS_KEY = "automations"

# 每条规则两次执行之间的最小间隔（秒）。可按规则覆盖。
DEFAULT_COOLDOWN_S = 300

# 允许自动化调用的指令，按指令表里的风险等级划分（tesla/catalog.py 是唯一来源）。
# 风险 0 的可以直接用；风险 ≥1 的（解锁、开前备箱/尾门、关哨兵、开车窗……）
# 要在 .env 里显式放开（AUTOMATION_ALLOW_UNLOCK=true）——一条写错的规则半夜
# 把车解锁，和其它任何 bug 都不是一个量级的后果。风险 2（远程启动）永远不行。
from ..tesla import catalog as _catalog  # noqa: E402

SAFE_COMMANDS = frozenset(
    [c.id for c in _catalog.COMMANDS.values() if c.risk == 0] + list(_catalog.ALIASES))
RISKY_COMMANDS = frozenset(c.id for c in _catalog.COMMANDS.values() if c.risk == 1)

# 支持的触发类型
TRIGGERS = {
    "state":        "车辆状态变化（online / asleep / driving / charging / offline）",
    "geofence":     "进入或离开某个地点",
    "battery":      "电量越过阈值",
    "plugged_in":   "插枪 / 拔枪",
    "sentry":       "哨兵开关变化",
    "climate":      "空调开关变化",
    "locked":       "上锁 / 解锁",
    "schedule":     "每天固定时刻",
}


def _now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


class AutomationEngine:
    def __init__(self) -> None:
        self._last_run: dict[str, float] = {}
        self._sched_task: asyncio.Task | None = None
        self._sched_fired: dict[str, str] = {}   # rule_id -> 已触发的日期

    # ---------------- 规则存取 ----------------

    async def rules(self) -> list[dict]:
        return await store.get_setting(SETTINGS_KEY, []) or []

    async def save_rules(self, rules: list[dict]) -> list[dict]:
        validate(rules)
        await store.set_setting(SETTINGS_KEY, rules)
        log.info("automation rules updated (%d rules, %d enabled)",
                 len(rules), sum(1 for r in rules if r.get("enabled")))
        return rules

    async def upsert(self, rule: dict) -> list[dict]:
        def apply(rules):
            rules = list(rules or [])
            rid = rule.get("id")
            out = [r for r in rules if r.get("id") != rid]
            out.append(rule)
            validate(out)
            return out

        return await store.mutate_setting(SETTINGS_KEY, apply, [])

    async def delete(self, rule_id: str) -> list[dict]:
        return await store.mutate_setting(
            SETTINGS_KEY, lambda rs: [r for r in (rs or []) if r.get("id") != rule_id], [])

    # ---------------- 触发判定 ----------------

    def _matches(self, rule: dict, car_id: str, key: str, old, new) -> bool:
        trig = rule.get("trigger") or {}
        kind = trig.get("type")

        if trig.get("car_id") and str(trig["car_id"]) != str(car_id):
            return False

        if kind == "state":
            return key == "state" and new == trig.get("to") and old != new

        if kind == "geofence":
            if key != "geofence":
                return False
            place = trig.get("place")
            if trig.get("direction") == "leave":
                return old == place and new != place
            return new == place and old != place

        if kind == "battery":
            if key not in ("battery_level", "usable_battery_level"):
                return False
            try:
                o, n, th = float(old or -1), float(new), float(trig.get("threshold"))
            except (TypeError, ValueError):
                return False
            # 只在**跨过**阈值的那一刻触发，不是"低于就一直触发"
            return (o > th >= n) if trig.get("direction") == "below" else (o < th <= n)

        if kind == "plugged_in":
            return key == "plugged_in" and str(new).lower() == str(trig.get("to", "true")).lower() \
                   and old != new

        if kind in ("sentry", "climate", "locked"):
            field = {"sentry": "sentry_mode", "climate": "is_climate_on", "locked": "locked"}[kind]
            return key == field and str(new).lower() == str(trig.get("to", "true")).lower() \
                   and old != new

        return False

    def _conditions_ok(self, rule: dict, car_id: str) -> tuple[bool, str]:
        """附加条件。返回 (是否通过, 不通过的原因)。"""
        cond = rule.get("conditions") or {}
        car = live.cars.get(car_id, {})

        if cond.get("only_at") and (car.get("geofence") or "") != cond["only_at"]:
            return False, f"not at {cond['only_at']}"
        if cond.get("not_at") and (car.get("geofence") or "") == cond["not_at"]:
            return False, f"at {cond['not_at']}"

        window = cond.get("time_window")           # "22:00-06:00"
        if window and "-" in window:
            try:
                a, b = window.split("-", 1)
                now = _now_local().strftime("%H:%M")
                inside = (a <= now < b) if a <= b else (now >= a or now < b)
                if not inside:
                    return False, f"outside {window}"
            except ValueError:
                log.warning("automation %s: bad time_window %r, ignoring",
                            rule.get("id"), window)

        if cond.get("min_battery"):
            try:
                if float(car.get("battery_level") or 0) < float(cond["min_battery"]):
                    return False, "battery below min_battery"
            except (TypeError, ValueError):
                log.warning("automation %s: bad min_battery %r, ignoring",
                            rule.get("id"), cond.get("min_battery"))
        return True, ""

    # ---------------- 执行 ----------------

    async def _run_actions(self, rule: dict, car_id: str, why: str) -> None:
        from ..config import settings
        from ..tesla import commands as cmd

        allow_risky = bool(getattr(settings, "automation_allow_unlock", False))
        for action in rule.get("actions") or []:
            kind = action.get("type")
            if kind == "notify":
                from ..alerts.engine import engine as alert_engine

                await alert_engine.fire_custom(
                    car_id, action.get("level") or "info",
                    action.get("title") or rule.get("name") or "Automation",
                    action.get("body") or why,
                    rule="automation", gate="automation",
                )
                continue

            if kind != "command":
                log.warning("automation %s: unknown action type %r", rule.get("id"), kind)
                continue

            name = action.get("command")
            if name in RISKY_COMMANDS and not allow_risky:
                log.error("automation %s: %s is not allowed (set AUTOMATION_ALLOW_UNLOCK=true)",
                          rule.get("id"), name)
                continue
            if name not in SAFE_COMMANDS and name not in RISKY_COMMANDS:
                log.error("automation %s: %s is not an allowed command", rule.get("id"), name)
                continue

            t0 = time.time()
            try:
                res = await cmd.send(int(car_id), name, action.get("params") or {})
                log.info("automation %s ran %s -> %s", rule.get("id"), name, res.get("ok"))
                # Logged on success too: Sentry reads this log to tell the
                # owner's own actions (an automation opening the frunk) from
                # someone at the car, and it's the audit trail.
                await store.log_command(car_id, name, action.get("params"),
                                        f"automation:{rule.get('id')}", bool(res.get("ok", True)), "",
                                        int((time.time() - t0) * 1000))
            except Exception as e:
                log.exception("automation %s: command %s failed", rule.get("id"), name)
                code = getattr(e, "code", "")
                await store.log_command(car_id, name, action.get("params"),
                                        f"automation:{rule.get('id')}", False,
                                        f"{code}: {e}" if code else str(e),
                                        int((time.time() - t0) * 1000))

    async def _maybe_run(self, rule: dict, car_id: str, why: str) -> None:
        rid = str(rule.get("id"))
        cool = float(rule.get("cooldown_s") or DEFAULT_COOLDOWN_S)
        now = time.time()
        # 冷却按 (规则, 车) 分别计。MQTT 重连时一批 transition 会连着到，
        # 没有这道闸门"到家开空调"会在几秒内连发好几次。
        gate_key = f"{rid}:{car_id}"
        if now - self._last_run.get(gate_key, 0) < cool:
            log.debug("automation %s skipped: cooling down", rid)
            return
        ok, reason = self._conditions_ok(rule, car_id)
        if not ok:
            log.debug("automation %s skipped: %s", rid, reason)
            return
        self._last_run[gate_key] = now
        log.info("automation %s triggered (%s)", rid, why)
        await self._run_actions(rule, car_id, why)
        await self._record(rid, car_id, why)

    async def _record(self, rule_id: str, car_id: str, why: str) -> None:
        def apply(hist):
            hist = list(hist or [])
            hist.append({"ts": time.time(), "rule": rule_id, "car_id": car_id, "why": why})
            return hist[-200:]          # 只留最近 200 条，这是给人看的不是账本

        await store.mutate_setting("automation_log", apply, [])

    async def history(self) -> list[dict]:
        return list(reversed(await store.get_setting("automation_log", []) or []))

    # ---------------- 接入 transition 管道 ----------------

    async def handle(self, car_id: str, key: str, old, new) -> None:
        # 回放期（old is None）绝不执行。启动时 livestate 会把 retained 快照
        # 灌一遍，那是"最后已知状态"而不是"刚刚发生"—— 在这里执行动作意味着
        # 每次重启都会把昨天的事重做一遍。
        if old is None:
            return
        try:
            for rule in await self.rules():
                if not rule.get("enabled"):
                    continue
                if self._matches(rule, car_id, key, old, new):
                    await self._maybe_run(rule, car_id, f"{key}: {old} → {new}")
        except Exception:
            log.exception("automation dispatch failed")

    # ---------------- 定时触发 ----------------

    def start(self) -> None:
        self._sched_task = asyncio.create_task(self._sched_loop(), name="automation-schedule")

    async def stop(self) -> None:
        if self._sched_task:
            self._sched_task.cancel()
            import contextlib

            with contextlib.suppress(asyncio.CancelledError):
                await self._sched_task

    async def _sched_loop(self) -> None:
        await asyncio.sleep(45)
        while True:
            try:
                await self._check_schedules()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("automation schedule check failed")
            await asyncio.sleep(30)

    async def _check_schedules(self) -> None:
        now = _now_local()
        hhmm = now.strftime("%H:%M")
        today = now.date().isoformat()
        for rule in await self.rules():
            if not rule.get("enabled"):
                continue
            trig = rule.get("trigger") or {}
            if trig.get("type") != "schedule" or trig.get("at") != hhmm:
                continue
            # 每天只放一次。循环每 30 秒跑一次，同一分钟内会命中两遍。
            rid = str(rule.get("id"))
            if self._sched_fired.get(rid) == today:
                continue
            self._sched_fired[rid] = today
            car_id = str(trig.get("car_id") or next((k for k in live.cars if k.isdigit()), "1"))
            await self._maybe_run(rule, car_id, f"schedule {hhmm}")


def validate(rules) -> None:
    """保存前校验。错误直接抛 ValueError，由路由转成 400。"""
    # 这里一律抛 ValueError（而不是 ruff 建议的 TypeError）：路由层用
    # `except ValueError` 统一转成 400，换成 TypeError 就变成 500 了。
    if not isinstance(rules, list):
        raise ValueError("automations must be a list")  # noqa: TRY004
    seen = set()
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise ValueError(f"rule[{i}] must be an object")  # noqa: TRY004
        rid = r.get("id")
        if not rid or not isinstance(rid, str):
            raise ValueError(f"rule[{i}]: id is required")
        if rid in seen:
            raise ValueError(f"duplicate rule id {rid!r}")
        seen.add(rid)

        trig = r.get("trigger") or {}
        if trig.get("type") not in TRIGGERS:
            raise ValueError(f"{rid}: trigger.type must be one of {sorted(TRIGGERS)}")
        if trig["type"] == "schedule":
            at = trig.get("at") or ""
            try:
                datetime.strptime(at, "%H:%M")  # noqa: DTZ007 - 只解析时刻，本就无时区
            except ValueError:
                raise ValueError(f"{rid}: schedule trigger needs at=HH:MM") from None
        if trig["type"] == "geofence" and not trig.get("place"):
            raise ValueError(f"{rid}: geofence trigger needs a place")
        if trig["type"] == "battery" and trig.get("threshold") is None:
            raise ValueError(f"{rid}: battery trigger needs a threshold")

        actions = r.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError(f"{rid}: needs at least one action")
        for a in actions:
            if not isinstance(a, dict) or a.get("type") not in ("command", "notify"):
                raise ValueError(f"{rid}: action.type must be command or notify")
            if a["type"] == "command":
                name = a.get("command")
                if name not in SAFE_COMMANDS and name not in RISKY_COMMANDS:
                    raise ValueError(f"{rid}: unknown or disallowed command {name!r}")

        cd = r.get("cooldown_s")
        if cd is not None:
            try:
                bad_cd = float(cd) < 0
            except (TypeError, ValueError):
                bad_cd = True
            if bad_cd:
                raise ValueError(f"{rid}: cooldown_s must be a non-negative number")


engine = AutomationEngine()
