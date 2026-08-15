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

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["health"])

# 采集缺口的判定阈值。车休眠时本来就没数据，而一整夜甚至一个周末不动
# 都很正常 —— 阈值定低了就会把"车主没开车"报成"采集出问题"。
# 36 小时意味着"整整一天半一条数据都没有"，那才值得看一眼。
GAP_HOURS = 36


def _iso(dt):
    return None if dt is None else dt.replace(tzinfo=timezone.utc).isoformat()


@router.get("/data")
async def data_health(car_id: int | None = None, days: int = 90):
    """采集完整性体检。"""
    try:
        cars = await db.fetch("SELECT id, name, vin FROM cars ORDER BY id")
        if not cars:
            return {"cars": [], "note": "no vehicles yet"}
        cid = car_id or cars[0]["id"]
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
        #    存在 hub 自己的库里（回写默认是关的）。只看 cost IS NULL 会把
        #    "已经算好了，只是没写回 TeslaMate" 报成问题。
        from ..tariff.calc import costs_map

        computed = await costs_map()
        nocost_rows = await db.fetch(
            """
            SELECT id FROM charging_processes
            WHERE car_id = $1 AND charge_energy_added > 0.1 AND cost IS NULL
              AND start_date >= now() - ($2::int * INTERVAL '1 day')
            """,
            cid, d,
        )
        no_cost = sum(1 for r in nocost_rows if str(r["id"]) not in computed)

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
        }
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
