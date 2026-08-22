"""视频合成后台任务队列（单 worker 串行跑 ffmpeg）。"""

import asyncio
import contextlib
import logging
import re
from datetime import datetime, timedelta

from .. import db
from ..alerts.store import store
from ..config import settings
from . import composer, sei, telemetry
from .scanner import scanner

log = logging.getLogger(__name__)

_OUT_TIME_RE = re.compile(rb"out_time_us=(\d+)")


class JobManager:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._cancel_req: set[int] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._worker(), name="compose-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for proc in self._procs.values():
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def submit(self, event_id: str, params: dict) -> int:
        job_id = await store.create_job(event_id, params)
        await self._queue.put(job_id)
        return job_id

    async def cancel(self, job_id: int) -> bool:
        proc = self._procs.get(job_id)
        if proc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return True
        job = await store.get_job(job_id)
        if job and job["status"] == "queued":
            await store.update_job(job_id, status="cancelled")
            return True
        if job and job["status"] == "running":
            # 准备阶段（还没起 ffmpeg）：打标记，_run 在检查点响应
            self._cancel_req.add(job_id)
            return True
        return False

    def _check_cancelled(self, job_id: int) -> bool:
        return job_id in self._cancel_req

    async def _recover(self) -> None:
        """重启恢复：running 的判失败，queued 的重新入队。"""
        try:
            rows = await store.db.execute_fetchall(
                "SELECT id, status FROM compose_jobs WHERE status IN ('queued', 'running')"
            )
            for r in rows:
                if r["status"] == "running":
                    await store.update_job(
                        r["id"], status="failed", error="服务重启导致任务中断，请重新提交"
                    )
                else:
                    await self._queue.put(r["id"])
        except Exception:
            log.exception("job recovery failed")

    async def _worker(self) -> None:
        """唯一一个合成 worker。它死了 = 之后所有任务永远卡在 queued。

        try 必须罩住整个循环体，不能只罩 self._run：`store.get_job` 或
        `update_job` 抛错（SQLite 锁竞争、库文件被换掉）时异常会逃出去，
        这个任务就此结束，而 jobs.start() 只在启动时创建一次、无人监督。
        用户看到的是"点了导出没反应"，日志里也只有一行 Task exception。
        """
        await self._recover()
        while True:
            job_id = None
            try:
                job_id = await self._queue.get()
                job = await store.get_job(job_id)
                if not job or job["status"] != "queued":
                    continue
                await self._run(job_id, job)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("compose job %s failed", job_id)
                if job_id is not None:
                    with contextlib.suppress(Exception):
                        await store.update_job(job_id, status="failed", error=str(e)[:2000])
            finally:
                if job_id is not None:
                    self._cancel_req.discard(job_id)

    async def _default_car_id(self) -> int | None:
        try:
            row = await db.fetchrow("SELECT id FROM cars ORDER BY id LIMIT 1")
            return row["id"] if row else None
        except db.DbUnavailable:
            return None

    async def _run(self, job_id: int, job: dict) -> None:
        import json

        params = json.loads(job["params"])
        event = scanner.get(job["event_id"])
        if event is None:
            raise ValueError(f"event not found: {job['event_id']}")
        base = scanner.base_path()
        assert base is not None

        await store.update_job(job_id, status="running", progress=0)

        layout = params.get("layout", "grid")
        if layout not in ("grid", "front_pip", "back_pip"):
            layout = "grid"
        quality = params.get("quality", "standard")
        cameras = params.get("cameras") or None
        start_offset = max(float(params.get("start_offset") or 0), 0.0)
        end_offset = params.get("end_offset")
        end_offset = float(end_offset) if end_offset else None
        if end_offset is not None and end_offset <= start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        only_segment = params.get("segment") or None
        with_overlay = params.get("overlay", True)
        from ..i18n import prefs as user_prefs

        up = await user_prefs.get()
        lang = params.get("lang") or up["locale"]

        safe_name = re.sub(r"[^\w.-]", "_", job["event_id"])
        out_path = settings.exports_dir / f"{safe_name}_{layout}_{job_id}.mp4"

        # 第一遍拿画布尺寸，写好 ASS 后第二遍生成最终命令
        plan = composer.build_plan(
            event, base, None, out_path,
            cameras=cameras, layout=layout, quality=quality,
            start_offset=start_offset, end_offset=end_offset,
            only_segment=only_segment,
        )

        ass_path = None
        if with_overlay:
            step = 0.5
            car_id = params.get("car_id") or await self._default_car_id()
            clk = await telemetry.clock_offset(int(car_id)) if car_id is not None else 0.0
            # 兜底时钟（SEI-only 路径的 ASS 时钟显示）也要吃进校准偏移
            clip_start_local = plan.segments[0].start + timedelta(seconds=start_offset + clk)

            # 1) TeslaMate positions 轨道（电量/功率/海拔/温度等，~1Hz）。
            #    分段可能不连续（掉段/间隙），因此逐段做"输出视频时间 ↔ 墙钟"映射，
            #    不能拿 event.start 线性外推。
            overlay: list[dict] = []
            if car_id is not None:
                try:
                    positions = await telemetry.fetch_positions(
                        int(car_id),
                        telemetry.local_naive_to_utc(plan.segments[0].start)
                        + timedelta(seconds=clk),
                        telemetry.local_naive_to_utc(plan.segments[-1].start)
                        + timedelta(seconds=60 + clk),
                    )
                    tz = telemetry.local_tz()
                    for i, seg in enumerate(plan.segments):
                        v0 = i * 60.0 - start_offset  # 分段在输出视频中的起点
                        v_lo = max(v0, 0.0)
                        v_hi = min(v0 + 60.0, plan.duration)
                        if v_hi <= v_lo:
                            continue
                        wall0 = (
                            telemetry.local_naive_to_utc(seg.start).timestamp()
                            + clk + (v_lo - v0)
                        )
                        seg_overlay = telemetry.resample(
                            positions, wall0, wall0 + (v_hi - v_lo), step=step
                        )
                        for s in seg_overlay:
                            wall = wall0 + s["t"]
                            s["t"] = round(v_lo + s["t"], 2)
                            s["clock"] = datetime.fromtimestamp(wall, tz).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            overlay.append(s)
                except db.DbUnavailable:
                    log.warning("TeslaMate DB unavailable; overlay will be SEI-only")

            if self._check_cancelled(job_id):
                await store.update_job(job_id, status="cancelled")
                return

            # 2) SEI 逐帧遥测轨道（档位/方向盘/踏板/AP，固件 2025.44.25+）
            sei_track: list[dict] = []
            sei_cam = "front" if "front" in plan.cameras else plan.cameras[0]
            for i, seg in enumerate(plan.segments):
                rel = seg.cameras.get(sei_cam)
                if not rel:
                    continue
                for s in await asyncio.to_thread(sei.extract, base / rel):
                    t_video = i * 60 + s["t"] - start_offset
                    if 0 <= t_video <= plan.duration:
                        sei_track.append({**s, "t": round(t_video, 2)})

            # positions 不可用但有 SEI 时，用空时间轴承载 SEI 仪表
            if not overlay and sei_track:
                n_steps = int(plan.duration / step) + 1
                overlay = [{"t": round(i * step, 1)} for i in range(n_steps)]

            if overlay:
                overlay.sort(key=lambda s: s["t"])
                ass_path = settings.exports_dir / f"{safe_name}_{job_id}.ass"
                ass_path.write_text(
                    telemetry.build_ass(
                        overlay, plan.width, plan.height,
                        clip_start_local, step=step, lang=lang,
                        sei=sei_track or None, prefs=up,
                    ),
                    encoding="utf-8",
                )

        if self._check_cancelled(job_id):
            await store.update_job(job_id, status="cancelled")
            return

        plan = composer.build_plan(
            event, base, ass_path, out_path,
            cameras=cameras, layout=layout, quality=quality,
            start_offset=start_offset, end_offset=end_offset,
            only_segment=only_segment,
        )

        log.info("compose job %s: %s", job_id, " ".join(plan.cmd[:20]) + " ...")
        proc = await asyncio.create_subprocess_exec(
            *plan.cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._procs[job_id] = proc
        stderr_tail: list[bytes] = []

        async def read_stderr():
            assert proc.stderr
            async for line in proc.stderr:
                stderr_tail.append(line)
                if len(stderr_tail) > 60:
                    stderr_tail.pop(0)

        stderr_task = asyncio.create_task(read_stderr())
        try:
            assert proc.stdout
            last_pct = -1.0
            async for line in proc.stdout:
                m = _OUT_TIME_RE.search(line)
                if m:
                    pct = min(int(m.group(1)) / 1e6 / plan.duration * 100, 99.9)
                    if pct - last_pct >= 1:
                        last_pct = pct
                        await store.update_job(job_id, progress=round(pct, 1))
            await proc.wait()
        finally:
            # 任何异常/取消路径都先杀掉 ffmpeg，再等 stderr 收尾，避免 worker 被卡死
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            with contextlib.suppress(Exception):
                await stderr_task
            self._procs.pop(job_id, None)

        if proc.returncode == 0:
            await store.update_job(
                job_id, status="done", progress=100,
                output=f"/exports/{out_path.name}",
                error=f"units:{up['length']}/{up['temp']} lang:{lang}",
            )
            if ass_path:
                with contextlib.suppress(OSError):
                    ass_path.unlink()
        else:
            err = b"".join(stderr_tail[-25:]).decode("utf-8", "replace")
            status = "cancelled" if proc.returncode in (-9, 137) else "failed"
            await store.update_job(job_id, status=status, error=err[:2000])


jobs = JobManager()
