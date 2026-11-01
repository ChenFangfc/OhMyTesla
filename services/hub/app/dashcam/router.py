import asyncio
import contextlib
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from .. import db
from ..config import settings
from . import align, ff, sei, telemetry
from .composer import CAMERA_NAMES_ZH, CAMERAS
from .jobs import get_reports, jobs
from .scanner import fill_durations, scanner
from .teslausb import router as teslausb_router

router = APIRouter(prefix="/api/dashcam", tags=["dashcam"])
router.include_router(teslausb_router)


@router.get("/status")
async def status():
    from .localtime import describe

    events = await scanner.ascan()
    base = scanner.base_path()
    root = Path(settings.teslacam_root)

    def _root_state() -> tuple[bool, bool]:
        exists = root.exists()
        return exists, exists and not any(root.iterdir())

    root_exists, root_empty = await asyncio.to_thread(_root_state)
    stats = scanner.stats or {}
    # "目录不存在" 和 "目录在但还没放东西" 是两回事，混为一谈会让人以为坏了
    return {
        "root": settings.teslacam_root,
        "found": base is not None,
        "base": str(base) if base else None,
        "root_exists": root_exists,
        "root_empty": root_empty,
        "events": len(events),
        "by_type": {
            t: sum(1 for e in events if e.type == t)
            for t in ("sentry", "saved", "recent")
        },
        "camera_names": CAMERA_NAMES_ZH,
        # 文件名按哪个时区解释、这个时区是怎么来的 —— 排查"整体差一小时"用
        "time_zone": describe(),
        # 扫描时跳过的条目（坏文件名、读不了的目录）和被挡住的另一种布局
        "scan": {"skipped": stats.get("skipped", 0),
                 "skipped_items": stats.get("skipped_items", [])[:10],
                 "shadowed_root": stats.get("shadowed_root"),
                 "seconds": stats.get("scan_seconds")},
        **(await _readability(base, events)),
    }


async def _readability(base, events) -> dict:
    """有多少事件是解不开的。

    加密片段的目录结构、文件名、大小都正常，扫描器照样能数出"3 个事件、
    每个 2 分钟"。不明说的话，用户看到的就是"有条目、没画面"，只会以为
    是这个软件坏了。
    """
    from . import probe as _probe

    if base is None or not events:
        return {"unreadable": 0, "needs_transcode": 0}
    sample = events[:12]          # 只抽查最近的，整目录探测太慢
    results = await asyncio.gather(
        *(_probe.probe_event(base, e.segments) for e in sample),
        return_exceptions=True,
    )
    ok = [r for r in results if isinstance(r, dict)]
    # 探测超时（多半是网络挂载卡了一下）不算"读不出来"，单独报
    timeouts = sum(1 for r in ok if not r["readable"] and r["reason"] == "timeout")
    bad = sum(1 for r in ok if not r["readable"]) - timeouts
    hevc = sum(1 for r in ok if r["readable"] and r["reason"] == "needs_transcode")
    return {
        "unreadable": bad,
        "needs_transcode": hevc,
        "probe_timeouts": timeouts,
        "sampled": len(ok),
    }


@router.get("/thumb")
async def thumb(id: str = Query(...)):
    """事件缩略图。

    Tesla 只给 SentryClips / SavedClips 附 thumb.png，RecentClips 从来没有。
    没有就自己从第一个能解码的片段抽一帧 —— 否则整页只剩胶片图标，
    看起来就像"有条目、没画面"。
    """
    from fastapi.responses import FileResponse

    from . import thumbs

    ev = await scanner.aget(id)
    base = scanner.base_path()
    if ev is None or base is None:
        raise HTTPException(404, "event not found")
    try:
        hit = await thumbs.event_thumbnail(base, ev)
    except asyncio.TimeoutError:
        raise HTTPException(504, "thumbnail timed out") from None
    if hit is None:
        raise HTTPException(404, "could not extract a frame")
    return FileResponse(hit[0], media_type=hit[1])


