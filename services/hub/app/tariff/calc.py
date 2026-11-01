"""充电成本核算。

TeslaMate 的 charging_processes.cost 需要手动逐次填写，绝大多数人是空的。
这里根据电价方案自动算钱：

方案结构（存在 hub 自有库）:
{
  "currency": "CNY",
  "timezone": "Asia/Shanghai",          # 电价时段所用时区（留空跟随 TZ / TESLACAM_TZ）
  "default": {"type": "flat", "rate": 0.6},
  "locations": {                        # 按地理围栏名覆盖
     "家": {"type": "tou", "periods": [
              {"from": "22:00", "to": "08:00", "rate": 0.35, "name": "谷"},
              {"from": "08:00", "to": "17:00", "rate": 0.65, "name": "平"},
              {"from": "17:00", "to": "22:00", "rate": 0.95, "name": "峰"}]},
     "公司": {"type": "flat", "rate": 0}          # 免费
  },
  "supercharger": {"type": "flat", "rate": 2.0},  # 直流快充（超充/第三方）
  "free_supercharging": false                     # 只对特斯拉超充生效
}

**计费电量 = 墙端电量**，与 TeslaMate 自己的 put_cost 完全同一口径：
`max(charge_energy_added, charge_energy_used)`（elixir/lib/teslamate/log.ex:632-680；
地理围栏回填的 SQL 是 `GREATEST(charge_energy_used, charge_energy_added)`）。
charge_energy_added 是**进电池**的电量，不含充电损耗；按它计费会把家充少算
10～15%（审计实测本车 AC 少算 14.7%）。

分时电价：这一场的计费电量按"每个采样区间实际从墙上取了多少电"分摊到时间轴上
（区间电量用 TeslaMate 积分 charge_energy_used 的同一个公式：交流
`相数×电流×电压`、直流 `charger_power`，再乘区间时长），跨时段边界按时间占比切开，
再乘各自单价。分摊权重归一化到计费电量，所以：
- 单一电价的结果与 TeslaMate 逐分不差（rate × max(added, used)）；
- charge_energy_added 计数器中途回落再爬升时，不会把爬回来的那段算两遍
  （旧算法把所有正增量相加，审计 cp 9 因此多算了 2.99 kWh）。

回写安全性：只回写 cost 为空的场次，或本工具此前写过、且库里仍是我们写的那个值
的场次 —— 绝不覆盖用户在 TeslaMate 里手填的、或 TeslaMate 依地理围栏算出的费用。
全部定价（自动补算与手动重算）都在同一把锁里串行，双击"重算"不会互相踩。
"""

import asyncio
import hashlib
import json
import logging
import time as _time
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db
from ..alerts.store import store
from ..config import settings

log = logging.getLogger(__name__)

# 时段没盖住的那部分电量用这个标记。必须是用户不可能拿来当时段名的字符串：
# 之前用的是中文"未覆盖"，用户真给某个时段起这个名字就会被当成没覆盖，
# 从而永久阻塞该场次的回写。breakdown 的键也用中性标识，别把中文塞进接口。
UNCOVERED = "__uncovered__"
FLAT = "__flat__"
TOU = "__tou__"
FREE = "__free_supercharging__"

# 计价方法版本。改了算法（例如从电池端换到墙端电量）就加一：老结果自动作废重算。
COST_METHOD = 2
COSTS_KEY = "tariff_costs"            # {"method": 2, "costs": {cp_id: {c, k, p, t}}}
REGISTRY_KEY = "cost_written_ids"     # {cp_id: 我们写进 TeslaMate 的那个值}

# 计费电量低于这个值的场次不定价（插枪即拔、计数器全零）
MIN_BILLED_KWH = 0.01
_BATCH = 25

DEFAULT_PLAN = {
    "currency": "",          # 空 = 跟随地区偏好，见 tariff/router.py

    "timezone": "",
    "default": {"type": "flat", "rate": 0.0},
    "locations": {},
    "supercharger": {"type": "flat", "rate": 0.0},
    "free_supercharging": False,
}

# 所有定价（自动补算、手动重算、回写）共用一把锁。以前两次并发的重算各自在
# 开头读一份登记表、结尾整份写回：后结束的那次把先结束那次的登记冲掉，
# 之后所有回写过的场次都被当成"用户手填"，费用永久冻结在旧价格上。
_pricing_lock = asyncio.Lock()


