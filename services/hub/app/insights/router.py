import asyncpg
from fastapi import APIRouter, HTTPException

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


@router.get("/insights/charging")
async def charging(car_id: int):
    return await _guard(queries.charging)(car_id)


@router.get("/insights/drives")
async def drives(car_id: int):
    return await _guard(queries.drives)(car_id)


@router.get("/insights/tpms")
async def tpms(car_id: int):
    return await _guard(queries.tpms)(car_id)


@router.get("/insights/updates")
async def updates(car_id: int):
    return await _guard(queries.updates)(car_id)


@router.get("/insights/records")
async def records(car_id: int):
    return await _guard(queries.records)(car_id)


# ---------------- 行程 ----------------

@router.get("/drives")
async def drives(car_id: int, limit: int = 50):
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
