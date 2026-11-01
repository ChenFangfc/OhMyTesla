"""/api/analytics/* —— 与 TeslaMate Grafana 面板对齐的 JSON 接口。

挂在 insights 路由下（insights/router.py 里 include），不需要改 main.py。
全部接口：
- car_id 可省略：省略时取"最近活跃、未归档"的那辆车（响应里的 car 字段写明是哪辆）；
- 时间段统一用 ?days= / ?from= / ?to=（见 period.py）；
- 单位一律公制，前端换算；完整说明见 insights/API.md。
"""

from fastapi import APIRouter, HTTPException, Query

from . import analytics as A
from . import cars as car_registry
from .charging_stops import learned_schedule
from .guard import guard
from .period import clamp, parse_period

router = APIRouter(prefix="/analytics", tags=["analytics"])


async def _car(car_id: int | None) -> int:
    cid = await car_registry.resolve(car_id)
    if cid is None:
        raise HTTPException(404, "no vehicles in TeslaMate yet")
    return cid


@router.get("/cars")
@guard
async def cars():
    """全部车辆，含 archived / last_seen / mqtt_live，以及默认车。"""
    rows = await car_registry.list_cars()
    return {"cars": rows, "default_car_id": await car_registry.default_car_id()}


@router.get("/drives")
@guard
async def drives(car_id: int | None = None, days: int | None = None,
                 from_: str | None = Query(None, alias="from"), to: str | None = None,
                 limit: int = 100, offset: int = 0, min_distance_km: float = 0.0):
    p = parse_period(days, from_, to)
    return await A.drives_table(await _car(car_id), p, clamp(limit, 1, 1000, 100),
                                clamp(offset, 0, 10_000_000, 0), max(float(min_distance_km), 0.0))


@router.get("/drives/{drive_id}")
@guard
async def drive(drive_id: int, max_points: int = 2000):
    return await A.drive_detail(drive_id, clamp(max_points, 50, 10000, 2000))


@router.get("/speed-histogram")
@guard
async def speed_histogram(car_id: int | None = None, days: int | None = None,
                          from_: str | None = Query(None, alias="from"), to: str | None = None):
    cid = await _car(car_id)
    p = parse_period(days, from_, to, default_days=90)
    return {"car": await A.car_meta(cid), "period": p.as_dict(),
            "buckets": await A.speed_histogram(cid, p)}


@router.get("/charges")
@guard
async def charges(car_id: int | None = None, days: int | None = None,
                  from_: str | None = Query(None, alias="from"), to: str | None = None,
                  limit: int = 100, offset: int = 0):
    p = parse_period(days, from_, to)
    return await A.charges_table(await _car(car_id), p, clamp(limit, 1, 1000, 100),
                                 clamp(offset, 0, 10_000_000, 0))


@router.get("/charges/{cp_id}")
@guard
async def charge(cp_id: int, max_points: int = 800):
    return await A.charge_detail(cp_id, clamp(max_points, 50, 5000, 800))


@router.get("/charging-stats")
@guard
async def charging_stats(car_id: int | None = None, days: int | None = None,
                         from_: str | None = Query(None, alias="from"), to: str | None = None):
    return await A.charging_stats(await _car(car_id), parse_period(days, from_, to))


@router.get("/charging-schedule")
@guard
async def charging_schedule(car_id: int | None = None, days: int = 90):
    """学到的惯常开始/停止充电时刻（谷电窗口、定时出发）。"""
    cid = await _car(car_id)
    return {"car": await A.car_meta(cid), **(await learned_schedule(cid, clamp(days, 7, 3650, 90)))}


@router.get("/battery")
@guard
async def battery(car_id: int | None = None, trend: str = "month"):
    return await A.battery(await _car(car_id), trend)


@router.get("/efficiency")
@guard
async def efficiency(car_id: int | None = None, days: int | None = None,
                     from_: str | None = Query(None, alias="from"), to: str | None = None,
                     min_distance_km: float = 1.0):
    return await A.efficiency(await _car(car_id), parse_period(days, from_, to),
                              max(float(min_distance_km), 0.0))


@router.get("/vampire")
@guard
async def vampire(car_id: int | None = None, days: int | None = None,
                  from_: str | None = Query(None, alias="from"), to: str | None = None,
                  min_idle_h: float = 6.0):
    return await A.vampire(await _car(car_id), parse_period(days, from_, to),
                           min(max(float(min_idle_h), 0.0), 24 * 30))


@router.get("/states")
@guard
async def states(car_id: int | None = None, days: int | None = None,
                 from_: str | None = Query(None, alias="from"), to: str | None = None):
    return await A.states_timeline(await _car(car_id), parse_period(days, from_, to, default_days=7))


@router.get("/statistics")
@guard
async def statistics(car_id: int | None = None, period: str = "month", days: int | None = None,
                     from_: str | None = Query(None, alias="from"), to: str | None = None):
    return await A.statistics(await _car(car_id), parse_period(days, from_, to), period)


@router.get("/mileage")
@guard
async def mileage(car_id: int | None = None, bucket: str = "day", days: int | None = None,
                  from_: str | None = Query(None, alias="from"), to: str | None = None):
    return await A.mileage(await _car(car_id), parse_period(days, from_, to), bucket)


@router.get("/locations")
@guard
async def locations(car_id: int | None = None, days: int | None = None,
                    from_: str | None = Query(None, alias="from"), to: str | None = None,
                    limit: int = 20):
    return await A.locations(await _car(car_id), parse_period(days, from_, to),
                             clamp(limit, 1, 500, 20))


@router.get("/updates")
@guard
async def updates(car_id: int | None = None):
    return await A.updates(await _car(car_id))


@router.get("/charge-level")
@guard
async def charge_level(car_id: int | None = None, days: int | None = None,
                       from_: str | None = Query(None, alias="from"), to: str | None = None,
                       max_points: int = 600):
    return await A.charge_level(await _car(car_id), parse_period(days, from_, to, default_days=30),
                                clamp(max_points, 20, 5000, 600))


@router.get("/projected-range")
@guard
async def projected_range(car_id: int | None = None, bucket: str = "day", days: int | None = None,
                          from_: str | None = Query(None, alias="from"), to: str | None = None):
    cid = await _car(car_id)
    p = parse_period(days, from_, to)
    return {"car": await A.car_meta(cid), "period": p.as_dict(),
            **(await A.range_trend(cid, p, bucket))}


@router.get("/coverage")
@guard
async def coverage(car_id: int | None = None):
    """里程表跨度 vs 记录到的行程（数据完整性）。"""
    cid = await _car(car_id)
    return {"car": await A.car_meta(cid), **(await A.odometer_coverage(cid))}
