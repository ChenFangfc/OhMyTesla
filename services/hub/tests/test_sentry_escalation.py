"""哨兵会话升级逻辑的回归测试。

这个文件的存在理由是一条 critical 缺陷：会话一旦因「活动持续 90 秒」升级过，
随后车辆真的进入 Panic（喇叭鸣响）时，`not sess.escalated` 会把推送整个挡掉。
也就是说，**整套告警系统最严重的那一级事件，恰好在最典型的真实入侵形态下丢失**
——因为真入侵总是先有持续活动，再有警报拉响。

下面第一个用例就是当时的复现路径。它必须一直是绿的。

跑法：docker exec ohmytesla-ohmytesla-1 python3 /opt/ohmytesla/tests/test_sentry_escalation.py
"""

import asyncio
import os
import sys

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break

from app.alerts import sentry as S  # noqa: E402
from app.config import settings  # noqa: E402

FIRED: list[tuple[str, str]] = []


async def _fake_fire(car_id, level, title, body, rule=None, gate=None):
    """替身推送通道：记下 (级别, 闸门)，永远算投递成功。"""
    FIRED.append((level, gate))
    return True


async def _no_policy(_self, _geofence):
    return S.DEFAULT_POLICY


async def _no_persist(_self, _sess, closed):
    return 1


async def _prefs():
    return {"locale": "en"}


def _install_stubs(tracker):
    tracker.bind(_fake_fire)
    # 这三样都要打数据库/偏好；测的是状态机，不是它们
    S.SentryTracker._policy_for = _no_policy
    S.SentryTracker._persist = _no_persist
    S.get_prefs = _prefs


async def scenario(states, escalate_seconds=90):
    """按顺序喂入若干 state，返回推送出去的 (级别, 闸门) 列表。

    states 里的每一项是 (state, 距会话开始的秒数)。时间靠直接改 last_seen
    伪造，避免测试真的 sleep 90 秒。
    """
    FIRED.clear()
    old = settings.sentry_escalate_seconds
    settings.sentry_escalate_seconds = escalate_seconds
    try:
        tracker = S.SentryTracker()
        _install_stubs(tracker)
        for i, (state, at) in enumerate(states):
            if i > 0:
                sess = tracker._sessions["1"]
                # observe() 会把 last_seen 设成 now，所以把 started 往前挪，
                # 等效于"这个会话已经持续了 at 秒"
                sess.started = sess.last_seen - at
            await tracker.observe("1", "Car", "fleet", state=state)
        return list(FIRED)
    finally:
        settings.sentry_escalate_seconds = old


async def main():
    fails = []

    def chk(name, cond, extra=""):
        print(f"  {'✅' if cond else '❌'} {name}{(' — ' + extra) if extra else ''}")
        if not cond:
            fails.append(name)

    # ── 回归用例：持续活动升级之后，真正的 Panic 仍然必须推 ──────────────
    got = await scenario([("aware", 0), ("aware", 120), ("panic", 130)])
    levels = [lv for lv, _ in got]
    gates = [g for _, g in got]
    chk("Aware → 持续升级 → Panic 共推 3 次", len(got) == 3, str(got))
    chk("最后一条是 critical", levels[-1:] == [S.LEVEL_CRIT], str(levels))
    chk("Panic 走 sentry_panic 闸门", gates[-1:] == ["sentry_panic"], str(gates))

    # ── 同一级别内的重复要被压制，否则 Panic 会连环轰炸 ────────────────
    got = await scenario([("aware", 0), ("panic", 10), ("panic", 20), ("panic", 30)])
    chk("多次 Panic 只推一次", len([lv for lv in (x[0] for x in got) if lv == S.LEVEL_CRIT]) == 1, str(got))

    # ── 一上来就 Panic：首推即 critical ───────────────────────────────
    got = await scenario([("panic", 0)])
    chk("首次即 Panic 直接 critical", [lv for lv, _ in got] == [S.LEVEL_CRIT], str(got))

    # ── 没到时长门槛不该升级 ─────────────────────────────────────────
    got = await scenario([("aware", 0), ("aware", 10)], escalate_seconds=90)
    chk("未到 90 秒不升级", len(got) == 1, str(got))

    # ── 到了门槛升一次，之后不再重复升 ────────────────────────────────
    got = await scenario([("aware", 0), ("aware", 100), ("aware", 200)], escalate_seconds=90)
    chk("持续活动只升一次", len(got) == 2, str(got))

    # ── 投递失败时不能置闩，否则下一次真事件被吞 ──────────────────────
    FIRED.clear()
    async def _drop(*a, **k):
        return False        # 模拟被静音时段/规则开关丢弃
    tracker = S.SentryTracker()
    _install_stubs(tracker)
    tracker.bind(_drop)
    await tracker.observe("1", "Car", "fleet", state="aware")
    sess = tracker._sessions["1"]
    sess.started = sess.last_seen - 200
    await tracker.observe("1", "Car", "fleet", state="aware")
    chk("推送被丢弃时不置升级闩", sess.escalated_level == "", repr(sess.escalated_level))

    print()
    print("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
