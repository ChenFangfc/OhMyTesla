from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import asyncio
import hashlib
import re
from pathlib import Path

from fastapi import File, Form, UploadFile

from .. import db
from ..config import settings
from . import align, sei, telemetry
from .composer import CAMERA_NAMES_ZH
from .jobs import jobs
from .scanner import scanner

router = APIRouter(prefix="/api/dashcam", tags=["dashcam"])


@router.get("/status")
async def status():
    events = scanner.scan()
    base = scanner.base_path()
    root = Path(settings.teslacam_root)
    # "目录不存在" 和 "目录在但还没放东西" 是两回事，混为一谈会让人以为坏了
    return {
        "root": settings.teslacam_root,
        "found": base is not None,
        "root_exists": root.exists(),
        "root_empty": root.exists() and not any(root.iterdir()),
        "events": len(events),
        "by_type": {
            t: sum(1 for e in events if e.type == t)
            for t in ("sentry", "saved", "recent")
        },
        "camera_names": CAMERA_NAMES_ZH,
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
    bad = sum(1 for r in ok if not r["readable"])
    hevc = sum(1 for r in ok if r["readable"] and r["reason"] == "needs_transcode")
    return {
        "unreadable": bad,
        "needs_transcode": hevc,
        "sampled": len(ok),
    }


@router.get("/thumb")
async def thumb(id: str = Query(...)):
    """事件缩略图。

    Tesla 只给 SentryClips / SavedClips 附 thumb.png，RecentClips 从来没有。
    没有就自己从第一个能解码的片段抽一帧 —— 否则整页只剩胶片图标，
    看起来就像"有条目、没画面"。
    """
    from fastapi.responses import FileResponse, Response

    ev = scanner.get(id)
    base = scanner.base_path()
    if ev is None or base is None:
        raise HTTPException(404, "event not found")

    if ev.thumb:                                  # 车机自带的，直接给
        f = (base / ev.thumb).resolve()
        if f.is_file() and f.is_relative_to(base.resolve()):
            return FileResponse(f, media_type="image/png")

    cache = Path(settings.preview_dir) / "thumbs"
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / (hashlib.sha256(id.encode()).hexdigest()[:20] + ".jpg")
    if dest.is_file() and dest.stat().st_size > 512:
        return FileResponse(dest, media_type="image/jpeg")

    src = _first_readable(base, ev)
    if src is None:
        raise HTTPException(404, "no readable frame")

    tmp = dest.with_suffix(".part")
    proc = await asyncio.create_subprocess_exec(
        # -update 1 必须有：没有它 ffmpeg 会把输出当成图片序列，
        # 因为文件名里没有 %03d 这类模式而报错退出
        "ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", str(src),
        "-frames:v", "1", "-update", "1", "-vf", "scale=480:-2",
        "-q:v", "5", "-f", "image2", str(tmp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=25)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "thumbnail timed out") from None
    if not tmp.is_file() or tmp.stat().st_size < 512:
        # 片段不足 2 秒时 -ss 2 会落在片尾之后，退回第一帧再试
        tmp.unlink(missing_ok=True)
        retry = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-frames:v", "1", "-update", "1", "-vf", "scale=480:-2",
            "-q:v", "5", "-f", "image2", str(tmp),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(retry.wait(), timeout=25)
        except asyncio.TimeoutError:
            retry.kill()
    if not tmp.is_file() or tmp.stat().st_size < 512:
        tmp.unlink(missing_ok=True)
        raise HTTPException(404, "could not extract a frame")
    tmp.replace(dest)                             # 原子替换，避免读到写一半的文件
    return FileResponse(dest, media_type="image/jpeg")


def _first_readable(base: Path, ev) -> Path | None:
    """挑一个用来抽帧的片段：优先前摄，其次任意机位。"""
    for seg in ev.segments:
        for cam in ("front", "back", "left_repeater", "right_repeater"):
            rel = seg.cameras.get(cam)
            if rel:
                f = (base / rel).resolve()
                if f.is_file() and f.stat().st_size > 10_000:
                    return f
    return None


@router.get("/events")
async def list_events(type: str | None = None, limit: int = 200, force: bool = False):
    events = scanner.scan(force=force)
    if type:
        events = [e for e in events if e.type == type]
    events.sort(key=lambda e: e.timestamp, reverse=True)
    return [e.to_dict() for e in events[: min(limit, 1000)]]


@router.get("/event")
async def get_event(id: str = Query(...)):
    ev = scanner.get(id)
    if ev is None:
        raise HTTPException(404, "event not found")
    return ev.to_dict(with_segments=True)


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


from typing import Literal


class ComposeRequest(BaseModel):
    event_id: str
    layout: Literal["grid", "front_pip", "back_pip"] = "grid"
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
    ev = scanner.get(req.event_id)
    if ev is None:
        raise HTTPException(404, "event not found")
    if req.start_offset < 0:
        raise HTTPException(400, "start_offset must be >= 0")
    if req.end_offset is not None and req.end_offset <= req.start_offset:
        raise HTTPException(400, "end_offset must be greater than start_offset")
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
    base = scanner.base_path()
    if base is None:
        root = Path(settings.teslacam_root)
        base = root / "TeslaCam"
        base.mkdir(parents=True, exist_ok=True)
    return base


_MAX_IMPORT_FILE = 800 * 1024 * 1024  # 单文件上限（HW4 前摄 1 分钟远小于此）


@router.post("/import")
async def import_files(
    files: list[UploadFile] = File(...),
    paths: list[str] = Form(...),
    rescan: bool = Form(False),
):
    """浏览器把 U 盘里的事件文件上传进服务器素材目录（保留相对路径）。

    前端逐文件调用，只在最后一个文件带 rescan=true（全量重扫较重，
    不能每个文件都在事件循环里做一次）。
    """
    import shutil

    if len(files) != len(paths):
        raise HTTPException(400, "files/paths mismatch")
    base = _import_base()
    imported, skipped = 0, []
    for f, rel in zip(files, paths):
        rel = rel.replace("\\", "/").lstrip("/")
        # 兼容传来的路径带 TeslaCam/ 前缀
        if rel.startswith("TeslaCam/"):
            rel = rel[len("TeslaCam/"):]
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
            with open(dest, "wb") as out:
                while chunk := await f.read(4 * 1024 * 1024):
                    out.write(chunk)
        except OSError as e:
            with __import__("contextlib").suppress(OSError):
                dest.unlink()  # 别留半截文件，扫描器会当成有效分段
            raise HTTPException(507, f"写入失败: {e}") from e
        imported += 1
    if rescan:
        await asyncio.to_thread(scanner.scan, True)
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

    key = hashlib.sha1(f"{target}:{target.stat().st_mtime}".encode()).hexdigest()[:20]
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
                    "ffmpeg", "-hide_banner", "-y", "-i", str(target),
                    "-vf", "scale=960:-2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "28", "-an", "-movflags", "+faststart", str(tmp),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, err = await asyncio.wait_for(proc.communicate(), timeout=240)
                except asyncio.TimeoutError:
                    proc.kill()
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

@router.post("/align")
async def auto_align_clock(event_id: str = Form(...), car_id: int | None = Form(None)):
    ev = scanner.get(event_id)
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
            raise HTTPException(422, "多车账号请先在播放器里选择车辆再校准")
        car_id = rows[0]["id"]
    try:
        result = await align.auto_align(ev, base, car_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except db.DbUnavailable as e:
        raise HTTPException(503, f"TeslaMate database unavailable: {e}") from e
    return {"car_id": car_id, **result}


@router.get("/jobs")
async def list_jobs():
    from ..alerts.store import store

    return await store.list_jobs()


@router.get("/jobs/{job_id}")
async def get_job(job_id: int):
    from ..alerts.store import store

    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: int):
    ok = await jobs.cancel(job_id)
    return {"ok": ok}
