"""数据体检：找出采集缺口和不完整的记录。

为什么需要：这套系统是**沉默地**退化的。Nominatim 超时 → 地址是空的；
容器停了一晚上 → 那段时间没有任何数据；某次行程没落到 positions →
轨迹页点开是空的。这些都不会报错，只会让某个页面看起来"就是没东西"。

这里把它们一次列清楚，并明确区分「真的有问题」和「车在睡觉，正常」。
"""

import logging
from datetime import timezone

from fastapi import APIRouter, HTTPException

from .. import db
from ..insights.guard import http_error

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["health"])

# 采集缺口的判定阈值。车休眠时本来就没数据，而一整夜甚至一个周末不动
# 都很正常 —— 阈值定低了就会把"车主没开车"报成"采集出问题"。
# 36 小时意味着"整整一天半一条数据都没有"，那才值得看一眼。
GAP_HOURS = 36


def _iso(dt):
    return None if dt is None else dt.replace(tzinfo=timezone.utc).isoformat()


@router.get("/teslamate")
async def teslamate_schema(refresh: bool = False):
    """TeslaMate schema 守卫的结果。

    TeslaMate 4.3 起明确说「数据库不是接口」，schema 改名不会提前通知。hub 第一次
    连上数据库时会核对 schema_migrations 的最大版本和本 hub 读到的列：
      ok / older        照常工作
      newer             比验证过的 20261014120000（4.4.0）新：没人核对过，ERROR 日志
      incompatible      缺列：相关接口会以 503 tm_schema 失败并说明是哪一列
      not_ready         TeslaMate 首次启动、还没建表
      unreachable       连不上数据库
    `warning` 为 true 时界面应当显示横幅。
    """
    st = await db.check_teslamate_schema(force=bool(refresh))
    st["warning"] = st.get("status") in ("newer", "incompatible", "error")
    return st