# 常见的分时电价形态。**只给时段结构，费率一律留 0 让用户自己填** ——
# 各地各档费率年年变，内置一个数字出来，用户很可能直接用了，然后账就是错的。
# 时段结构则相对稳定，而且正是最烦人、最容易填错（漏掉几小时）的部分。
TEMPLATES = [
    {
        "id": "cn_peak_valley",
        "name_zh": "中国 峰谷平（常见三段）", "name_en": "China peak / mid-peak / off-peak",
        "periods": [
            {"from": "22:00", "to": "08:00", "name_zh": "谷", "name_en": "Off-peak"},
            {"from": "08:00", "to": "17:00", "name_zh": "平", "name_en": "Mid-peak"},
            {"from": "17:00", "to": "22:00", "name_zh": "峰", "name_en": "Peak"},
        ],
    },
    {
        "id": "us_tou_evening",
        "name_zh": "美国 傍晚峰时（典型 TOU）", "name_en": "US evening peak (typical TOU)",
        "periods": [
            {"from": "00:00", "to": "16:00", "name_zh": "非峰", "name_en": "Off-peak"},
            {"from": "16:00", "to": "21:00", "name_zh": "峰", "name_en": "On-peak"},
            {"from": "21:00", "to": "00:00", "name_zh": "非峰", "name_en": "Off-peak"},
        ],
    },
    {
        "id": "uk_economy7",
        "name_zh": "英国 Economy 7（夜间七小时）", "name_en": "UK Economy 7",
        "periods": [
            {"from": "00:30", "to": "07:30", "name_zh": "夜间", "name_en": "Night"},
            {"from": "07:30", "to": "00:30", "name_zh": "日间", "name_en": "Day"},
        ],
    },
    {
        "id": "flat",
        "name_zh": "单一电价（不分时段）", "name_en": "Single flat rate",
        "periods": [],
    },
]


def template(tid: str, locale: str = "en") -> dict | None:
    """把模板展开成一份 plan 的 default 规则。费率留 0，等用户填。"""
    tpl = next((t for t in TEMPLATES if t["id"] == tid), None)
    if tpl is None:
        return None
    if not tpl["periods"]:
        return {"type": "flat", "rate": 0.0}
    key = "name_zh" if locale == "zh" else "name_en"
    return {
        "type": "tou",
        "periods": [
            {"from": p["from"], "to": p["to"], "name": p[key], "rate": 0.0}
            for p in tpl["periods"]
        ],
    }


def normalize_plan(plan: dict) -> dict:
    """存/读之前统一形状：`locations: null`、`{"家": null}` 这类条目一律去掉。

    以前 validate_plan 放行了它们，结果预览和重算在 `location in None` /
    `None.get()` 上 500（审计 F17）。null 规则最自然的理解就是"这一条不要了"。
    """
    out = dict(plan or {})
    locs = out.get("locations")
    out["locations"] = (
        {str(k): v for k, v in locs.items() if isinstance(v, dict)}
        if isinstance(locs, dict) else {}
    )
    for key in ("default", "supercharger"):
        if out.get(key) is not None and not isinstance(out.get(key), dict):
            out[key] = None
    return out


async def get_plan() -> dict:
    plan = await store.get_setting("tariff_plan")
    merged = dict(DEFAULT_PLAN)
    if plan and isinstance(plan, dict):
        merged.update(plan)
    return normalize_plan(merged)


async def plan_saved() -> bool:
    """用户是否保存过方案。没保存过就不自动定价（全零默认值算出来的 0 元是假数据）。"""
    return bool(await store.get_setting("tariff_plan"))


async def set_plan(plan: dict) -> None:
    await store.set_setting("tariff_plan", normalize_plan(plan))
    await store.set_setting("tariff_dirty", True)  # 洞察页据此提示"需要重算"


async def is_dirty() -> bool:
    return bool(await store.get_setting("tariff_dirty", False))