@router.get("/events")
async def list_events(type: str | None = None, limit: int = 200, force: bool = False):
    events = await scanner.ascan(force=force)
    if type:
        events = [e for e in events if e.type == type]
    events.sort(key=lambda e: e.timestamp, reverse=True)
    # 只夹上界的话 limit=-5 会变成"除最后 5 条以外全给"，limit=0 又和
    # "一条录像都没有"长得一模一样。两头都夹住。
    return [e.to_dict() for e in events[: max(1, min(limit, 1000))]]


@router.get("/event")
async def get_event(id: str = Query(...)):
    """事件详情。每段带实测时长 duration 和它在事件时间轴上的起点 offset（秒）。

    播放器的总时长、进度条、"第几段第几秒"都应该按这两个字段算，不要再假设
    每段 60 秒 —— 事件的最后一段、被截断的片段都更短。区间导出的
    start_offset / end_offset 也是按这条时间轴解释的。
    """
    ev = await scanner.aget(id)
    base = scanner.base_path()
    if ev is None or base is None:
        raise HTTPException(404, "event not found")
    await fill_durations(ev, base)
    d = ev.to_dict(with_segments=True)
    t = 0.0
    for seg in d["segments"]:
        seg["offset"] = round(t, 3)
        seg["duration_known"] = "duration" in seg
        seg.setdefault("duration", 60.0)
        t += seg["duration"]
    d["duration"] = round(t, 3)
    return d


@router.get("/telemetry")
async def get_telemetry(
    start: str,
    end: str,
    car_id: int | None = None,
    step: float = 0.5,
):
    """按车机本地时间段取遥测（供网页播放器实时叠加显示）。"""
    try:
        t0_local = datetime.fromisoformat(start)
        t1_local = datetime.fromisoformat(end)
    except ValueError as e:
        raise HTTPException(400, f"bad time format: {e}") from e
    if (t1_local - t0_local).total_seconds() > 16200:
        raise HTTPException(400, "time range too long (max 4.5h)")

    if car_id is None:
        try:
            row = await db.fetchrow("SELECT id FROM cars ORDER BY id LIMIT 1")
        except db.DbUnavailable as e:
            raise HTTPException(503, f"TeslaMate database unavailable: {e}") from e
        if row is None:
            raise HTTPException(404, "no car in TeslaMate yet")
        car_id = row["id"]

    from datetime import timedelta

    offset = await telemetry.clock_offset(car_id)
    t0 = telemetry.local_naive_to_utc(t0_local) + timedelta(seconds=offset)
    t1 = telemetry.local_naive_to_utc(t1_local) + timedelta(seconds=offset)
    try:
        positions = await telemetry.fetch_positions(car_id, t0, t1)
    except db.DbUnavailable as e:
        raise HTTPException(503, f"TeslaMate database unavailable: {e}") from e
    samples = telemetry.resample(
        positions, t0.timestamp(), t1.timestamp(), step=max(step, 0.2)
    )
    return {
        "car_id": car_id, "start": start, "step": step,
        "clock_offset": offset, "samples": samples,
    }


