"""充电成本核算。

TeslaMate 的 charging_processes.cost 需要手动逐次填写，绝大多数人是空的。
这里根据电价方案自动算钱：

方案结构（存在 hub 自有库）:
{
  "currency": "CNY",
  "timezone": "Asia/Shanghai",          # 电价时段所用时区（留空跟随 TESLACAM_TZ）
  "default": {"type": "flat", "rate": 0.6},
  "locations": {                        # 按地理围栏名覆盖
     "家": {"type": "tou", "periods": [
              {"from": "22:00", "to": "08:00", "rate": 0.35, "name": "谷"},
              {"from": "08:00", "to": "17:00", "rate": 0.65, "name": "平"},
              {"from": "17:00", "to": "22:00", "rate": 0.95, "name": "峰"}]},
     "公司": {"type": "flat", "rate": 0}          # 免费
  },
  "supercharger": {"type": "flat", "rate": 2.0},  # 直流快充（超充/第三方）
  "free_supercharging": false
}

分时电价按 `charges` 表逐条采样积分：每两条采样之间的能量增量按**时间占比**
拆分到各时段（跨时段边界自动切分），再乘以各自单价。

回写安全性：只回写 cost 为空的场次，或本工具此前写过的场次——
绝不覆盖用户在 TeslaMate 里手填的、或 TeslaMate 依地理围栏算出的费用。
"""

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db
from ..alerts.store import store
from ..config import settings

log = logging.getLogger(__name__)

DEFAULT_PLAN = {
    "currency": "",          # 空 = 跟随地区偏好，见 tariff/router.py

    "timezone": "",
    "default": {"type": "flat", "rate": 0.0},
    "locations": {},
    "supercharger": {"type": "flat", "rate": 0.0},
    "free_supercharging": False,
}


async def get_plan() -> dict:
    plan = await store.get_setting("tariff_plan")
    if not plan:
        return dict(DEFAULT_PLAN)
    merged = dict(DEFAULT_PLAN)
    merged.update(plan)
    return merged


async def set_plan(plan: dict) -> None:
    await store.set_setting("tariff_plan", plan)
    await store.set_setting("tariff_dirty", True)  # 洞察页据此提示"需要重算"


async def is_dirty() -> bool:
    return bool(await store.get_setting("tariff_dirty", False))


def is_unconfigured(plan: dict) -> bool:
    """方案是否等同于"没配"（全零费率且无地点覆盖）——此时禁止回写，防清零。"""
    def zero(rule) -> bool:
        if not isinstance(rule, dict):
            return True
        if rule.get("type") == "tou":
            return all(float(p.get("rate") or 0) == 0 for p in rule.get("periods") or [])
        return float(rule.get("rate") or 0) == 0

    return (
        zero(plan.get("default"))
        and zero(plan.get("supercharger"))
        and not plan.get("locations")
    )


def plan_tz(plan: dict):
    """电价时段所用时区：方案里指定 > TESLACAM_TZ > 系统时区。"""
    for name in (plan.get("timezone"), settings.teslacam_tz):
        if name:
            try:
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001
                log.warning("invalid tariff timezone %r", name)
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_hm(s: str) -> time:
    h, _, m = str(s).partition(":")
    return time(int(h), int(m or 0))


def validate(plan: dict) -> list[str]:
    """返回人类可读的方案问题清单（时段没盖满 24 小时会导致按 0 元计费）。"""
    problems: list[str] = []

    def check(rule, label):
        if not isinstance(rule, dict) or rule.get("type") != "tou":
            return
        marks = [False] * (24 * 60)
        for p in rule.get("periods") or []:
            try:
                a, b = _parse_hm(p["from"]), _parse_hm(p["to"])
            except (KeyError, ValueError):
                problems.append(f"{label}: 时段「{p.get('name', '?')}」的起止时间无效")
                continue
            am, bm = a.hour * 60 + a.minute, b.hour * 60 + b.minute
            rng = range(am, bm) if am < bm else [*range(am, 1440), *range(0, bm)]
            for m in rng:
                marks[m] = True
        gaps = sum(1 for m in marks if not m)
        if gaps:
            problems.append(
                f"{label}: 有 {gaps // 60} 小时 {gaps % 60} 分钟没有被任何时段覆盖，"
                "这些时间段的电费会算成 0"
            )

    check(plan.get("default"), "默认电价")
    check(plan.get("supercharger"), "直流快充")
    for name, rule in (plan.get("locations") or {}).items():
        check(rule, f"地点「{name}」")
    return problems


