"""电价引擎的纯计算部分：计费电量口径、分时切分、计数器回落、免费超充、方案归一化。

这些都是审计 data-api F3 / F17 / F18 里实测出来的错：
- 计费电量用的是电池端（charge_energy_added），家充少算 14.7%；
- 计数器回落再爬升时把爬回来那段算了两遍（cp 9 多算 2.99 kWh）；
- `locations: null` 让预览/重算 500；
- 方案级"免费超充"把第三方直流快充也清零了。

跑法：python3 services/hub/tests/test_data_tariff.py（需要 hub 的运行时依赖，
宿主机没有就跳过；容器里：run.sh --docker）
"""

import os
import tempfile
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="omt-test-data-")   # never the real /data
try:
    from app.tariff import calc as C
except ImportError as e:  # 宿主机没装 fastapi/asyncpg/aiosqlite
    print(f"⏭  skipped: needs the hub runtime ({e}); run with tests/run.sh --docker")
    sys.exit(0)

FAILS: list[str] = []
CHI = ZoneInfo("America/Chicago")


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def utc(local: str) -> datetime:
    """芝加哥本地时间 → naive UTC（TeslaMate 的存储格式）。"""
    return (datetime.fromisoformat(local).replace(tzinfo=CHI)
            .astimezone(ZoneInfo("UTC")).replace(tzinfo=None))


def head(**kw):
    h = {"id": 1, "start_date": utc("2026-08-22 21:05"), "added": 87.26, "used": 104.83,
         "location": "Home", "is_dc": False, "charger_type": None, "charger_brand": None,
         "geofence": None, "addr_name": None, "free_sc_car": False}
    h.update(kw)
    return h


def even(start_local: str, hours: float, kw: float = 10.0, step_min: int = 10):
    """恒功率充电的采样区间 [(t_prev, t, kwh_wall, d_added)]。"""
    t0 = utc(start_local)
    out = []
    n = int(hours * 60 / step_min)
    for i in range(n):
        a = t0 + timedelta(minutes=step_min * i)
        b = a + timedelta(minutes=step_min)
        out.append((a, b, kw * step_min / 60, kw * step_min / 60 * 0.9))
    return out


FLAT = {"default": {"type": "flat", "rate": 0.12}, "locations": {},
        "supercharger": {"type": "flat", "rate": 0.36}}
TOU = {"default": {"type": "tou", "periods": [
    {"from": "22:00", "to": "06:00", "rate": 0.10, "name": "valley"},
    {"from": "06:00", "to": "22:00", "rate": 0.30, "name": "peak"}]},
    "locations": {}, "supercharger": {"type": "flat", "rate": 0}}