@router.get("/telemetry.csv")
async def telemetry_csv(
    start: str,
    end: str,
    car_id: int | None = None,
    step: float = 1.0,
    clip: str | None = None,
):
    """把某段时间的遥测导成 CSV —— 双源融合的结果，一行一秒。

    SEI 给的是档位/方向盘/踏板/AP/GPS（30-36Hz，但只在行驶时有、且要求
    固件 2025.44.25+），TeslaMate 给的是电量/功率/海拔/温度（~1Hz）。
    传了 clip 就把两者按时间对齐后合并 —— 这个融合正是本项目的核心，
    导出来才好拿去做视频叠加、事故分析或者自己画图。
    """
    import csv as _csv
    import io as _io
    from datetime import timedelta

    try:
        t0_local = datetime.fromisoformat(start)
        t1_local = datetime.fromisoformat(end)
    except ValueError as e:
        raise HTTPException(400, f"bad time format: {e}") from e
    if (t1_local - t0_local).total_seconds() > 16200:
        raise HTTPException(400, "time range too long (max 4.5h)")

    if car_id is None:
        try:
            row = await db.fetchrow("SELECT id FROM cars ORDER BY id LIMIT 1")
        except db.DbUnavailable as e:
            raise HTTPException(503, f"TeslaMate database unavailable: {e}") from e
        if row is None:
            raise HTTPException(404, "no car in TeslaMate yet")
        car_id = row["id"]

    step = max(float(step), 0.2)
    offset = await telemetry.clock_offset(car_id)
    t0 = telemetry.local_naive_to_utc(t0_local) + timedelta(seconds=offset)
    t1 = telemetry.local_naive_to_utc(t1_local) + timedelta(seconds=offset)
    try:
        positions = await telemetry.fetch_positions(car_id, t0, t1)
    except db.DbUnavailable as e:
        raise HTTPException(503, f"TeslaMate database unavailable: {e}") from e
    samples = telemetry.resample(positions, t0.timestamp(), t1.timestamp(), step=step)

    # SEI（可选）。注意 SEI 的 t 是**相对片段起点**的秒数，而下面的桶号是
    # 相对**请求区间起点**的。两者起点不同就会整体错位 —— 片段起点从文件名
    # 解析出来（TeslaCam 命名是 YYYY-MM-DD_HH-MM-SS-camera.mp4，车机本地时间），
    # 换算成同一条时间轴再落桶。
    sei_by_bucket: dict[int, dict] = {}
    if clip:
        base = scanner.base_path()
        if base is not None:
            target = (base / clip).resolve()
            if target.is_relative_to(base.resolve()) and target.is_file():
                shift = 0.0
                m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", target.name)
                if m:
                    clip_start = datetime.fromisoformat(
                        f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}")
                    # 片段起点比请求起点晚多少秒
                    shift = (clip_start - t0_local).total_seconds()
                for s in await asyncio.to_thread(sei.extract, target, step=step):
                    idx = round((s["t"] + shift) / step)
                    if idx >= 0:
                        sei_by_bucket[idx] = s

    def num(v, digits=2):
        return "" if v is None else round(float(v), digits)

    cols = ["t", "timestamp_utc", "speed_kmh", "power_kw", "soc", "usable_soc",
            "range_km", "elevation_m", "odometer_km", "outside_temp_c", "inside_temp_c",
            "lat", "lon",
            "sei_gear", "sei_speed_kmh", "sei_steering", "sei_accel_pedal",
            "sei_brake", "sei_autopilot", "sei_lat", "sei_lon"]
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(cols)
    base_ts = t0.timestamp()
    for i, s in enumerate(samples):
        e = sei_by_bucket.get(i, {})
        # s["t"] 是相对片段起点的秒数；换算回绝对 UTC 时刻方便和别的数据对齐
        rel = s.get("t", i * step)
        w.writerow([
            round(rel, 2),
            datetime.fromtimestamp(base_ts + rel, tz=timezone.utc).isoformat(),
            num(s.get("speed"), 1), num(s.get("power"), 2),
            num(s.get("battery_level"), 1), num(s.get("usable_battery_level"), 1),
            num(s.get("rated_battery_range_km"), 2), num(s.get("elevation"), 1),
            num(s.get("odometer"), 2), num(s.get("outside_temp"), 1),
            num(s.get("inside_temp"), 1),
            num(s.get("latitude"), 6), num(s.get("longitude"), 6),
            e.get("gear", ""), e.get("speed_kmh", ""), e.get("steering_angle", ""),
            e.get("accel_pedal", ""), e.get("brake", ""), e.get("ap", ""),
            e.get("lat", ""), e.get("lon", ""),
        ])
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ohmytesla-telemetry.csv"'},
    )