@router.get("/data")
async def data_health(car_id: int | None = None, days: int = 90):
    """采集完整性体检。"""
    try:
        cars = await db.fetch("SELECT id, name, vin FROM cars ORDER BY id")
        if not cars:
            return {"cars": [], "note": "no vehicles yet"}
        # 没指定车时取"最近活跃、未归档"的那辆（不是 id 最小的那辆）
        from ..insights import cars as registry

        cid = car_id or await registry.default_car_id() or cars[0]["id"]
        d = max(int(days), 1)

        # 1) 采集缺口：相邻两条 position 之间隔了多久
        gaps = await db.fetch(
            """
            WITH p AS (
                SELECT date, LEAD(date) OVER (ORDER BY date) AS next_date
                FROM positions
                WHERE car_id = $1 AND date >= now() - ($2::int * INTERVAL '1 day')
            )
            SELECT date AS gap_start, next_date AS gap_end,
                   EXTRACT(EPOCH FROM (next_date - date)) / 3600.0 AS hours
            FROM p
            WHERE next_date IS NOT NULL
              AND next_date - date > ($3::int * INTERVAL '1 hour')
            ORDER BY date DESC LIMIT 50
            """,
            cid, d, GAP_HOURS,
        )

        # 2) 地址反查失败的（Nominatim 超时 / 国内被墙）
        no_addr = await db.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE start_address_id IS NULL) AS no_start,
                   COUNT(*) FILTER (WHERE end_address_id IS NULL) AS no_end,
                   COUNT(*) AS total
            FROM drives
            WHERE car_id = $1 AND start_date >= now() - ($2::int * INTERVAL '1 day')
            """,
            cid, d,
        )

        # 3) 有行程但没轨迹点 —— 行程页点开会是空地图
        no_track = await db.fetchval(
            """
            SELECT COUNT(*) FROM drives d
            WHERE d.car_id = $1 AND d.distance > 0.5
              AND d.start_date >= now() - ($2::int * INTERVAL '1 day')
              AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.drive_id = d.id)
            """,
            cid, d,
        )

        # 4) 真的没有费用的场次。cost 为空**不等于**没算出来 —— 电价引擎算的值
        #    存在 hub 自己的库里（回写默认是关的）。费用走唯一真源
        #    （insights/costs.py）：只有"已结束、TeslaMate 没记录、电价引擎也没算"
        #    的才算缺。读的时候会顺手补算，所以配好电价后这一项会自己清零。
        from ..insights import costs as cost_source

        costs = await cost_source.session_costs(cid)
        nocost_rows = await db.fetch(
            """
            SELECT id FROM charging_processes
            WHERE car_id = $1 AND end_date IS NOT NULL
              AND GREATEST(charge_energy_added, charge_energy_used) > 0.1
              AND start_date >= now() - ($2::int * INTERVAL '1 day')
            """,
            cid, d,
        )
        no_cost = sum(1 for r in nocost_rows
                      if (costs.get(r["id"]) or {}).get("source") == "unset")
        open_rows = await db.fetch(
            """
            SELECT 'drive' AS kind, id, start_date FROM drives
             WHERE car_id = $1 AND end_date IS NULL
            UNION ALL
            SELECT 'charge', id, start_date FROM charging_processes
             WHERE car_id = $1 AND end_date IS NULL
            """,
            cid,
        )

        # 5) 数据边界
        span = await db.fetchrow(
            "SELECT MIN(date) AS first, MAX(date) AS last, COUNT(*) AS n"
            " FROM positions WHERE car_id = $1",
            cid,
        )

        issues = []
        gap_list = [
            {"from": _iso(g["gap_start"]), "to": _iso(g["gap_end"]),
             "hours": round(float(g["hours"]), 1)}
            for g in gaps
        ]
        if gap_list:
            issues.append({"kind": "gaps", "count": len(gap_list),
                           "worst_hours": max(g["hours"] for g in gap_list)})
        if no_addr and (no_addr["no_start"] or no_addr["no_end"]):
            issues.append({"kind": "missing_addresses",
                           "count": int(no_addr["no_start"] or 0) + int(no_addr["no_end"] or 0)})
        if no_track:
            issues.append({"kind": "drives_without_track", "count": int(no_track)})
        if no_cost:
            issues.append({"kind": "charges_without_cost", "count": int(no_cost)})
        # 没关闭的行程/充电：正在进行的，或者 TeslaMate 中途重启留下的孤儿
        # （Database Information 面板同款）。超过 1 天还开着的基本就是孤儿。
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        now = _dt.now(timezone.utc).replace(tzinfo=None)
        orphans = [r for r in open_rows if now - r["start_date"] > _td(days=1)]
        if orphans:
            issues.append({"kind": "incomplete_records", "count": len(orphans),
                           "items": [{"kind": r["kind"], "id": r["id"],
                                      "start": _iso(r["start_date"])} for r in orphans[:20]]})
        schema = db.teslamate_schema()
        if schema.get("status") in ("newer", "incompatible"):
            issues.append({"kind": "teslamate_schema", "status": schema["status"],
                           "version": schema.get("version"),
                           "verified_version": db.VERIFIED_TM_SCHEMA})
        car_meta = await registry.get_car(cid)

        return {
            "car_id": cid,
            "days": d,
            "ok": not issues,
            "issues": issues,
            "gaps": gap_list[:20],
            "gap_threshold_hours": GAP_HOURS,
            "coverage": {
                "first": _iso(span["first"]) if span else None,
                "last": _iso(span["last"]) if span else None,
                "positions": int(span["n"] or 0) if span else 0,
            },
            "drives_total": int(no_addr["total"] or 0) if no_addr else 0,
            "archived": bool(car_meta and car_meta["archived"]),
            "last_seen": car_meta["last_seen"] if car_meta else None,
            "teslamate_schema": {k: schema.get(k) for k in ("status", "version", "verified_version")},
        }
    except HTTPException:
        raise
    except Exception as e:  # 交给 http_error 分类，不认识的照常冒泡
        err = http_error(e)
        if err is None:
            raise
        raise err from e
