"""自动化引擎的触发判定与安全闸门。

这个引擎能给车下指令，所以两类错误的代价完全不对称：
- 该触发没触发 → 用户抱怨"自动化不好使"
- 不该触发却触发了 → 半夜给车解锁、反复唤醒烧 API 费用

下面的用例大部分在测**第二类**。

跑法：docker exec ohmytesla-ohmytesla-1 python3 /opt/ohmytesla/tests/test_automation.py
"""

import asyncio
import os
import sys

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break

from app.automation import engine as A  # noqa: E402
from app.livestate import live  # noqa: E402

FAILS: list[str] = []


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def rule(**kw):
    base = {"id": "r1", "enabled": True, "trigger": {}, "actions": [{"type": "notify"}]}
    base.update(kw)
    return base


def main() -> int:
    eng = A.AutomationEngine()
    live.cars["1"] = {"geofence": "Home", "battery_level": "55", "state": "online"}

    # ── 触发匹配 ─────────────────────────────────────────────────
    r = rule(trigger={"type": "state", "to": "driving"})
    chk("状态变化匹配", eng._matches(r, "1", "state", "online", "driving"))
    chk("状态未变化不匹配", not eng._matches(r, "1", "state", "driving", "driving"))
    chk("别的键不匹配", not eng._matches(r, "1", "battery_level", "50", "driving"))

    r = rule(trigger={"type": "geofence", "place": "Home", "direction": "enter"})
    chk("进入地点匹配", eng._matches(r, "1", "geofence", "Work", "Home"))
    chk("已在该地点不重复匹配", not eng._matches(r, "1", "geofence", "Home", "Home"))
    r = rule(trigger={"type": "geofence", "place": "Home", "direction": "leave"})
    chk("离开地点匹配", eng._matches(r, "1", "geofence", "Home", "Work"))
    chk("从别处到别处不匹配", not eng._matches(r, "1", "geofence", "Work", "Store"))

    # 电量阈值：只在**跨过**的那一刻触发，不是"低于就一直触发"。
    # 这一条没有的话，电量停在 19% 期间每一次上报都会再执行一遍动作。
    r = rule(trigger={"type": "battery", "threshold": 20, "direction": "below"})
    chk("电量跨过阈值向下", eng._matches(r, "1", "battery_level", "21", "19"))
    chk("已在阈值下不重复", not eng._matches(r, "1", "battery_level", "19", "18"))
    chk("向上穿越不触发 below", not eng._matches(r, "1", "battery_level", "19", "21"))
    r = rule(trigger={"type": "battery", "threshold": 80, "direction": "above"})
    chk("电量跨过阈值向上", eng._matches(r, "1", "battery_level", "79", "81"))

    r = rule(trigger={"type": "plugged_in", "to": "true"})
    chk("插枪匹配", eng._matches(r, "1", "plugged_in", "false", "true"))
    chk("拔枪不匹配 to=true", not eng._matches(r, "1", "plugged_in", "true", "false"))

    # 限定车辆
    r = rule(trigger={"type": "state", "to": "online", "car_id": 2})
    chk("限定别的车时不匹配", not eng._matches(r, "1", "state", "asleep", "online"))

    # ── 条件 ─────────────────────────────────────────────────────
    ok, _ = eng._conditions_ok(rule(conditions={"only_at": "Home"}), "1")
    chk("only_at 命中", ok)
    ok, why = eng._conditions_ok(rule(conditions={"only_at": "Work"}), "1")
    chk("only_at 不命中被挡", not ok, why)
    ok, why = eng._conditions_ok(rule(conditions={"not_at": "Home"}), "1")
    chk("not_at 命中被挡", not ok, why)
    ok, why = eng._conditions_ok(rule(conditions={"min_battery": 80}), "1")
    chk("电量不足被挡", not ok, why)
    ok, _ = eng._conditions_ok(rule(conditions={"min_battery": 20}), "1")
    chk("电量充足放行", ok)
    # 跨午夜的时间窗
    ok_a, _ = eng._conditions_ok(rule(conditions={"time_window": "00:00-23:59"}), "1")
    chk("全天窗口放行", ok_a)

    # ── 回放期绝不执行 ───────────────────────────────────────────
    ran = []
    eng._maybe_run = lambda *a, **k: ran.append(a) or asyncio.sleep(0)  # type: ignore[assignment]

    async def replay():
        # old=None 就是 livestate 灌 retained 快照的形状
        await eng.handle("1", "state", None, "driving")
    asyncio.run(replay())
    chk("retained 回放不执行动作", ran == [], ran)

    # ── 指令白名单 ───────────────────────────────────────────────
    chk("door_unlock 在高危集合里", "door_unlock" in A.RISKY_COMMANDS)
    chk("door_unlock 不在安全集合里", "door_unlock" not in A.SAFE_COMMANDS)
    chk("climate_on 是安全指令", "climate_on" in A.SAFE_COMMANDS)

    # ── 校验 ─────────────────────────────────────────────────────
    def bad(rules, why):
        try:
            A.validate(rules)
            chk(f"应当拒绝：{why}", False)
        except ValueError:
            chk(f"拒绝：{why}", True)

    bad([{"id": "a", "trigger": {"type": "nope"}, "actions": [{"type": "notify"}]}], "未知触发类型")
    bad([{"id": "a", "trigger": {"type": "state"}, "actions": []}], "没有动作")
    bad([{"id": "a", "trigger": {"type": "geofence"}, "actions": [{"type": "notify"}]}], "围栏触发缺 place")
    bad([{"id": "a", "trigger": {"type": "battery"}, "actions": [{"type": "notify"}]}], "电量触发缺阈值")
    bad([{"id": "a", "trigger": {"type": "state"}, "actions": [{"type": "command", "command": "sudo"}]}],
        "未知指令")
    bad([{"id": "a", "trigger": {"type": "state"}, "actions": [{"type": "notify"}]},
         {"id": "a", "trigger": {"type": "state"}, "actions": [{"type": "notify"}]}], "重复 id")
    bad([{"id": "a", "trigger": {"type": "state"}, "actions": [{"type": "notify"}], "cooldown_s": -1}],
        "负冷却时间")

    A.validate([{"id": "ok", "trigger": {"type": "schedule", "at": "07:30"},
                 "actions": [{"type": "command", "command": "climate_on"}]}])
    chk("合法规则通过校验", True)

    print()
    print("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