@router.get("/sei")
async def get_sei(path: str, step: float = 0.5):
    """提取单个片段内嵌的 SEI 逐帧遥测（供网页播放器仪表显示）。"""
    base = scanner.base_path()
    if base is None:
        raise HTTPException(404, "TeslaCam root not found")
    target = (base / path).resolve()
    if not target.is_relative_to(base.resolve()) or not target.is_file():
        raise HTTPException(404, "clip not found")
    samples = await asyncio.to_thread(sei.extract, target, max(step, 0.2))
    return {"available": bool(samples), "samples": samples}


from typing import Literal  # noqa: E402 - 延后导入以避免模块级循环依赖


class ComposeRequest(BaseModel):
    event_id: str
    layout: Literal["grid", "front_pip", "back_pip", "vertical", "wide"] = "grid"
    cameras: list[str] | None = None
    quality: Literal["high", "standard", "fast"] = "standard"
    start_offset: float = 0
    end_offset: float | None = None
    segment: str | None = None  # 分段起始时间 ISO 串——只合成该分段
    overlay: bool = True
    lang: str | None = None
    car_id: int | None = None


@router.post("/compose")
async def compose(req: ComposeRequest):
    ev = await scanner.aget(req.event_id)
    base = scanner.base_path()
    if ev is None or base is None:
        raise HTTPException(404, "event not found")
    if req.start_offset < 0:
        raise HTTPException(400, "start_offset must be >= 0")
    if req.end_offset is not None and req.end_offset <= req.start_offset:
        raise HTTPException(400, "end_offset must be greater than start_offset")
    if req.cameras and not any(c in CAMERAS for c in req.cameras):
        raise HTTPException(400, f"unknown cameras: {req.cameras}")
    if req.start_offset > 0 and not req.segment:
        # 起点越过片尾时 ffmpeg 会 rc 0 产出一个没有画面的空壳 mp4，任务还显示"完成"
        await fill_durations(ev, base)
        total = sum(s.duration or 60.0 for s in ev.segments)
        if req.start_offset >= total:
            raise HTTPException(
                400, f"start_offset {req.start_offset:g}s is beyond the end of the event ({total:.1f}s)")
    job_id = await jobs.submit(req.event_id, req.model_dump(exclude={"event_id"}))
    return {"job_id": job_id}


# ---------------- U 盘导入 ----------------

_IMPORT_RE = re.compile(
    r"^(?:"
    r"(?:SentryClips|SavedClips)/\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}/"
    r"(?:event\.json|thumb\.png|\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-"
    r"(?:front|back|left_repeater|right_repeater|left_pillar|right_pillar)\.mp4)"
    r"|RecentClips/\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-"
    r"(?:front|back|left_repeater|right_repeater|left_pillar|right_pillar)\.mp4"
    r")$"
)


def _import_base() -> Path:
    """导入写到当前生效的素材根；还没有的话直接用根目录。

    以前这里会在空根目录下建一个空的 TeslaCam/，而扫描器只要看到 TeslaCam/
    存在就只看它 —— 之后 teslausb 落在根目录的素材就全看不见了。
    """
    base = scanner.base_path()
    if base is None:
        base = Path(settings.teslacam_root)
        base.mkdir(parents=True, exist_ok=True)
    return base


_MAX_IMPORT_FILE = 800 * 1024 * 1024  # 单文件上限（HW4 前摄 1 分钟远小于此）