def _rate_at(rule: dict, dt_local: datetime) -> tuple[float, str]:
    """返回 (单价, 时段名)。"""
    if rule.get("type") != "tou":
        return float(rule.get("rate") or 0), "固定"
    now = dt_local.time()
    for p in rule.get("periods", []):
        try:
            start, end = _parse_hm(p["from"]), _parse_hm(p["to"])
        except (KeyError, ValueError):
            continue
        inside = start <= now < end if start <= end else (now >= start or now < end)
        if inside:
            return float(p.get("rate") or 0), p.get("name") or "分时"
    return 0.0, "未覆盖"


def _boundaries(rule: dict) -> list[int]:
    """时段切换点（一天内的分钟数），升序去重。"""
    marks: set[int] = set()
    for p in rule.get("periods") or []:
        for key in ("from", "to"):
            try:
                t = _parse_hm(p[key])
            except (KeyError, ValueError):
                continue
            marks.add(t.hour * 60 + t.minute)
    return sorted(marks)


def _next_boundary(bounds: list[int], dt: datetime) -> datetime:
    """dt 之后的下一个时段切换时刻。"""
    mins = dt.hour * 60 + dt.minute
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    for b in bounds:
        cand = day + timedelta(minutes=b)
        if cand > dt:
            return cand
    return day + timedelta(days=1, minutes=bounds[0])  # 跨到明天第一个切换点


def _split_interval(rule: dict, t0: datetime, t1: datetime) -> list[tuple[float, float, str]]:
    """把 [t0, t1) 按时段边界**精确**切分，返回 [(时长占比, 单价, 时段名)]。

    电量按时间占比分摊到各时段——这才是"跨时段自动切分"，
    而不是把整段电量算在某一端的价格上。
    """
    total = (t1 - t0).total_seconds()
    if total <= 0:
        return []
    if rule.get("type") != "tou":
        rate, name = _rate_at(rule, t0)
        return [(1.0, rate, name)]

    bounds = _boundaries(rule)
    if not bounds:
        rate, name = _rate_at(rule, t0)
        return [(1.0, rate, name)]

    segs: list[tuple[float, float, str]] = []
    cur = t0
    guard = 0
    while cur < t1 and guard < 200:  # guard: 防御畸形配置导致的死循环
        guard += 1
        rate, name = _rate_at(rule, cur)
        nxt = min(_next_boundary(bounds, cur), t1)
        if nxt <= cur:
            nxt = min(cur + timedelta(minutes=1), t1)
        dur = (nxt - cur).total_seconds()
        if segs and segs[-1][1] == rate and segs[-1][2] == name:
            segs[-1] = (segs[-1][0] + dur, rate, name)
        else:
            segs.append((dur, rate, name))
        cur = nxt
    return [(d / total, r, n) for d, r, n in segs]


def _pick_rule(plan: dict, location: str | None, is_dc: bool) -> dict:
    if location and location in plan.get("locations", {}):
        return plan["locations"][location]
    if is_dc:
        if plan.get("free_supercharging"):
            return {"type": "flat", "rate": 0}
        return plan.get("supercharger") or plan["default"]
    return plan.get("default") or {"type": "flat", "rate": 0}


def _local(dt: datetime, tz) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(tz)


