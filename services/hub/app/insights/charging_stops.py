"""充电"停了"是不是一次**中断**？—— 给告警规则 charging_interrupted 用的数据库侧判断。

审计 alerts F10：charging_interrupted 线上触发 11 次、11 次都是误报。其中 8 次在
06:00:05–06:00:21 —— 正是车主谷电时段结束、车按计划停充的时刻（TeslaMate 里对应
场次全都结束在 11:00 UTC = 06:00 CDT）；另外 3 次是车主自己结束的（两次直流快充、
一次 14 分钟的补电）。规则只看 `Charging → Stopped 且低于上限-2%`，分不出这些。

这里从历史里**学**这辆车的"惯常停充时刻"：最近 N 天里已结束的交流场次，按本地
时刻聚类，±6 分钟内至少 2 场结束在同一时刻，就是一个计划停充点。外加三条直接
信号：到了充电上限、直流快充（车主拔枪走人）、定时/谷电充电正开着。

告警引擎（alerts/，不归这里）只需要在 Stopped 时调 classify_stop()，
interrupted 为 False 就不报；NoPower 照报（那才是真断电）。见 NEEDS.md。
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db
from .period import local_tz_name, utcnow

log = logging.getLogger(__name__)

TOLERANCE_MIN = 6
MIN_SESSIONS = 2
LOOKBACK_DAYS = 90


def _minute_of_day(dt: datetime, tz: ZoneInfo) -> int:
    loc = dt.replace(tzinfo=timezone.utc).astimezone(tz)
    return loc.hour * 60 + loc.minute


def _circ(a: int, b: int) -> int:
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


def clusters(minutes: list[int], tol: int = TOLERANCE_MIN, min_n: int = MIN_SESSIONS) -> list[dict]:
    """把一天里的若干个时刻（分钟）聚成"惯常时刻"。贪心：每次取邻域最密的那个点。"""
    left = list(minutes)
    out = []
    while left:
        best = max(left, key=lambda m: sum(1 for x in left if _circ(m, x) <= tol))
        members = [x for x in left if _circ(best, x) <= tol]
        if len(members) < min_n:
            break
        # 圆周上取中位：以 best 为原点展开再取中位，避免 23:58/00:02 平均成 12:00
        rel = sorted(((x - best + 720) % 1440) - 720 for x in members)
        center = (best + rel[len(rel) // 2]) % 1440
        out.append({"minute": center, "local_time": f"{center // 60:02d}:{center % 60:02d}",
                    "sessions": len(members)})
        left = [x for x in left if x not in members]
    return out


async def _history(car_id: int, days: int) -> list:
    return await db.fetch(
        """
        SELECT cp.id, cp.start_date, cp.end_date, cp.end_battery_level,
               COALESCE(fc.fast, false) AS is_dc
        FROM charging_processes cp
        LEFT JOIN LATERAL (SELECT bool_or(fast_charger_present) AS fast FROM charges
                           WHERE charging_process_id = cp.id) fc ON TRUE
        WHERE cp.car_id = $1 AND cp.end_date IS NOT NULL
          AND cp.end_date >= $2
        ORDER BY cp.end_date
        """,
        car_id, utcnow() - timedelta(days=days),
    )


async def learned_schedule(car_id: int, days: int = LOOKBACK_DAYS) -> dict:
    """这辆车惯常的开始/停止充电时刻（本地时间）。只看交流场次 —— 直流是在路上，不规律。"""
    tz = ZoneInfo(local_tz_name())
    rows = [r for r in await _history(car_id, days) if not r["is_dc"]]
    return {
        "tz": tz.key,
        "lookback_days": days,
        "ac_sessions": len(rows),
        "stop_times": clusters([_minute_of_day(r["end_date"], tz) for r in rows]),
        "start_times": clusters([_minute_of_day(r["start_date"], tz) for r in rows]),
    }


async def classify_stop(car_id: int, ended_at: datetime | None = None, *,
                        end_soc: float | None = None, charge_limit_soc: float | None = None,
                        is_dc: bool | None = None,
                        scheduled_charging_start_time: str | None = None,
                        charging_state: str | None = None) -> dict:
    """一次 Charging→(Stopped/Complete/NoPower/…) 该不该当成"中断"报。

    参数都可以只给一部分（MQTT 里有什么给什么）：
      ended_at   停止时刻（naive UTC 或带时区；默认现在）
      end_soc / charge_limit_soc  停止时的电量与上限
      is_dc      是否直流快充；None = 从库里最近一场的采样判断（MQTT 没有这个主题）
      scheduled_charging_start_time  TeslaMate MQTT 同名主题（空 = 没开定时充电）
      charging_state  新状态；NoPower 永远判为中断
    返回 {"interrupted": bool, "reason": str, "evidence": {...}}，reason 取值：
      no_power / limit_reached / dc_session / scheduled_stop / scheduled_charging / unexplained
    """
    if charging_state and charging_state.lower() == "nopower":
        return {"interrupted": True, "reason": "no_power", "evidence": {}}
    if charging_state and charging_state.lower() == "complete":
        return {"interrupted": False, "reason": "limit_reached", "evidence": {}}
    ev: dict = {}
    if end_soc is not None and charge_limit_soc is not None:
        ev.update(end_soc=end_soc, charge_limit_soc=charge_limit_soc)
        if float(end_soc) >= float(charge_limit_soc) - 1:
            return {"interrupted": False, "reason": "limit_reached", "evidence": ev}
    if is_dc is None:
        # TeslaMate 的 MQTT 没有 fast_charger_present；从库里最近一场（多半还开着）的采样判断
        try:
            is_dc = bool(await db.fetchval(
                """
                SELECT bool_or(c.fast_charger_present) FROM charges c
                WHERE c.charging_process_id = (
                    SELECT id FROM charging_processes WHERE car_id = $1
                    ORDER BY start_date DESC LIMIT 1)
                """,
                car_id,
            ))
            ev["is_dc_from_db"] = is_dc
        except db.DbUnavailable:
            is_dc = False
    if is_dc:
        return {"interrupted": False, "reason": "dc_session", "evidence": ev}

    when = ended_at or datetime.now(timezone.utc)
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        sched = await learned_schedule(car_id)
    except db.DbUnavailable:
        sched = {"stop_times": [], "tz": local_tz_name()}
    tz = ZoneInfo(sched["tz"])
    m = _minute_of_day(when, tz)
    ev["local_time"] = f"{m // 60:02d}:{m % 60:02d}"
    for c in sched.get("stop_times", []):
        if _circ(m, c["minute"]) <= TOLERANCE_MIN:
            ev["learned_stop"] = c
            return {"interrupted": False, "reason": "scheduled_stop", "evidence": ev}
    if scheduled_charging_start_time and str(scheduled_charging_start_time).strip():
        ev["scheduled_charging_start_time"] = scheduled_charging_start_time
        return {"interrupted": False, "reason": "scheduled_charging", "evidence": ev}
    return {"interrupted": True, "reason": "unexplained", "evidence": ev}