def plan_hash(plan: dict) -> str:
    """方案的指纹（不含币种 —— 换币种不改变数值）。用来标出"按旧方案算的"费用。"""
    p = {k: v for k, v in normalize_plan(plan).items() if k != "currency"}
    raw = json.dumps(p, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _num(v, default: float = 0.0) -> float:
    """把费率转成数。

    存进来的 JSON 可能带任何东西（空串、null、用户手打的 "0.6 元"）。
    以前是裸 float()，一条坏数据就让 recompute 和 preview 永久 500 —— 而且
    是在**读**的路径上炸，用户既看不到费用也改不回来，只能去翻数据库。
    读的时候宽容、写的时候严格，才不会把自己锁在门外。
    """
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def validate_plan(plan: dict) -> list[str]:
    """保存前的结构校验。返回人话的错误列表，空列表表示可以存。"""
    errs: list[str] = []
    if not isinstance(plan, dict):
        return ["plan must be an object"]

    def check_rule(rule, where: str) -> None:
        if rule is None:
            return
        if not isinstance(rule, dict):
            errs.append(f"{where}: must be an object")
            return
        kind = rule.get("type", "flat")
        if kind not in ("flat", "tou"):
            errs.append(f"{where}: type must be flat or tou")
        if kind == "flat":
            if _num(rule.get("rate"), -1) < 0:
                errs.append(f"{where}: rate must be a number >= 0")
        else:
            periods = rule.get("periods")
            if not isinstance(periods, list) or not periods:
                errs.append(f"{where}: tou needs a non-empty periods list")
            else:
                for i, prd in enumerate(periods):
                    if not isinstance(prd, dict):
                        errs.append(f"{where}.periods[{i}]: must be an object")
                        continue
                    if _num(prd.get("rate"), -1) < 0:
                        errs.append(f"{where}.periods[{i}]: rate must be a number >= 0")
                    for k in ("from", "to"):
                        try:
                            _parse_hm(prd[k])
                        except (KeyError, ValueError, TypeError):
                            errs.append(f"{where}.periods[{i}].{k}: expected HH:MM")

    check_rule(plan.get("default"), "default")
    check_rule(plan.get("supercharger"), "supercharger")
    locs = plan.get("locations")
    if locs is not None and not isinstance(locs, dict):
        errs.append("locations: must be an object keyed by place name")
    elif isinstance(locs, dict):
        for name, rule in locs.items():
            check_rule(rule, f"locations[{name}]")   # null 规则合法：保存时会被去掉
    tz = plan.get("timezone")
    if tz:
        try:
            ZoneInfo(str(tz))
        except Exception:  # noqa: BLE001
            errs.append(f"timezone: unknown time zone {tz!r}")
    return errs


def _is_zero_rule(rule) -> bool:
    """这条规则是不是"等于没配"（费率全零）。"""
    if not isinstance(rule, dict):
        return True
    if rule.get("type") == "tou":
        periods = rule.get("periods") or []
        if not periods:
            return True
        return all(
            _num(p.get("rate")) == 0
            for p in periods if isinstance(p, dict)
        )
    return _num(rule.get("rate")) == 0


def is_unconfigured(plan: dict) -> bool:
    """方案是否等同于"没配"（全零费率且无地点覆盖）——此时禁止回写，防清零。"""
    return (
        _is_zero_rule(plan.get("default"))
        and _is_zero_rule(plan.get("supercharger"))
        and not (plan.get("locations") or {})
    )


def plan_tz(plan: dict):
    """电价时段所用时区：方案里指定 > 本地时区（TZ / TESLACAM_TZ）> 系统时区。"""
    from ..insights.period import local_tz_name

    for name in (plan.get("timezone"), local_tz_name(), settings.teslacam_tz):
        if name and name != "UTC":
            try:
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001
                log.warning("invalid tariff timezone %r", name)
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_hm(s: str) -> time:
    h, _, m = str(s).partition(":")
    return time(int(h), int(m or 0))


def validate(plan: dict) -> list[dict]:
    """方案问题清单（时段没盖满 24 小时会导致那几个钟头按 0 元计费）。

    返回的是结构化 {code, scope, name, ...} 而不是成品文案 —— 这个结果直接渲染
    在设置页上，写死中文的话英文界面会冒出中文。文案由前端按 code 取词条。
    """
    problems: list[dict] = []

    def check(rule, scope: str, place: str = ""):
        if not isinstance(rule, dict) or rule.get("type") != "tou":
            return
        marks = [False] * (24 * 60)
        for p in rule.get("periods") or []:
            if not isinstance(p, dict):
                problems.append({"code": "bad_period", "scope": scope, "place": place, "period": ""})
                continue
            try:
                a, b = _parse_hm(p["from"]), _parse_hm(p["to"])
            except (KeyError, ValueError, TypeError):
                problems.append({"code": "bad_period", "scope": scope, "place": place,
                                 "period": str(p.get("name") or "")})
                continue
            am, bm = a.hour * 60 + a.minute, b.hour * 60 + b.minute
            if am == bm:
                rng = range(1440)      # 与 _rate_at 一致：起止相同 = 全天
            elif am < bm:
                rng = range(am, bm)
            else:
                rng = [*range(am, 1440), *range(bm)]
            for m in rng:
                marks[m] = True
        gaps = sum(1 for m in marks if not m)
        if gaps:
            problems.append({"code": "gap", "scope": scope, "place": place,
                             "hours": gaps // 60, "minutes": gaps % 60})

    check(plan.get("default"), "default")
    check(plan.get("supercharger"), "supercharger")
    for name, rule in (plan.get("locations") or {}).items():
        check(rule, "location", str(name))
    return problems


def _rate_at(rule: dict, dt_local: datetime) -> tuple[float, str]:
    """返回 (单价, 时段名)。"""
    if rule.get("type") != "tou":
        return _num(rule.get("rate")), FLAT
    now = dt_local.time()
    for p in rule.get("periods") or []:
        try:
            start, end = _parse_hm(p["from"]), _parse_hm(p["to"])
        except (KeyError, ValueError, TypeError):
            log.debug("suppressed", exc_info=True)
            continue
        if start == end:
            # 00:00→00:00 是"全天一个价"最自然的写法。按 start <= now < end
            # 判永远为假，而 validate() 那边又算它盖满了 24 小时——
            # 于是没有任何警告，每一度电却都按 0 元记。
            inside = True
        elif start < end:
            inside = start <= now < end
        else:
            inside = now >= start or now < end
        if inside:
            return _num(p.get("rate")), p.get("name") or TOU
    return 0.0, UNCOVERED


def _boundaries(rule: dict) -> list[int]:
    """时段切换点（一天内的分钟数），升序去重。"""
    marks: set[int] = set()
    for p in rule.get("periods") or []:
        for key in ("from", "to"):
            try:
                t = _parse_hm(p[key])
            except (KeyError, ValueError, TypeError):
                log.debug("suppressed", exc_info=True)
                continue
            marks.add(t.hour * 60 + t.minute)
    return sorted(marks)


def _next_boundary(bounds: list[int], dt: datetime) -> datetime:
    """dt 之后的下一个时段切换时刻。"""
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
    while cur < t1 and guard < 400:  # guard: 防御畸形配置导致的死循环
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
    if cur < t1:   # 防御：剩下的部分记在最后一段的价格上，总量守恒
        rate, name = _rate_at(rule, cur)
        segs.append(((t1 - cur).total_seconds(), rate, name))
    return [(d / total, r, n) for d, r, n in segs]


def _pick_rule(plan: dict, location: str | None, is_dc: bool) -> dict:
    locs = plan.get("locations") or {}
    if location and isinstance(locs, dict) and isinstance(locs.get(location), dict):
        return locs[location]
    default = plan.get("default") if isinstance(plan.get("default"), dict) else None
    default = default or {"type": "flat", "rate": 0}
    if is_dc:
        # DEFAULT_PLAN 里 supercharger 恒存在（费率 0），所以 `or` 永远判不出
        # "没配这一项"——只填了默认电价的用户，所有直流快充会被算成 0 元。
        sc = plan.get("supercharger")
        return sc if (isinstance(sc, dict) and not _is_zero_rule(sc)) else default
    return default


def _local(dt: datetime, tz) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(tz)


def billed_kwh(added, used) -> float:
    """TeslaMate 的计费电量：max(added, used)，忽略 NULL（Postgres GREATEST 同款）。"""
    vals = [float(v) for v in (added, used) if v is not None]
    return max(*vals, 0.0) if vals else 0.0


def is_tesla_supercharger(head: dict) -> bool:
    """特斯拉超充（免费超充只对它生效）。

    TeslaMate put_cost：众数 fast_charger_type 以 "Tesla" 开头；4.4.0 的面板还认
    fast_charger_brand = 'Tesla' 与地址/围栏名里的 supercharger。第三方直流快充
    （Electrify America 之类）不在此列 —— 以前方案级 free_supercharging 把**所有**
    直流都清零了（审计 F18）。
    """
    if not head.get("is_dc"):
        return False
    ctype = str(head.get("charger_type") or "")
    brand = str(head.get("charger_brand") or "")
    where = " ".join(str(head.get(k) or "") for k in ("geofence", "addr_name", "location")).lower()
    return ctype.startswith("Tesla") or brand == "Tesla" or "supercharger" in where


def price(head: dict, intervals: list | None, plan: dict, tz) -> dict:
    """纯函数：给一场充电定价。

    head:      {id, start_date, added, used, location, is_dc, charger_type, charger_brand,
                geofence, addr_name, free_sc_car}
    intervals: [(t_prev, t, kwh_wall, d_added), ...]（naive UTC），只有分时规则才需要；
               None/空 时整场按开始时刻的单价算。
    """
    billed = billed_kwh(head.get("added"), head.get("used"))
    is_dc = bool(head.get("is_dc"))
    rule = _pick_rule(plan, head.get("location") or None, is_dc)
    free = (is_dc and is_tesla_supercharger(head)
            and bool(plan.get("free_supercharging") or head.get("free_sc_car")))

    cost = 0.0
    uncovered_kwh = 0.0
    breakdown: dict[str, dict] = {}

    def add(kwh: float, rate: float, name: str):
        nonlocal cost, uncovered_kwh
        cost += kwh * rate
        if name == UNCOVERED:
            uncovered_kwh += kwh
        b = breakdown.setdefault(name, {"kwh": 0.0, "cost": 0.0, "rate": rate})
        b["kwh"] += kwh
        b["cost"] += kwh * rate

    start = head.get("start_date")
    if free:
        add(billed, 0.0, FREE)
    elif rule.get("type") != "tou" or not intervals:
        rate, name = _rate_at(rule, _local(start, tz) if start else datetime.now(tz))
        add(billed, rate, name)
    else:
        # 权重：区间墙端电量（TeslaMate 积分 charge_energy_used 的同一公式）；
        # 全无功率数据时退回电池端正增量；再没有就整场记在开始时刻。
        weights = [(a, b, max(float(w or 0), 0.0)) for a, b, w, _ in intervals]
        total_w = sum(w for _, _, w in weights)
        if total_w <= 0:
            weights = [(a, b, max(float(d or 0), 0.0)) for a, b, _, d in intervals]
            total_w = sum(w for _, _, w in weights)
        if total_w <= 0:
            rate, name = _rate_at(rule, _local(start, tz) if start else datetime.now(tz))
            add(billed, rate, name)
        else:
            for a, b, w in weights:
                if w <= 0:
                    continue
                kwh = billed * w / total_w
                parts = _split_interval(rule, _local(a, tz), _local(b, tz))
                if not parts:    # 零时长区间：按端点单价
                    rate, name = _rate_at(rule, _local(b, tz))
                    parts = [(1.0, rate, name)]
                for frac, rate, name in parts:
                    add(kwh * frac, rate, name)

    added = head.get("added")
    used = head.get("used")
    return {
        "id": head.get("id"),
        "cost": round(cost, 2),
        "kwh": round(billed, 2),              # 计费电量（墙端）
        "kwh_billed": round(billed, 2),
        "kwh_added": round(float(added), 2) if added is not None else None,
        "kwh_used": round(float(used), 2) if used is not None else None,
        "energy_basis": "max(charge_energy_added, charge_energy_used)",
        "location": head.get("location"),
        "is_dc": is_dc,
        "free_supercharging": free,
        "rule_type": "free" if free else rule.get("type", "flat"),
        "uncovered_kwh": round(uncovered_kwh, 2),
        "breakdown": {
            k: {"kwh": round(v["kwh"], 2), "cost": round(v["cost"], 2), "rate": v["rate"]}
            for k, v in breakdown.items()
        },
    }


# ---------------------------------------------------------------- 取数

_HEAD_SQL = """
SELECT cp.id, cp.car_id, cp.start_date, cp.end_date, cp.duration_min,
       cp.charge_energy_added::float8 AS added, cp.charge_energy_used::float8 AS used,
       cp.cost::float8 AS db_cost,
       COALESCE(g.name, a.city, a.display_name, '') AS location,
       g.name AS geofence, a.name AS addr_name,
       COALESCE(fc.fast, false) AS is_dc, fc.brand AS charger_brand, fc.ctype AS charger_type,
       COALESCE(s.free_supercharging, false) AS free_sc_car
FROM charging_processes cp
LEFT JOIN geofences g ON g.id = cp.geofence_id
LEFT JOIN addresses a ON a.id = cp.address_id
LEFT JOIN cars c ON c.id = cp.car_id
LEFT JOIN car_settings s ON s.id = c.settings_id
LEFT JOIN LATERAL (
    SELECT bool_or(fast_charger_present) AS fast,
           mode() WITHIN GROUP (ORDER BY fast_charger_brand) AS brand,
           mode() WITHIN GROUP (ORDER BY fast_charger_type) FILTER (WHERE charger_power > 0) AS ctype
    FROM charges WHERE charging_process_id = cp.id
) fc ON TRUE
WHERE cp.id = ANY($1::int[])
"""

# 每个采样区间的墙端电量 —— 逐字照搬 TeslaMate calculate_energy_used/determine_phases
# （log.ex:561-630）：区间 (t[i-1], t[i]] 的电量 = 第 i 条的功率 × 区间时长。
# smallint×smallint 在直流上会溢出，先转 int / float。
_INTERVALS_SQL = """
WITH ph AS (
    SELECT charging_process_id AS cp,
           avg(charger_power * 1000.0
               / NULLIF(charger_actual_current::int * charger_voltage, 0))::float8 AS p,
           avg(charger_phases)::int AS r,
           count(*) AS n
    FROM charges WHERE charging_process_id = ANY($1::int[])
    GROUP BY 1
), phases AS (
    SELECT cp, CASE WHEN p IS NOT NULL AND p > 0 AND n > 15 THEN
                 CASE WHEN r = round(p) THEN r::float8
                      WHEN r = 3 AND abs(p / sqrt(3) - 1) <= 0.1 THEN sqrt(3)
                      WHEN round(p) > 0 AND abs(round(p) - p) <= 0.3 THEN round(p)::float8
                 END
               END AS phases
    FROM ph
), s AS (
    SELECT c.charging_process_id AS cp, c.date,
           lag(c.date) OVER w AS prev_date,
           (c.charge_energy_added - lag(c.charge_energy_added) OVER w)::float8 AS d_added,
           c.charger_phases, c.charger_power,
           c.charger_actual_current::int AS i, c.charger_voltage::int AS v
    FROM charges c WHERE c.charging_process_id = ANY($1::int[])
    WINDOW w AS (PARTITION BY c.charging_process_id ORDER BY c.date)
)
SELECT s.cp, s.prev_date, s.date, s.d_added,
       (CASE WHEN s.charger_phases IS NULL THEN s.charger_power::float8
             ELSE COALESCE(ph.phases * s.i * s.v / 1000.0, s.charger_power::float8) END)
         * EXTRACT(EPOCH FROM (s.date - s.prev_date))::float8 / 3600.0 AS kwh_wall
FROM s LEFT JOIN phases ph ON ph.cp = s.cp
WHERE s.prev_date IS NOT NULL
ORDER BY s.cp, s.date
"""


async def _heads(ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    rows = await db.fetch(_HEAD_SQL, ids)
    return {r["id"]: dict(r) for r in rows}


async def _intervals(ids: list[int]) -> dict[int, list]:
    if not ids:
        return {}
    out: dict[int, list] = {}
    for r in await db.fetch(_INTERVALS_SQL, ids):
        out.setdefault(r["cp"], []).append((r["prev_date"], r["date"], r["kwh_wall"], r["d_added"]))
    return out


async def compute_sessions(ids: list[int], plan: dict, tz) -> dict[int, dict]:
    """批量定价。只有命中分时规则的场次才去拉逐条采样（单一电价不需要）。"""
    out: dict[int, dict] = {}
    for i in range(0, len(ids), _BATCH):
        chunk = ids[i:i + _BATCH]
        heads = await _heads(chunk)
        need = [cid for cid, h in heads.items()
                if _pick_rule(plan, h.get("location") or None, bool(h.get("is_dc"))).get("type") == "tou"]
        samples = await _intervals(need)
        for cid, h in heads.items():
            res = price(h, samples.get(cid), plan, tz)
            res["car_id"] = h.get("car_id")
            res["open"] = h.get("end_date") is None
            out[cid] = res
    return out


async def compute_session(cp_id: int, plan: dict, tz) -> dict | None:
    """单场定价（预览接口用）。会话不存在返回 None。"""
    res = (await compute_sessions([int(cp_id)], plan, tz)).get(int(cp_id))
    return res


# ---------------------------------------------------------------- 存取

async def stored_costs() -> dict[int, dict]:
    raw = await store.get_setting(COSTS_KEY, {}) or {}
    if not isinstance(raw, dict) or raw.get("method") != COST_METHOD:
        return {}        # 老算法（电池端电量）的结果一律作废，自动补算会重算
    out: dict[int, dict] = {}
    for k, v in (raw.get("costs") or {}).items():
        try:
            out[int(k)] = v if isinstance(v, dict) else {"c": float(v)}
        except (TypeError, ValueError):
            log.warning("ignoring malformed stored cost %r=%r", k, v)
    return out


async def costs_map() -> dict[str, float]:
    """兼容旧调用方：{cp_id(str): cost}。"""
    return {str(k): float(v.get("c") or 0.0) for k, v in (await stored_costs()).items()}


async def _save_costs(results: dict[int, dict], phash: str) -> None:
    if not results:
        return
    now = round(_time.time())

    def apply(cur):
        cur = cur if isinstance(cur, dict) and cur.get("method") == COST_METHOD else {
            "method": COST_METHOD, "costs": {}}
        costs = dict(cur.get("costs") or {})
        for cid, res in results.items():
            costs[str(cid)] = {"c": res["cost"], "k": res["kwh"], "p": phash, "t": now}
        return {"method": COST_METHOD, "costs": costs}

    await store.mutate_setting(COSTS_KEY, apply, {})


async def written_registry() -> dict[int, float | None]:
    raw = await store.get_setting(REGISTRY_KEY, {}) or {}
    if isinstance(raw, list):   # 旧格式：写过，但不知道写的是什么
        return {int(i): None for i in raw}
    out: dict[int, float | None] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = None if v is None else float(v)
        except (TypeError, ValueError):
            log.warning("ignoring malformed cost registry entry %r=%r", k, v)
    return out


async def _registry_set(cid: int, value: float | None) -> None:
    def apply(cur):
        if isinstance(cur, list):
            cur = {str(i): None for i in cur}
        cur = dict(cur or {})
        if value is None:
            cur.pop(str(cid), None)
        else:
            cur[str(cid)] = value
        return cur

    await store.mutate_setting(REGISTRY_KEY, apply, {})


def owns(db_cost, registered) -> bool:
    """库里的值还是不是我们写进去的那个。"""
    return db_cost is not None and registered is not None and abs(float(db_cost) - registered) < 0.005


async def _write_back_one(cid: int, new_cost: float, registry: dict) -> str:
    """把一场的费用写进 TeslaMate。返回 written / unchanged / protected。

    规则：库里为空 → 填；库里仍是我们上次写的值 → 更新；否则（用户手填、
    TeslaMate 依围栏算出）→ 不碰，并从登记表里除名。
    每写一条就立刻持久化登记表：进程在中途被杀，下次也认得出哪些是我们写的。
    """
    prev = registry.get(cid)
    if prev is not None:
        # 库里已经是这个值就不写（重复重算是幂等的，也不去碰 TeslaMate 的表）：
        # 0 行 → 走下面的复核分支，报 unchanged
        status = await db.execute(
            "UPDATE charging_processes SET cost = $1"
            " WHERE id = $2 AND end_date IS NOT NULL"
            " AND (cost IS NULL OR abs(cost - $3) < 0.005)"
            " AND (cost IS NULL OR abs(cost - $1) >= 0.005)",
            new_cost, cid, prev,
        )
    else:
        status = await db.execute(
            "UPDATE charging_processes SET cost = $1"
            " WHERE id = $2 AND end_date IS NOT NULL AND cost IS NULL",
            new_cost, cid,
        )
    if not str(status).endswith(" 0"):
        registry[cid] = new_cost
        await _registry_set(cid, new_cost)
        return "written"
    # 没写进去：要么值本来就一样（上次写完登记前进程被杀），要么是别人的值
    cur = await db.fetchval("SELECT cost::float8 FROM charging_processes WHERE id = $1", cid)
    if cid in registry and cur is not None and abs(cur - new_cost) < 0.005:
        registry[cid] = new_cost
        await _registry_set(cid, new_cost)
        return "unchanged"
    if cid in registry:
        registry.pop(cid, None)
        await _registry_set(cid, None)
    return "protected"


async def _billable_ids(car_id: int | None) -> list[int]:
    """已结束、且真的取过电的场次。进行中的场次（end_date IS NULL）一律不定价：
    那时的电量是半截数，算出来必错，而且一旦写回就会盖住 TeslaMate 结束时的正确值。"""
    rows = await db.fetch(
        "SELECT id FROM charging_processes"
        " WHERE end_date IS NOT NULL"
        "   AND GREATEST(charge_energy_added, charge_energy_used) > $2"
        "   AND ($1::int IS NULL OR car_id = $1)"
        " ORDER BY id DESC",
        car_id, MIN_BILLED_KWH,
    )
    return [r["id"] for r in rows]


async def recompute_all(car_id: int | None = None, write_back: bool = False) -> dict:
    """按当前方案重算（全部或一辆车的）已结束场次，存入自有库；可选回写 TeslaMate。

    回写规则（保护用户数据）：
    - 方案还是"全零默认值"时一律不回写；
    - 只写 cost 为空的场次，或本工具此前写过、且库里仍是那个值的场次；
    - 时段没覆盖到的电量算不出价，跳过回写并在结果里报告。
    与自动补算共用一把锁：双击按钮时第二次会等第一次跑完再跑，结果一致（幂等）。
    """
    async with _pricing_lock:
        return await _recompute_locked(car_id, write_back)


async def _recompute_locked(car_id: int | None, write_back: bool) -> dict:
    plan = await get_plan()
    tz = plan_tz(plan)
    phash = plan_hash(plan)
    unconfigured = is_unconfigured(plan)
    if write_back and unconfigured:
        write_back = False
        log.warning("cost write-back skipped: tariff plan is still all-zero default")

    ids = await _billable_ids(car_id)
    registry = await written_registry() if write_back else {}
    total = 0.0
    written = unchanged = protected = uncovered_sessions = 0

    for i in range(0, len(ids), _BATCH):
        chunk = ids[i:i + _BATCH]
        results = await compute_sessions(chunk, plan, tz)
        results = {k: v for k, v in results.items() if not v.get("open")}
        await _save_costs(results, phash)
        for cid, res in results.items():
            total += res["cost"]
            if res["uncovered_kwh"] > 0.05:
                uncovered_sessions += 1
            if not write_back or res["uncovered_kwh"] > 0.05:
                continue
            try:
                outcome = await _write_back_one(cid, res["cost"], registry)
            except db.DbUnavailable:
                raise
            except Exception:
                log.exception("cost write-back failed for %s", cid)
                continue
            if outcome == "written":
                written += 1
            elif outcome == "unchanged":
                unchanged += 1
            else:
                protected += 1

    # 方案还是全零默认值时什么都没真正应用，别把"需要重算"的提示擦掉
    if not unconfigured:
        await store.set_setting("tariff_dirty", False)
    log.info(
        "recomputed %d charging costs (total %.2f, written=%d, unchanged=%d, protected=%d)",
        len(ids), total, written, unchanged, protected,
    )
    return {
        "sessions": len(ids),
        "total_cost": round(total, 2),
        "currency": plan.get("currency") or "USD",
        "written_back": written,
        "unchanged": unchanged,
        "protected": protected,
        "uncovered_sessions": uncovered_sessions,
        "unconfigured": unconfigured,
        "method": COST_METHOD,
        "warnings": validate(plan),
    }


async def price_missing(car_id: int | None = None, *, budget_s: float | None = 2.0,
                        write_back: bool = False) -> dict:
    """给还没定过价的已结束场次补算费用 —— 这样新场次不用等用户手点"重算"。

    读路径（洞察页、导出、MCP）调用它时只写 hub 自己的库（write_back=False）；
    后台定价任务调用时才会按 COST_WRITE_BACK 回写 TeslaMate。
    budget_s 限制一次最多花多少时间：老用户第一次打开可能有上千场要算，
    算不完的下次接着算，页面不会卡住。别人正在定价时直接跳过，不排队。
    """
    if _pricing_lock.locked():
        return {"busy": True, "priced": 0}
    async with _pricing_lock:
        if not await plan_saved():
            return {"skipped": "no_plan", "priced": 0}
        plan = await get_plan()
        tz = plan_tz(plan)
        phash = plan_hash(plan)
        if write_back and is_unconfigured(plan):
            write_back = False
        have = await stored_costs()
        todo = [cid for cid in await _billable_ids(car_id) if cid not in have]
        deadline = None if budget_s is None else _time.monotonic() + budget_s
        priced = written = 0
        registry = await written_registry() if write_back else {}
        for i in range(0, len(todo), _BATCH):
            if deadline is not None and _time.monotonic() > deadline:
                break
            results = await compute_sessions(todo[i:i + _BATCH], plan, tz)
            results = {k: v for k, v in results.items() if not v.get("open")}
            await _save_costs(results, phash)
            priced += len(results)
            if write_back:
                for cid, res in results.items():
                    if res["uncovered_kwh"] > 0.05:
                        continue
                    try:
                        if await _write_back_one(cid, res["cost"], registry) == "written":
                            written += 1
                    except db.DbUnavailable:
                        raise
                    except Exception:
                        log.exception("cost write-back failed for %s", cid)
        if priced:
            log.info("auto-priced %d charging session(s)%s", priced,
                     f", wrote back {written}" if written else "")
        return {"priced": priced, "remaining": max(len(todo) - priced, 0), "written_back": written}