async def compute_session(cp_id: int, plan: dict, tz) -> dict | None:
    """按采样逐段积分算出一次充电的费用（跨时段按时间占比切分）。"""
    head = await db.fetchrow(
        """
        SELECT cp.id, cp.start_date, cp.end_date, cp.charge_energy_added,
               COALESCE(g.name, a.city, a.display_name, '') AS location,
               COALESCE(fc.fast, false) AS is_dc
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        LEFT JOIN LATERAL (
            SELECT bool_or(fast_charger_present) AS fast
            FROM charges WHERE charging_process_id = cp.id
        ) fc ON TRUE
        WHERE cp.id = $1
        """,
        cp_id,
    )
    if head is None or not head["charge_energy_added"]:
        return None

    # 与洞察页保持同一口径：以车辆上报的"快充"标志判定直流，
    # 而不是用峰值功率阈值（超充尾段功率会掉到 50kW 以下）
    is_dc = bool(head["is_dc"])
    rule = _pick_rule(plan, head["location"] or None, is_dc)

    rows = await db.fetch(
        "SELECT date, charge_energy_added FROM charges"
        " WHERE charging_process_id = $1 ORDER BY date",
        cp_id,
    )
    total_kwh = float(head["charge_energy_added"])
    cost = 0.0
    uncovered_kwh = 0.0
    breakdown: dict[str, dict] = {}

    def add(kwh: float, rate: float, name: str):
        nonlocal cost, uncovered_kwh
        cost += kwh * rate
        if name == "未覆盖":
            uncovered_kwh += kwh
        b = breakdown.setdefault(name, {"kwh": 0.0, "cost": 0.0, "rate": rate})
        b["kwh"] += kwh
        b["cost"] += kwh * rate

    if len(rows) >= 2:
        prev_e = float(rows[0]["charge_energy_added"] or 0)
        prev_t = rows[0]["date"]
        for r in rows[1:]:
            e = float(r["charge_energy_added"] or 0)
            delta, t = e - prev_e, r["date"]
            if delta > 0:
                for frac, rate, name in _split_interval(
                    rule, _local(prev_t, tz), _local(t, tz)
                ):
                    add(delta * frac, rate, name)
            prev_e, prev_t = e, t
    else:
        # 采样不足（很短的充电）：按开始时刻的单价整段算
        rate, name = _rate_at(rule, _local(head["start_date"], tz))
        add(total_kwh, rate, name)

    return {
        "id": cp_id,
        "cost": round(cost, 2),
        "kwh": round(total_kwh, 2),
        "location": head["location"],
        "is_dc": is_dc,
        "rule_type": rule.get("type", "flat"),
        "uncovered_kwh": round(uncovered_kwh, 2),
        "breakdown": {
            k: {"kwh": round(v["kwh"], 2), "cost": round(v["cost"], 2), "rate": v["rate"]}
            for k, v in breakdown.items()
        },
    }


async def recompute_all(car_id: int | None = None, write_back: bool = False) -> dict:
    """重算充电成本，存入自有库；可选回写 TeslaMate 让 Grafana 也能看到。

    回写规则（保护用户数据）：
    - 方案还是"全零默认值"时一律不回写；
    - 只写 cost 为空的场次，或本工具此前写过的场次；
    - 时段没覆盖到的电量算不出价，跳过回写并在结果里报告。
    """
    plan = await get_plan()
    tz = plan_tz(plan)
    unconfigured = is_unconfigured(plan)
    if write_back and unconfigured:
        write_back = False
        log.warning("cost write-back skipped: tariff plan is still all-zero default")

    ours: set[int] = {int(i) for i in (await store.get_setting("cost_written_ids", []) or [])}

    where = "WHERE charge_energy_added > 0.1" + (" AND car_id = $1" if car_id else "")
    args = [car_id] if car_id else []
    ids = [r["id"] for r in await db.fetch(f"SELECT id FROM charging_processes {where} ORDER BY id", *args)]  # noqa: S608

    # 保留其他车已算好的结果（本次可能只重算了一辆车）
    costs: dict[str, float] = dict(await store.get_setting("charge_costs", {}) or {})
    total = 0.0
    written = 0
    skipped_protected = 0
    uncovered_sessions = 0

    for cp_id in ids:
        res = await compute_session(cp_id, plan, tz)
        if res is None:
            continue
        costs[str(cp_id)] = res["cost"]
        total += res["cost"]
        if res["uncovered_kwh"] > 0.05:
            uncovered_sessions += 1
        if not write_back or res["uncovered_kwh"] > 0.05:
            continue
        try:
            if cp_id in ours:
                status = await db.execute(
                    "UPDATE charging_processes SET cost = $1 WHERE id = $2", res["cost"], cp_id
                )
            else:
                # 只填空白，绝不覆盖手填/地理围栏算出的费用
                status = await db.execute(
                    "UPDATE charging_processes SET cost = $1 WHERE id = $2 AND cost IS NULL",
                    res["cost"], cp_id,
                )
            if str(status).endswith(" 0"):
                skipped_protected += 1
            else:
                ours.add(cp_id)
                written += 1
        except Exception:  # noqa: BLE001
            log.exception("cost write-back failed for %s", cp_id)

    await store.set_setting("charge_costs", costs)
    await store.set_setting("cost_written_ids", sorted(ours))
    await store.set_setting("tariff_dirty", False)
    log.info(
        "recomputed %d charging costs (total %.2f, written=%d, protected=%d)",
        len(ids), total, written, skipped_protected,
    )
    return {
        "sessions": len(ids),
        "total_cost": round(total, 2),
        "currency": plan.get("currency") or "USD",
        "written_back": written,
        "protected": skipped_protected,
        "uncovered_sessions": uncovered_sessions,
        "unconfigured": unconfigured,
        "warnings": validate(plan),
    }


async def costs_map() -> dict[str, float]:
    return await store.get_setting("charge_costs", {}) or {}
