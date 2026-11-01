"""充电费用的**唯一真源**。

以前每个页面各算各的：Energy 页用"TeslaMate cost 为空就补电价引擎的值"，
统计/日历/时间线只 SUM(cost)（本机永远是 0），导出又是第三套 —— 同一个八月，
Energy 页 $28.95、统计页 $0.00（审计 F8）。现在所有读费用的地方都走这里。

一场充电的"有效费用"：
  ongoing    还没结束（end_date IS NULL）→ 不给数，也不定价
  recorded   TeslaMate 的 cost 列有值，且**不是** hub 写进去的（用户手填，
             或 TeslaMate 依地理围栏算的）→ 原样采用，永远不覆盖
  computed   hub 的电价引擎算的（含"回写过、库里仍是我们写的值"的场次；
             这类以 hub 最新算出的值为准，written_back=true）
  unset      已结束但还没有任何费用（没配电价方案）

读的时候会顺手给还没定价的已结束场次补算（有时间预算、只写 hub 自己的库），
所以新场次不再要等用户去设置页点"重算"。
"""

import asyncio
import contextlib
import logging

from .. import db
from ..tariff import calc

log = logging.getLogger(__name__)

SOURCES = ("recorded", "computed", "unset", "ongoing")


async def session_costs(car_id: int | None = None, *, autoprice: bool = True,
                        budget_s: float = 2.0) -> dict[int, dict]:
    """{cp_id: {cost, source, written_back, stale}}，覆盖该车（或全部车）的每一场。"""
    if autoprice:
        try:
            await calc.price_missing(car_id, budget_s=budget_s)
        except db.DbUnavailable:
            raise
        except Exception:  # 补算失败不该让读接口失败，按已有结果回答
            log.exception("auto-pricing failed")
    rows = await db.fetch(
        "SELECT id, cost::float8 AS cost, end_date IS NULL AS open"
        " FROM charging_processes WHERE ($1::int IS NULL OR car_id = $1)",
        car_id,
    )
    stored = await calc.stored_costs()
    registry = await calc.written_registry()
    phash = calc.plan_hash(await calc.get_plan())
    out: dict[int, dict] = {}
    for r in rows:
        cid = r["id"]
        mine = stored.get(cid)
        stale = bool(mine and mine.get("p") and mine.get("p") != phash)
        if r["open"]:
            out[cid] = {"cost": None, "source": "ongoing", "written_back": False, "stale": False}
        elif r["cost"] is not None and not calc.owns(r["cost"], registry.get(cid)):
            out[cid] = {"cost": round(float(r["cost"]), 2), "source": "recorded",
                        "written_back": False, "stale": False}
        elif mine is not None:
            out[cid] = {"cost": round(float(mine.get("c") or 0.0), 2), "source": "computed",
                        "written_back": r["cost"] is not None, "stale": stale}
        elif r["cost"] is not None:
            # 我们写进去的值，但 hub 这边的计算结果丢了（换了 hub.db）：照用库里的
            out[cid] = {"cost": round(float(r["cost"]), 2), "source": "computed",
                        "written_back": True, "stale": False}
        else:
            out[cid] = {"cost": None, "source": "unset", "written_back": False, "stale": False}
    return out


def total(costs: dict[int, dict], ids=None) -> float:
    keys = costs.keys() if ids is None else ids
    return round(sum((costs.get(i) or {}).get("cost") or 0.0 for i in keys), 2)


def count_by_source(costs: dict[int, dict], ids=None) -> dict[str, int]:
    keys = costs.keys() if ids is None else ids
    out = dict.fromkeys(SOURCES, 0)
    for i in keys:
        src = (costs.get(i) or {}).get("source", "unset")
        out[src] = out.get(src, 0) + 1
    return out


async def currency() -> str:
    """方案里的币种 > 地区偏好 > USD。"""
    plan = await calc.get_plan()
    if plan.get("currency"):
        return str(plan["currency"])
    try:
        from ..i18n.prefs import get as _get_prefs

        return (await _get_prefs()).get("currency") or "USD"
    except Exception:  # noqa: BLE001
        return "USD"


# ---------------------------------------------------------------- 后台定价

class AutoPricer:
    """后台定价循环：每隔一段时间给新结束的场次定价；开了 COST_WRITE_BACK 就同时回写
    TeslaMate（只填空白、或仍是我们写的值的场次）。

    读接口已经会顺手补算（只写 hub 库）；这个循环的意义是**回写**不依赖有人打开页面，
    Grafana 里的费用也能自己跟上。需要在 main.py 的 lifespan 里 start()/stop()
    （见 insights/NEEDS.md）。
    """

    def __init__(self, interval_s: float = 300.0) -> None:
        self.interval_s = interval_s
        self._task: asyncio.Task | None = None
        self.last: dict = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="tariff-autopricer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> dict:
        from ..config import settings

        self.last = await calc.price_missing(None, budget_s=None,
                                             write_back=bool(settings.cost_write_back))
        return self.last

    async def _run(self) -> None:
        await asyncio.sleep(30)          # 让 TeslaMate / 连接池先起来
        while True:
            try:
                await self.run_once()
            except db.DbUnavailable as e:
                log.info("auto-pricer: database unavailable (%s)", e)
            except Exception:
                log.exception("auto-pricer iteration failed")
            await asyncio.sleep(self.interval_s)


autopricer = AutoPricer()