def main() -> int:
    print("计费电量 = max(电池端, 墙端)，忽略 NULL")
    chk("墙端更大取墙端", C.billed_kwh(87.26, 104.83) == 104.83)
    chk("电池端为空取墙端（cp 3：added 0.00 / used 5.83）", C.billed_kwh(None, 5.83) == 5.83)
    chk("墙端为空取电池端", C.billed_kwh(21.74, None) == 21.74)
    chk("都为空是 0", C.billed_kwh(None, None) == 0.0)

    print("单一电价与 TeslaMate put_cost 逐分一致")
    r = C.price(head(), None, FLAT, CHI)
    chk("cp 9：104.83 kWh × 0.12 = 12.58", r["cost"] == 12.58 and r["kwh"] == 104.83, r["cost"])
    chk("返回电池端/墙端/计费三个电量", (r["kwh_added"], r["kwh_used"], r["kwh_billed"]) == (87.26, 104.83, 104.83))
    r = C.price(head(is_dc=True, added=31.81, used=32.82), None, FLAT, CHI)
    chk("直流走 supercharger 费率（cp 17：32.82 × 0.36 = 11.82）", r["cost"] == 11.82, r["cost"])

    print("分时电价：按墙端电量分摊、跨边界按时间切")
    iv = even("2026-08-22 22:30", 6)                         # 全在谷时段
    r = C.price(head(added=54.0, used=60.0), iv, TOU, CHI)
    chk("全在谷时段 = 60 × 0.10", abs(r["cost"] - 6.0) < 0.01, r)
    iv = even("2026-08-22 20:00", 4)                         # 20-22 峰，22-24 谷
    r = C.price(head(added=36.0, used=40.0), iv, TOU, CHI)
    chk("跨 22:00 一半峰一半谷 = 40 × 0.20", abs(r["cost"] - 8.0) < 0.01, r["breakdown"])
    chk("分时明细电量守恒（= 计费电量）",
        abs(sum(b["kwh"] for b in r["breakdown"].values()) - 40.0) < 0.02)
    a = utc("2026-08-22 21:00")
    iv = [(a, a + timedelta(hours=1), 10.0, 9.0),            # 峰时段 10 kWh
          (a + timedelta(hours=1), a + timedelta(hours=2), 2.0, 1.8)]   # 谷时段 2 kWh
    r = C.price(head(added=10.8, used=12.0), iv, TOU, CHI)
    want = 12.0 * (10 / 12 * 0.30 + 2 / 12 * 0.10)
    chk("权重是墙端功率×时长，不是时间均分", abs(r["cost"] - want) < 0.01, (r["cost"], round(want, 2)))

    print("计数器回落不重复计费（cp 9 曾多算 2.99 kWh）")
    a = utc("2026-08-22 23:00")
    dip = [(a, a + timedelta(minutes=10), 0.0, 10.0),
           (a + timedelta(minutes=10), a + timedelta(minutes=20), 0.0, -3.0),
           (a + timedelta(minutes=20), a + timedelta(minutes=30), 0.0, 3.0),
           (a + timedelta(minutes=30), a + timedelta(minutes=40), 0.0, 5.0)]
    r = C.price(head(added=15.0, used=None), dip, TOU, CHI)
    chk("没有功率数据时退回电池端正增量做权重，总量仍是计费电量",
        abs(sum(b["kwh"] for b in r["breakdown"].values()) - 15.0) < 0.01, r["breakdown"])
    chk("费用 = 15 × 谷价", abs(r["cost"] - 1.5) < 0.01, r["cost"])

    print("时段没盖满")
    gap = {"default": {"type": "tou", "periods": [{"from": "00:00", "to": "06:00", "rate": 0.1}]}}
    r = C.price(head(added=9.0, used=10.0), even("2026-08-22 05:00", 2), gap, CHI)
    chk("没覆盖的电量单独报出", r["uncovered_kwh"] > 4.9, r["uncovered_kwh"])

    print("免费超充只对特斯拉超充（TeslaMate：fast_charger_type 以 Tesla 开头）")
    plan = {**FLAT, "free_supercharging": True}
    r = C.price(head(is_dc=True, charger_type="Tesla", added=30, used=31), None, plan, CHI)
    chk("特斯拉超充 → 0", r["cost"] == 0 and r["free_supercharging"], r)
    r = C.price(head(is_dc=True, charger_type="CCS", charger_brand="Electrify America",
                     added=30, used=31), None, plan, CHI)
    chk("第三方直流快充照常计费", r["cost"] == round(31 * 0.36, 2), r["cost"])
    r = C.price(head(is_dc=True, charger_brand="Tesla", free_sc_car=True, added=30, used=31),
                None, FLAT, CHI)
    chk("车辆级 free_supercharging（car_settings）也生效", r["cost"] == 0)
    r = C.price(head(is_dc=False, free_sc_car=True), None, FLAT, CHI)
    chk("交流不受免费超充影响", r["cost"] == 12.58)

    print("方案归一化：null 的地点不再让读路径 500")
    p = C.normalize_plan({"default": {"type": "flat", "rate": 0.2}, "locations": None})
    chk("locations: null → {}", p["locations"] == {})
    p = C.normalize_plan({"default": {"type": "flat", "rate": 0.2},
                          "locations": {"Home": None, "Work": {"type": "flat", "rate": 0}}})
    chk("值为 null 的地点被去掉", list(p["locations"]) == ["Work"])
    chk("_pick_rule 遇到 null 地点退回默认",
        C._pick_rule({"default": {"type": "flat", "rate": 0.2}, "locations": {"Home": None}},
                     "Home", False)["rate"] == 0.2)
    chk("validate_plan 拦住坏时区", any("timezone" in e for e in C.validate_plan(
        {"default": {"type": "flat", "rate": 0.1}, "timezone": "Mars/Base"})))
    chk("validate_plan 拦住负费率", bool(C.validate_plan({"default": {"type": "flat", "rate": -1}})))

    print("方案指纹")
    h1 = C.plan_hash({**FLAT, "currency": "USD"})
    chk("换币种不改指纹", h1 == C.plan_hash({**FLAT, "currency": "EUR"}))
    chk("改费率改指纹", h1 != C.plan_hash({**FLAT, "default": {"type": "flat", "rate": 0.13}}))

    print("时段切分守恒（跨午夜、跨夏令时）")
    rule = TOU["default"]
    ok = True
    for start, hours in (("2026-03-07 20:00", 30), ("2026-11-01 00:30", 5), ("2026-08-22 05:59", 0.05)):
        t0 = datetime.fromisoformat(start).replace(tzinfo=CHI)
        parts = C._split_interval(rule, t0, t0 + timedelta(hours=hours))
        ok &= abs(sum(f for f, _, _ in parts) - 1.0) < 1e-9
    chk("各段占比之和恒为 1", ok)

    print()
    print("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