@router.post("/import")
async def import_files(
    files: list[UploadFile] = File(...),  # noqa: B008 - FastAPI 的 File()/Form() 就是要写在默认值里，这是框架约定
    paths: list[str] = Form(...),  # noqa: B008 - FastAPI 的 File()/Form() 就是要写在默认值里，这是框架约定
    rescan: bool = Form(False),
):
    """浏览器把 U 盘里的事件文件上传进服务器素材目录（保留相对路径）。

    前端逐文件调用，只在最后一个文件带 rescan=true（全量重扫较重，
    不能每个文件都在事件循环里做一次）。
    """
    import shutil

    if len(files) != len(paths):
        raise HTTPException(400, "files/paths mismatch")
    base = await asyncio.to_thread(_import_base)
    imported, skipped = 0, []
    for f, rel in zip(files, paths, strict=False):
        rel = rel.replace("\\", "/").lstrip("/")  # noqa: PLW2901 - 循环变量被规范化后复用是本意，改名反而要多一个中间变量
        # 兼容传来的路径带 TeslaCam/ 前缀
        rel = rel.removeprefix("TeslaCam/")  # noqa: PLW2901 - 循环变量被规范化后复用是本意，改名反而要多一个中间变量
        if not _IMPORT_RE.match(rel):
            skipped.append(rel)
            continue
        if f.size and f.size > _MAX_IMPORT_FILE:
            skipped.append(f"{rel} (too large)")
            continue
        free = shutil.disk_usage(base).free
        if f.size and free < f.size + 1024 * 1024 * 1024:
            raise HTTPException(507, f"服务器磁盘空间不足（剩余 {free // 1024 // 1024} MB）")
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 写盘走线程池。uvicorn 是单进程的，直接在协程里 out.write() 会让
            # 事件循环在每个 4MB 块上停住 —— 导一个几 GB 的素材库时整个门户
            # （包括实时状态 SSE 和告警扇出）全程卡死。
            with open(dest, "wb") as out:  # noqa: ASYNC230 - 写操作已转入线程池
                while chunk := await f.read(4 * 1024 * 1024):
                    await asyncio.to_thread(out.write, chunk)
        except OSError as e:
            with contextlib.suppress(OSError):
                dest.unlink()  # 别留半截文件，扫描器会当成有效分段
            raise HTTPException(507, f"写入失败: {e}") from e
        imported += 1
    if rescan:
        await scanner.ascan(force=True)
    return {"imported": imported, "skipped": skipped}


# ---------------- HEVC 转码预览 ----------------

_preview_locks: dict[str, asyncio.Lock] = {}
_preview_sem = asyncio.Semaphore(2)  # 全局并发上限，防多标签页把小主机 CPU 打满


