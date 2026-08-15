import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from . import queries

router = APIRouter(prefix="/api", tags=["insights"])


def _guard(fn):
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except db.DbUnavailable as e:
            raise HTTPException(
                503, f"TeslaMate 数据库暂不可用（首次启动需等 TeslaMate 完成初始化）: {e}"
            ) from e
        except asyncpg.PostgresError as e:
            # 首次启动时 TeslaMate 还没跑完建表迁移，表不存在也归为"暂不可用"
            raise HTTPException(
                503, f"TeslaMate 数据库尚未就绪（可能仍在初始化建表）: {e}"
            ) from e

    return wrapper


@router.get("/cars")
async def cars():
    return await _guard(queries.cars)()


@router.get("/insights/summary")
async def summary(car_id: int):
    return await _guard(queries.summary)(car_id)


@router.get("/insights/battery")
async def battery(car_id: int):
    return await _guard(queries.battery_health)(car_id)


# days: 时间窗，0/省略 = 全部。Energy 页那排 30/90/365/全部 靠它生效
# —— 之前那排按钮只改了个 JS 变量，接口根本不收这个参数，点了等于没点。
@router.get("/insights/charging")
async def charging(car_id: int, days: int = 0):
    return await _guard(queries.charging)(car_id, days)


@router.get("/insights/drives")
async def drives(car_id: int, days: int = 0):
    return await _guard(queries.drives)(car_id, days)


@router.get("/insights/tpms")
async def tpms(car_id: int):
    return await _guard(queries.tpms)(car_id)


@router.get("/insights/updates")
async def updates(car_id: int):
    return await _guard(queries.updates)(car_id)


@router.get("/insights/records")
async def records(car_id: int):
    return await _guard(queries.records)(car_id)


@router.get("/insights/compare")
async def compare(car_id: int, days: int = 30):
    """本期 vs 上一个等长周期，带同比百分比。"""
    return await _guard(queries.period_compare)(car_id, days)


class IceBaseline(BaseModel):
    l_per_100km: float | None = None
    fuel_price: float | None = None


@router.post("/insights/ice-baseline")
async def set_ice_baseline(body: IceBaseline):
    """油车对比基准（百公里油耗 + 油价）。不填就不显示对比，不替用户编数字。"""
    from ..alerts.store import store

    if not body.l_per_100km or not body.fuel_price:
        await store.set_setting("ice_baseline", {})
        return {"ok": True, "baseline": None}
    b = {"l_per_100km": round(float(body.l_per_100km), 2),
         "fuel_price": round(float(body.fuel_price), 3)}
    await store.set_setting("ice_baseline", b)
    return {"ok": True, "baseline": b}


@router.get("/insights/real-range")
async def real_range(car_id: int, window_km: float = 160.0):
    """按你自己最近的真实能耗算的续航，而不是 EPA 标称值。"""
    return await _guard(queries.real_world_range)(car_id, window_km)


class BatteryBaseline(BaseModel):
    range_km: float | None = None
    kwh: float | None = None


@router.post("/insights/battery/baseline")
async def set_battery_baseline(car_id: int, body: BatteryBaseline):
    """手填"新车时的容量/续航"。

    默认基准是"观测到的最大值"，这对装本工具之前已经开了几年的车是偏袒的
    —— 那几年的衰减全被吃进基准，结果显示 0% 衰减。
    """
    from ..alerts.store import store

    cur = await store.get_setting("battery_baseline", {}) or {}
    if not body.range_km and not body.kwh:
        cur.pop(str(car_id), None)          # 清空 = 回到观测最大值
    else:
        entry = {}
        if body.range_km and body.range_km > 0:
            entry["range_km"] = round(float(body.range_km), 1)
        if body.kwh and body.kwh > 0:
            entry["kwh"] = round(float(body.kwh), 1)
        cur[str(car_id)] = entry
    await store.set_setting("battery_baseline", cur)
    return {"ok": True, "baseline": cur.get(str(car_id))}


@router.get("/insights/vampire")
async def vampire(car_id: int, days: int = 90):
    """停放耗电。每家记录器都有这张表，因为"车停着掉多少电"是最常问的问题之一。"""
    return await _guard(queries.vampire_drain)(car_id, days)


@router.get("/insights/charge-curve")
async def charge_curve(cp_id: int):
    """单次充电的功率-电量曲线。"""
    return await _guard(queries.charge_curve)(cp_id)


# ---------------- 行程 ----------------

# 注意：不要叫 drives —— 上面 /insights/drives 已经用了这个名字，
# 重名会把前一个函数从模块命名空间里挤掉（路由仍然工作，因为 FastAPI
# 在装饰时就抓住了函数对象，但任何按名字引用的地方都会拿到错的那个）。
@router.get("/drives")
async def drives_list(car_id: int, limit: int = 50):
    return await _guard(queries.drives_list)(car_id, limit)


@router.get("/drives/tracks")
async def drives_tracks(car_id: int, limit: int = 20):
    """最近行程的轨迹合集，供行程页落地时直接铺开。"""
    return await _guard(queries.drive_tracks)(car_id, limit)


@router.get("/drives/{drive_id}/track")
async def drive_track(drive_id: int):
    return await _guard(queries.drive_track)(drive_id)


@router.get("/drives/{drive_id}/footage")
async def drive_footage(drive_id: int):
    """匹配该行程时间段内的行车记录仪事件（含跳播偏移秒数）。"""
    from datetime import timedelta, timezone

    from ..dashcam import telemetry
    from ..dashcam.scanner import scanner

    row = await _guard(queries.drive_row)(drive_id)
    if row is None:
        raise HTTPException(404, "drive not found")

    offset = await telemetry.clock_offset(row["car_id"])
    tz = telemetry.local_tz()
    # UTC(naive) → 车机本地 naive，再减去时钟偏移得到文件名时间轴
    def to_cam_local(dt):
        return (
            dt.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)
            - timedelta(seconds=offset)
        )

    d_start = to_cam_local(row["start_date"])
    d_end = to_cam_local(row["end_date"]) if row["end_date"] else d_start + timedelta(hours=2)

    matches = []
    for ev in scanner.scan():
        if ev.end <= d_start or ev.start >= d_end:
            continue
        # seek 必须映射到播放器的"连续视频时间轴"（分段索引×60+段内偏移），
        # 分段可能有间隙，直接用墙钟差会指错位置
        seek = 0.0
        for i, seg in enumerate(ev.segments):
            if d_start < seg.start:
                seek = i * 60.0
                break
            within = (d_start - seg.start).total_seconds()
            if within < 60:
                seek = i * 60.0 + within
                break
            seek = (i + 1) * 60.0
        matches.append({
            "event_id": ev.id,
            "type": ev.type,
            "event_start": ev.start.isoformat(),
            "seek": round(seek),
            "thumb": ev.thumb,
        })
    matches.sort(key=lambda m: m["event_start"])
    return {"drive_id": drive_id, "footage": matches}
