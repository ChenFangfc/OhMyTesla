"""费用重算 / 自动定价 / 回写的并发与"绝不覆盖手填费用"。

用假的 TeslaMate 库和假的 hub 库跑**真的** tariff.calc 与 insights.costs 代码。
覆盖审计 data-api F4 / F5 的复现路径：
- 双击"重算"（两次 recompute_all 并发）以后，登记表被冲掉、之后所有回写过的场次
  都被当成"用户手填"，费用永久冻结在旧价格 —— 实测 1.2 而不是 2.0；
- 新结束的场次只有手点"重算"才有费用；
- 进行中的场次（end_date IS NULL）不能定价、不能回写；
- 用户手填的费用永远不被覆盖；
- 回写之后、登记之前进程被杀，下次不能把自己写的值当成"手填"。

跑法：python3 services/hub/tests/test_data_recompute.py（需要 hub 运行时依赖）
"""

import asyncio
import os
import tempfile
import sys

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="omt-test-data-")   # never the real /data
try:
    from app.insights import costs as K
    from app.tariff import calc as C
except ImportError as e:
    print(f"⏭  skipped: needs the hub runtime ({e}); run with tests/run.sh --docker")
    sys.exit(0)

FAILS: list[str] = []


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


class FakeStore:
    """hub 自有库：get/set 之间让出事件循环，复现读-改-写竞争。"""

    def __init__(self):
        self.s: dict = {}
        self.locks: dict = {}

    async def get_setting(self, k, d=None):
        await asyncio.sleep(0)
        v = self.s.get(k, d)
        return __import__("copy").deepcopy(v)

    async def set_setting(self, k, v):
        await asyncio.sleep(0)
        self.s[k] = __import__("copy").deepcopy(v)

    async def mutate_setting(self, k, fn, default=None):
        lock = self.locks.setdefault(k, asyncio.Lock())
        async with lock:
            cur = await self.get_setting(k, default)
            new = fn(cur)
            await self.set_setting(k, new)
            return new


class FakeDB:
    """TeslaMate 的 charging_processes：id → {cost, open}。"""

    def __init__(self, n=10):
        self.cp = {i: {"cost": None, "open": False} for i in range(1, n + 1)}
        self.writes = 0

    async def fetch(self, q, *a, timeout_s=None):
        await asyncio.sleep(0)
        if "SELECT id FROM charging_processes" in q:     # _billable_ids
            return [{"id": i} for i in sorted(self.cp, reverse=True) if not self.cp[i]["open"]]
        if "end_date IS NULL AS open" in q:              # costs.session_costs
            return [{"id": i, "cost": v["cost"], "open": v["open"]} for i, v in self.cp.items()]
        raise AssertionError(f"unexpected fetch: {q[:60]}")

    async def execute(self, q, *a, timeout_s=None):
        await asyncio.sleep(0)
        new, cid = a[0], a[1]
        row = self.cp[cid]
        if row["open"]:
            return "UPDATE 0"
        if "abs(cost" in q:
            ok = row["cost"] is None or (abs(row["cost"] - a[2]) < 0.005
                                         and abs(row["cost"] - new) >= 0.005)
        else:
            ok = row["cost"] is None
        if ok:
            row["cost"] = new
            self.writes += 1
        return "UPDATE 1" if ok else "UPDATE 0"

    async def fetchval(self, q, *a, timeout_s=None):
        await asyncio.sleep(0)
        return self.cp[a[0]]["cost"]


async def fake_compute(ids, plan, tz):
    out = {}
    for cid in ids:
        await asyncio.sleep(0)
        rate = plan["default"]["rate"]
        out[cid] = {"id": cid, "cost": round(10 * rate, 2), "kwh": 10.0, "uncovered_kwh": 0.0,
                    "open": False, "car_id": 1}
    return out


def setup(rate=0.12, n=10):
    st, fdb = FakeStore(), FakeDB(n)
    st.s["tariff_plan"] = {"default": {"type": "flat", "rate": rate}}
    C.store = st
    C.db.fetch, C.db.execute, C.db.fetchval = fdb.fetch, fdb.execute, fdb.fetchval
    C.compute_sessions = fake_compute
    return st, fdb