@router.post("/preview")
async def preview(path: str = Form(...)):
    """把浏览器放不了的 HEVC 片段转成 H.264 预览（缓存复用）。"""
    import contextlib

    base = scanner.base_path()
    if base is None:
        raise HTTPException(404, "TeslaCam root not found")
    target = (base / path).resolve()
    if not target.is_relative_to(base.resolve()) or not target.is_file():
        raise HTTPException(404, "clip not found")

    key = hashlib.sha1(f"{target}:{target.stat().st_mtime}".encode()).hexdigest()[:20]  # noqa: S324 - sha1 只用作缓存键，不用于任何安全判定
    out = settings.preview_dir / f"{key}.mp4"
    if out.exists():
        return {"url": f"/preview/{out.name}"}

    lock = _preview_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock, _preview_sem:
            if not out.exists():
                import uuid

                # 临时文件名唯一：即使极端并发下重复转码，rename 覆盖也无害
                tmp = settings.preview_dir / f"{key}.{uuid.uuid4().hex[:6]}.tmp.mp4"
                proc = await asyncio.create_subprocess_exec(
                    ff.ffmpeg(), "-hide_banner", "-nostdin", "-y", "-i", str(target),
                    "-vf", "scale=960:-2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "28", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(tmp),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, err = await asyncio.wait_for(proc.communicate(), timeout=240)
                except asyncio.TimeoutError:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        await proc.wait()
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                    raise HTTPException(504, "transcode timeout") from None
                if proc.returncode != 0:
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                    raise HTTPException(500, f"transcode failed: {err.decode()[-300:]}")
                tmp.rename(out)
    finally:
        # 防字典无限膨胀；仅在无人持有时清理
        if len(_preview_locks) > 128 and not lock.locked():
            _preview_locks.pop(key, None)
    return {"url": f"/preview/{out.name}"}


# ---------------- 时钟自动校正 ----------------


async def _say(key: str, **vars_) -> str:
    """A refusal in the owner's language (the page shows it as is)."""
    from ..i18n.prefs import get as get_prefs
    from ..i18n.router import t

    return t(key, (await get_prefs())["locale"], **vars_)

@router.post("/align")
async def auto_align_clock(event_id: str = Form(...), car_id: int | None = Form(None)):
    """拟合不可信（误差太大、没有明显的最优点、车速没变化）时返回 422 并保留原偏移。"""
    ev = await scanner.aget(event_id)
    if ev is None:
        raise HTTPException(404, "event not found")
    base = scanner.base_path()
    if base is None:
        raise HTTPException(404, "TeslaCam root not found（U 盘/挂载目录已消失？）")
    if car_id is None:
        try:
            rows = await db.fetch("SELECT id FROM cars ORDER BY id LIMIT 2")
        except db.DbUnavailable as e:
            raise HTTPException(503, f"TeslaMate database unavailable: {e}") from e
        if not rows:
            raise HTTPException(404, "no car in TeslaMate yet")
        if len(rows) > 1:
            # 校准结果会写入指定车辆并影响其所有叠加——多车时必须显式指定
            raise HTTPException(422, await _say("align.pick_car"))
        car_id = rows[0]["id"]
    try:
        result = await align.auto_align(ev, base, car_id)
    except align.AlignRefused as e:
        raise HTTPException(422, await _say(e.key, **e.vars)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except db.DbUnavailable as e:
        raise HTTPException(503, f"TeslaMate database unavailable: {e}") from e
    return {"car_id": car_id, **result}


@router.get("/jobs")
async def list_jobs():
    """任务列表。每个任务多一个 report：跳过的分段及原因、不完整的片段、
    叠加层状态（burned / unavailable / no_telemetry / off）、编码器、警告。"""
    from ..alerts.store import store

    rows = await store.list_jobs()
    reports = await get_reports()
    for r in rows:
        r["report"] = reports.get(str(r["id"]))
    return rows


@router.get("/jobs/{job_id}")
async def get_job(job_id: int):
    from ..alerts.store import store

    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    job["report"] = (await get_reports()).get(str(job_id))
    return job


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: int):
    ok = await jobs.cancel(job_id)
    return {"ok": ok}


# ---------------- SEI 遥测入库与驾驶分析 ----------------
#
# 这一组回答的是"整个素材库里"的问题（这个月自驾占多少、所有急刹在哪），
# 上面那组 /telemetry /sei 回答的是"这一段里"的问题。两者数据同源，
# 区别只在解析结果有没有落库。

@router.get("/evidence")
async def evidence_package(id: str, car_id: int | None = None):
    """取证包：原始片段 + 事件元数据 + 遥测 CSV + 校验和，打成一个 ZIP。

    流式输出：边打包边下载，内存占用与事件大小无关（几 GB 的事件也只占几 MB）。
    因此没有 Content-Length —— 浏览器显示的是已下载量而不是百分比。
    """
    from . import evidence

    try:
        stream, name = await evidence.build(id, car_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return StreamingResponse(
        stream, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 "X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


@router.get("/encoder")
async def encoder_status():
    """当前用的视频编码器。导出很慢时第一件事就是看这里是不是退回了 libx264。"""
    from .hwaccel import status

    return await asyncio.to_thread(status)


@router.get("/index/status")
async def index_status():
    from . import index as sei_index

    return await sei_index.index_status()


@router.post("/index/rebuild")
async def index_rebuild(force: bool = False, limit: int | None = None):
    """把素材库过一遍解析入库。

    单段的 NAL 遍历在 index_event 内部走 asyncio.to_thread，所以整库重建
    期间事件循环仍然能响应其它请求 —— uvicorn 是单进程的，解析要是占着
    事件循环，重建时整个门户都会卡死。
    """
    from . import index as sei_index

    return await sei_index.reindex(force, limit)


@router.get("/fsd/summary")
async def fsd_summary(days: int = 90):
    """自驾使用总览。数据只来自本地录像的 SEI，不花任何 API 费用。"""
    from . import index as sei_index

    return await sei_index.fsd_summary(days)


@router.get("/fsd/trend")
async def fsd_trend(days: int = 180, bucket_days: int = 7):
    from . import index as sei_index

    return await sei_index.fsd_trend(days, bucket_days)


@router.get("/driving-events")
async def driving_events(kind: str | None = None, days: int = 90, limit: int = 200):
    """检出的驾驶事件（急刹 / 自驾退出）。"""
    from . import index as sei_index

    if kind and kind not in ("hard_brake", "disengage"):
        raise HTTPException(422, "kind must be hard_brake or disengage")
    return await sei_index.driving_events(kind, days, limit)


@router.get("/watcher")
async def watcher_status():
    """新录像推送的状态：基线里有多少事件、哪些还在等落定/重试。"""
    from .watcher import watcher

    return watcher.status()


# ---------------- 2026.20+ 加密片段解密 ----------------
#
# 流程（详见 decrypt.py）：先 GET /decrypt/scan 看这个事件里有几个加密片段、
# 会往 Tesla 发什么（只有头部元数据：VIN、key_id、时间戳、wrapped key）；
# 用户贴入 dashcam.tesla.com 的登录 token 后 POST /decrypt 开始，
# GET /decrypt/status 轮询进度。token 只在内存里用这一次，不落盘、不进日志。

class DecryptRequest(BaseModel):
    event_id: str
    token: str
    keep_encrypted: bool = True   # 把加密原件留成 <名字>.mp4.enc


async def _event_clip_paths(event_id: str) -> tuple[Path, list[Path]]:
    ev = await scanner.aget(event_id)
    base = scanner.base_path()
    if ev is None or base is None:
        raise HTTPException(404, "event not found")
    paths = [base / rel for seg in ev.segments for rel in seg.cameras.values()]
    return base, [p for p in paths if p.resolve().is_relative_to(base.resolve())]


@router.get("/decrypt/scan")
async def decrypt_scan(id: str = Query(...)):
    from . import decrypt

    base, paths = await _event_clip_paths(id)
    enc = await asyncio.to_thread(decrypt.find_encrypted, paths)
    try:
        items = await asyncio.to_thread(decrypt.key_request_items, enc)
    except (OSError, ValueError) as e:
        raise HTTPException(422, f"could not read an encrypted clip header: {e}") from e
    return {
        "event_id": id,
        "clips": len(paths),
        "encrypted": len(enc),
        "encrypted_paths": [str(p.relative_to(base)) for p in enc],
        # 原样展示将要发给 Tesla 的内容：只有元数据，没有视频字节
        "key_service": decrypt.DECRYPT_ENDPOINT,
        "request_preview": items,
        "running": decrypt.decryptor.running,
    }


@router.post("/decrypt")
async def decrypt_start(req: DecryptRequest):
    from . import decrypt

    token = req.token.strip().removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(422, "a dashcam.tesla.com access token is required")
    base, paths = await _event_clip_paths(req.event_id)
    enc = await asyncio.to_thread(decrypt.find_encrypted, paths)
    if not enc:
        return {"state": "nothing_to_do", "event_id": req.event_id, "encrypted": 0}
    try:
        return decrypt.decryptor.start(req.event_id, enc, base, token, req.keep_encrypted)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/decrypt/status")
async def decrypt_status():
    from . import decrypt

    return decrypt.decryptor.state
