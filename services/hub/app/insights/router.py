from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import journey, queries
from .analytics_api import router as analytics_router
from .guard import guard as _guard_deco
from .period import clamp

router = APIRouter(prefix="/api", tags=["insights"])
# Grafana 面板对齐的新接口（/api/analytics/*）。挂在这里是因为 main.py 只 include
# 了 insights 这个路由；子路由在 include 之前合并进来即可。
router.include_router(analytics_router)


def _guard(fn):
    """数据库异常 → 准确的 HTTP 错误（503 连不上 / 504 超时 / 503 schema / 500 SQL）。

    以前一律报"数据库尚未就绪（可能仍在初始化建表）"——连 LIMIT 传负数都是这句。
    """
    return _guard_deco(fn)


@router.get("/cars")
async def cars():
    """车辆列表。每辆车带 archived / last_seen / mqtt_live（见 insights/cars.py）。"""
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

    # 整块 JSON 的读-改-写必须持锁（两辆车同时改基准时，后写的会冲掉先写的）
    def apply(cur):
        cur = dict(cur or {})
        if not body.range_km and not body.kwh:
            cur.pop(str(car_id), None)          # 清空 = 回到观测最大值
        else:
            entry = {}
            if body.range_km and body.range_km > 0:
                entry["range_km"] = round(float(body.range_km), 1)
            if body.kwh and body.kwh > 0:
                entry["kwh"] = round(float(body.kwh), 1)
            cur[str(car_id)] = entry
        return cur

    cur = await store.mutate_setting("battery_baseline", apply, {})
    return {"ok": True, "baseline": cur.get(str(car_id))}


@router.get("/insights/vampire")
async def vampire(car_id: int, days: int = 90, min_idle_h: float = 6.0):
    """停放耗电。每家记录器都有这张表，因为"车停着掉多少电"是最常问的问题之一。

    min_idle_h 与 TeslaMate 自家面板的 "min. Idle Time (h)" 同义，默认同为 6。
    调小能看到刚停车那段的高耗电，但那个数字不能当"每天掉多少"用。
    """
    return await _guard(queries.vampire_drain)(car_id, days, min_idle_h)


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
    return await _guard(queries.drives_list)(car_id, clamp(limit, 1, 10000, 50))


@router.get("/drives/tracks")
async def drives_tracks(car_id: int, limit: int = 20):
    """最近行程的轨迹合集，供行程页落地时直接铺开。

    limit 夹到 1..200：负数以前会变成 SQL 错误，再被翻译成"数据库尚未就绪"的 503。
    """
    return await _guard(queries.drive_tracks)(car_id, clamp(limit, 1, 200, 20))


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
    for ev in await scanner.ascan():
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
            # The player maps `at` (car-local wall clock) onto its real timeline;
            # `seek` assumes 60 s segments and is kept for older clients.
            "at": max(d_start, ev.start).isoformat(timespec="seconds"),
            "seek": round(seek),
            "thumb": ev.thumb,
        })
    matches.sort(key=lambda m: m["event_start"])
    return {"drive_id": drive_id, "footage": matches}


# ---------------- 时间维度与地理维度 ----------------

@router.get("/insights/timeline")
async def timeline(car_id: int, days: int = 30, limit: int = 300):
    """统一时间线：行程 / 充电 / 更新 / 停放按时间倒序排在一条轴上。"""
    return await _guard(journey.timeline)(car_id, days, limit)


@router.get("/insights/calendar")
async def calendar(car_id: int, days: int = 365):
    """按本地日分桶的活动强度，供月历热力图使用。"""
    return await _guard(journey.calendar)(car_id, days)


@router.get("/insights/visited")
async def visited(car_id: int):
    """生涯足迹：去过的国家 / 省州 / 城市。"""
    return await _guard(journey.visited)(car_id)


@router.get("/insights/places")
async def places(car_id: int, limit: int = 30):
    """常去地点排行：到访次数 + 累计停留时长。"""
    return await _guard(journey.places)(car_id, limit)


@router.get("/insights/statistics")
async def statistics(car_id: int, bucket: str = "month", limit: int = 36):
    """周期汇总。bucket 取 day/week/month/year，非法值回落到 month。"""
    return await _guard(journey.statistics)(car_id, bucket, limit)


@router.get("/insights/states")
async def states(car_id: int, days: int = 30):
    """在线 / 睡眠 / 离线占比与睡眠评分。"""
    return await _guard(journey.states_breakdown)(car_id, days)


@router.get("/insights/coverage")
async def coverage(car_id: int):
    """未记录里程与采集覆盖率。"""
    return await _guard(journey.unlogged_km)(car_id)


@router.get("/drives/{drive_id}/detail")
async def drive_detail(drive_id: int):
    """单次行程的完整档案（元数据；逐点序列走 /drives/{id}/track）。"""
    d = await _guard(journey.drive_detail)(drive_id)
    if not d:
        raise HTTPException(404, "drive not found")
    return d