async def main() -> int:
    print("双击重算（两次并发）")
    st, fdb = setup(0.12)
    a, b = await asyncio.gather(C.recompute_all(None, write_back=True),
                                C.recompute_all(None, write_back=True))
    reg = st.s.get(C.REGISTRY_KEY, {})
    chk("第一次写了 10 场", a["written_back"] == 10, a)
    chk("第二次是幂等的：0 写入、10 场不变、0 场被当成手填",
        b["written_back"] == 0 and b["unchanged"] == 10 and b["protected"] == 0, b)
    chk("登记表完整（10 场都认得是自己写的）", len(reg) == 10, reg)

    print("改电价后重算：我们写的值全部跟着更新")
    st.s["tariff_plan"] = {"default": {"type": "flat", "rate": 0.20}}
    c = await C.recompute_all(None, write_back=True)
    chk("10 场更新为 2.0（审计复现里是冻在 1.2）",
        all(v["cost"] == 2.0 for v in fdb.cp.values()) and c["protected"] == 0, (c, fdb.cp[1]))

    print("用户手填的费用绝不覆盖")
    fdb.cp[3]["cost"] = 5.55
    st.s["tariff_plan"] = {"default": {"type": "flat", "rate": 0.30}}
    d = await C.recompute_all(None, write_back=True)
    chk("手填的 5.55 原样保留", fdb.cp[3]["cost"] == 5.55, fdb.cp[3])
    chk("其余 9 场更新为 3.0", all(fdb.cp[i]["cost"] == 3.0 for i in fdb.cp if i != 3))
    chk("被保护的那场从登记表里除名",
        "3" not in st.s[C.REGISTRY_KEY] and d["protected"] == 1, d)
    costs = await K.session_costs(1, autoprice=False)
    chk("有效费用：手填的那场标 recorded、值是 5.55",
        costs[3] == {"cost": 5.55, "source": "recorded", "written_back": False, "stale": False},
        costs[3])
    chk("我们写回的那些标 computed + written_back",
        costs[1]["source"] == "computed" and costs[1]["written_back"] and costs[1]["cost"] == 3.0,
        costs[1])

    print("回写之后、登记之前进程被杀")
    st.s[C.REGISTRY_KEY]["4"] = 2.0          # 登记表停在上一次的值，库里已经是 3.0
    e = await C.recompute_all(None, write_back=True)
    chk("库里的值等于这次算出的值 → 认作自己写的（unchanged），而不是手填",
        st.s[C.REGISTRY_KEY].get("4") == 3.0 and e["protected"] == 1, (e, st.s[C.REGISTRY_KEY].get("4")))

    print("进行中的场次")
    st, fdb = setup(0.12, n=3)
    fdb.cp[2]["open"] = True
    await C.recompute_all(None, write_back=True)
    chk("end_date IS NULL 的场次不定价、不回写", fdb.cp[2]["cost"] is None
        and "2" not in (st.s.get(C.COSTS_KEY, {}).get("costs", {})))
    costs = await K.session_costs(None, autoprice=False)
    chk("有效费用标 ongoing", costs[2]["source"] == "ongoing" and costs[2]["cost"] is None, costs[2])

    print("方案还是全零默认值时不回写")
    st, fdb = setup(0.0, n=3)
    f = await C.recompute_all(None, write_back=True)
    chk("一条都不写", fdb.writes == 0 and f["unconfigured"], f)

    print("自动定价（新场次不用等手点重算）")
    st, fdb = setup(0.12, n=4)
    r = await C.price_missing(None, budget_s=5)
    chk("补算了 4 场，只写 hub 自己的库", r["priced"] == 4 and fdb.writes == 0, r)
    r = await C.price_missing(None, budget_s=5)
    chk("再调一次什么都不做", r["priced"] == 0, r)
    fdb.cp[5] = {"cost": None, "open": False}
    costs = await K.session_costs(None)               # 读接口会顺手补算
    chk("新结束的场次在读的时候被自动定价", costs[5]["source"] == "computed" and costs[5]["cost"] == 1.2,
        costs[5])
    st2, fdb2 = setup(0.12, n=2)
    del st2.s["tariff_plan"]
    r = await C.price_missing(None)
    chk("没保存过电价方案就不自动定价（全零算出来的 0 元是假数据）", r.get("skipped") == "no_plan", r)
    costs = await K.session_costs(None, autoprice=False)
    chk("此时费用来源是 unset", all(v["source"] == "unset" for v in costs.values()))

    print("自动定价与重算互斥")
    st, fdb = setup(0.12, n=3)
    async with C._pricing_lock:
        r = await C.price_missing(None)
    chk("正在重算时自动定价直接跳过，不排队", r.get("busy") is True, r)
    w = await C.price_missing(None, budget_s=None, write_back=True)
    chk("后台定价开了回写时只填空白场次", w["written_back"] == 3 and fdb.writes == 3, w)

    print("老算法（电池端电量）的结果作废")
    st.s[C.COSTS_KEY] = {"method": 1, "costs": {"1": {"c": 9.99}}}
    chk("method != 2 的存量不再被读出", await C.stored_costs() == {})

    print()
    print("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
