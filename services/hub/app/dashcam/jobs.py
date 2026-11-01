"""视频合成后台任务队列（单 worker 串行跑 ffmpeg）。

每个任务除了 compose_jobs 表里的状态，还有一份"报告"（存在 hub 设置
`dashcam_job_reports` 里，/api/dashcam/jobs 会合并进返回值）：跳过了哪些分段、
为什么跳过、哪路机位的片段不完整、叠加层烧没烧进去、用的哪个编码器。
以前这些全是静默的 —— 导出少了两分钟、或者没有叠加层，用户无从知道。
"""

import asyncio
import contextlib
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from .. import db
from ..alerts.store import store
from ..config import settings
from . import composer, ff, hwaccel, probe, sei, telemetry
from .localtime import local_naive_to_utc, local_tz
from .scanner import scanner

log = logging.getLogger(__name__)

_OUT_TIME_RE = re.compile(rb"out_time_us=(\d+)")
_REPORTS_KEY = "dashcam_job_reports"
_REPORTS_MAX = 100
LAYOUTS = ("grid", "front_pip", "back_pip", "vertical", "wide")

# ffmpeg 这么久没有任何进度输出就判定卡死（坏掉的 CIFS 挂载上读文件会永远阻塞）
STALL_TIMEOUT_S = 600
# 输出小于这个字节数 / 时长为 0 就不算成功（起点越过片尾时 ffmpeg 会 rc 0 产出 261 字节的空壳）
_MIN_OUTPUT_BYTES = 2048


async def save_report(job_id: int, report: dict) -> None:
    def put(cur):
        cur = dict(cur or {})
        cur[str(job_id)] = report
        if len(cur) > _REPORTS_MAX:
            for k in sorted(cur, key=int)[: len(cur) - _REPORTS_MAX]:
                cur.pop(k, None)
        return cur

    try:
        await store.mutate_setting(_REPORTS_KEY, put, {})
    except Exception:
        log.warning("could not store report for job %s", job_id, exc_info=True)


async def get_reports() -> dict:
    try:
        return await store.get_setting(_REPORTS_KEY, {}) or {}
    except Exception:  # noqa: BLE001
        return {}


class JobCancelled(Exception):
    pass


def _cleanup(paths: list[str]) -> None:
    for f in paths:
        with contextlib.suppress(OSError):
            Path(f).unlink()


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


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
        with contextlib.suppress(Exception):
            await asyncio.to_thread(probe.save)

    async def submit(self, event_id: str, params: dict) -> int:
        job_id = await store.create_job(event_id, params)
        await self._queue.put(job_id)
        return job_id

    async def cancel(self, job_id: int) -> bool:
        # 先打标记再看状态：任何一个检查点（含"正在起 ffmpeg"那一瞬间）都能看见它
        self._cancel_req.add(job_id)
        proc = self._procs.get(job_id)
        if proc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return True
        # 只在还是 queued 时改成 cancelled；worker 同时把它改成 running 的话，
        # 由 worker 在检查点上响应标记
        cur = await store.db.execute(
            "UPDATE compose_jobs SET status = 'cancelled' WHERE id = ? AND status = 'queued'",
            (job_id,))
        await store.db.commit()
        if cur.rowcount:
            return True
        job = await store.get_job(job_id)
        if job and job["status"] == "running":
            return True
        self._cancel_req.discard(job_id)
        return False

    def _check_cancelled(self, job_id: int) -> bool:
        return job_id in self._cancel_req

    async def _recover(self) -> None:
        """重启恢复：running 的判失败，queued 的重新入队，残留的半成品清掉。"""
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
        # 启动时没有任务在跑，*.part.mp4 / 列表文件全是上次中断留下的
        with contextlib.suppress(OSError):
            for f in list(settings.exports_dir.glob("*.part.mp4")) + list(
                    settings.exports_dir.glob("*.ffconcat")):
                with contextlib.suppress(OSError):
                    f.unlink()

    async def _worker(self) -> None:
        """唯一一个合成 worker。它死了 = 之后所有任务永远卡在 queued。

        try 必须罩住整个循环体，不能只罩 self._run：`store.get_job` 或
        `update_job` 抛错（SQLite 锁竞争、库文件被换掉）时异常会逃出去，
        这个任务就此结束，而 jobs.start() 只在启动时创建一次、无人监督。
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
            except JobCancelled:
                with contextlib.suppress(Exception):
                    await store.update_job(job_id, status="cancelled")
            except (ValueError, RuntimeError) as e:
                # 预期内的失败（ffmpeg 报错、输出为空、参数不对）：原因已经写进任务，不刷堆栈
                log.warning("compose job %s failed: %s", job_id, (str(e).strip().splitlines() or [""])[-1][:300])
                if job_id is not None:
                    with contextlib.suppress(Exception):
                        await store.update_job(job_id, status="failed", error=str(e)[:2000])
            except Exception as e:
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

    def _checkpoint(self, job_id: int) -> None:
        if self._check_cancelled(job_id):
            raise JobCancelled

    async def _run(self, job_id: int, job: dict) -> None:
        params = json.loads(job["params"])
        # queued → running 必须是条件更新：用户恰好在这之前点了取消的话，
        # 无条件写 running 会把 cancelled 覆盖掉，任务照跑
        cur = await store.db.execute(
            "UPDATE compose_jobs SET status = 'running', progress = 0 WHERE id = ? AND status = 'queued'",
            (job_id,))
        await store.db.commit()
        if not cur.rowcount:
            return
        self._checkpoint(job_id)

        event = await scanner.aget(job["event_id"])
        if event is None:
            raise ValueError(f"event not found: {job['event_id']}")
        base = scanner.base_path()
        if base is None:
            raise ValueError("TeslaCam directory not found")

        layout = params.get("layout", "grid")
        if layout not in LAYOUTS:
            layout = "grid"
        quality = params.get("quality", "standard")
        cameras = [c for c in (params.get("cameras") or []) if c in composer.CAMERAS] or None
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
        stem = f"{safe_name}_{layout}_{job_id}"
        out_final = settings.exports_dir / f"{stem}.mp4"
        # 写到 .part.mp4，成功后再改名：失败/取消的半成品不会以成品名字留在 /exports 里
        out_part = settings.exports_dir / f"{stem}.part.mp4"
        ass_path_file = settings.exports_dir / f"{stem}.ass"

        report: dict = {"job_id": job_id, "layout": layout, "quality": quality,
                        "overlay": "off", "warnings": []}
        lists: list[str] = []
        ass_path: Path | None = None
        try:
            # 每个参与合成的文件都先探测：读不出来的（截断、加密）不交给 ffmpeg，
            # 实际时长也从这里来。结果有缓存，第二次导出同一事件是瞬时的。
            sel = composer._ordered_cameras(event, cameras)
            paths = [base / seg.cameras[c] for seg in event.segments for c in sel if c in seg.cameras]
            probes = await probe.probe_many(paths)
            self._checkpoint(job_id)
            encoder = await asyncio.to_thread(hwaccel.detect)
            report["encoder"] = encoder.name + (f" ({encoder.mode})" if encoder.mode else "")

            def plan_with(ass: Path | None) -> composer.ComposePlan:
                return composer.build_plan(
                    event, base, ass, out_part,
                    cameras=cameras, layout=layout, quality=quality,
                    start_offset=start_offset, end_offset=end_offset,
                    only_segment=only_segment, probes=probes, encoder=encoder,
                )

            plan = await asyncio.to_thread(plan_with, None)
            lists = list(plan.concat_lists)
            report.update(dropped=plan.dropped, short_clips=plan.short_clips,
                          duration=plan.duration, cameras=plan.cameras)
            if plan.dropped:
                report["warnings"].append(
                    f"{len(plan.dropped)} segment(s) skipped: a selected camera is missing or unreadable")

            if with_overlay:
                if not await asyncio.to_thread(ff.has_filter, "ass"):
                    # 没有 libass 就导出不带叠加层的版本，并且说清楚 —— 以前是整个任务失败
                    report["overlay"] = "unavailable"
                    report["warnings"].append(
                        "this ffmpeg has no libass (the 'ass' filter); exported without the telemetry overlay")
                else:
                    ass_text = await self._overlay(job_id, params, plan, base, lang, up, report)
                    if ass_text:
                        await asyncio.to_thread(ass_path_file.write_text, ass_text, encoding="utf-8")
                        ass_path = ass_path_file
                        report["overlay"] = "burned"
                    else:
                        report["overlay"] = "no_telemetry"
                        report["warnings"].append("no telemetry for this time range; exported without overlay")

            self._checkpoint(job_id)
            if ass_path is not None:
                plan = await asyncio.to_thread(plan_with, ass_path)
                lists = sorted(set(lists) | set(plan.concat_lists))

            await self._encode(job_id, plan, out_part, report)

            # rc 0 不等于有画面：确认输出真的有内容
            size = await asyncio.to_thread(_size, out_part)
            info = await probe.probe(out_part) if size else {"ok": False}
            if size < _MIN_OUTPUT_BYTES or not info.get("ok") or not info.get("duration"):
                raise ValueError(  # noqa: TRY301 - 外层统一清理半成品、记报告
                    f"ffmpeg finished but the output is empty ({size} bytes); "
                    "the selected range probably has no frames")
            out_part.replace(out_final)
            report["output_seconds"] = info.get("duration")
            await store.update_job(
                job_id, status="done", progress=100,
                output=f"/exports/{out_final.name}",
                error=f"units:{up['length']}/{up['temp']} lang:{lang}",
            )
        except BaseException as e:
            report["error"] = "cancelled" if isinstance(e, JobCancelled) else str(e)[:500]
            with contextlib.suppress(OSError):
                out_part.unlink()
            raise
        finally:
            await asyncio.to_thread(_cleanup, [*lists, *([str(ass_path)] if ass_path else [])])
            await save_report(job_id, report)

    async def _overlay(self, job_id: int, params: dict, plan: composer.ComposePlan,
                       base: Path, lang: str, up: dict, report: dict) -> str | None:
        """生成 ASS 文本；没有任何遥测时返回 None。"""
        step = 0.5
        car_id = params.get("car_id") or await self._default_car_id()
        clk = await telemetry.clock_offset(int(car_id)) if car_id is not None else 0.0
        tz = local_tz()

        # 1) TeslaMate positions 轨道（电量/功率/海拔/温度等，~1Hz）。
        #    分段之间可能有间隙、也可能被跳过，所以逐段做"输出视频时间 ↔ 墙钟"
        #    映射，不能拿事件起点线性外推。
        positions: list[dict] = []
        if car_id is not None:
            try:
                last = plan.segments[-1]
                positions = await telemetry.fetch_positions(
                    int(car_id),
                    local_naive_to_utc(plan.segments[0].start) + timedelta(seconds=clk),
                    local_naive_to_utc(last.start)
                    + timedelta(seconds=plan.segment_durations[-1] + clk),
                )
            except db.DbUnavailable:
                log.warning("TeslaMate DB unavailable; overlay will be SEI-only")
                report["warnings"].append("TeslaMate database unavailable; overlay is SEI-only")
        self._checkpoint(job_id)

        # 2) SEI 逐帧遥测轨道（档位/方向盘/踏板/AP，固件 2025.44.25+）
        sei_track: list[dict] = []
        sei_cam = "front" if "front" in plan.cameras else plan.cameras[0]
        for k, seg in enumerate(plan.segments):
            rel = seg.cameras.get(sei_cam)
            if not rel:
                continue
            v0 = plan.segment_out_starts[k]
            for s in await asyncio.to_thread(sei.extract, base / rel):
                t_video = v0 + s["t"]
                if 0 <= t_video <= plan.duration:
                    sei_track.append({**s, "t": round(t_video, 2)})
            self._checkpoint(job_id)

        if not positions and not sei_track:
            return None

        # 3) 逐段的时间步：每一步都带上它自己的墙钟（跨分段间隙也对）
        overlay: list[dict] = []
        for k, seg in enumerate(plan.segments):
            v0 = plan.segment_out_starts[k]
            v_lo = max(v0, 0.0)
            v_hi = min(v0 + plan.segment_durations[k], plan.duration)
            if v_hi <= v_lo:
                continue
            n = max(int((v_hi - v_lo) / step + 1e-9), 1)
            wall0 = local_naive_to_utc(seg.start).timestamp() + clk + (v_lo - v0)
            if positions:
                steps = telemetry.resample(positions, wall0, wall0 + (n - 1) * step, step=step)
            else:
                steps = [{"t": i * step} for i in range(n)]
            for s in steps[:n]:
                wall = wall0 + s["t"]
                s["t"] = round(v_lo + s["t"], 3)
                s["clock"] = datetime.fromtimestamp(wall, tz).strftime("%Y-%m-%d %H:%M:%S")
                overlay.append(s)
        overlay.sort(key=lambda s: s["t"])
        clip_start_local = plan.segments[0].start + timedelta(seconds=plan.first_segment_skip + clk)
        return telemetry.build_ass(
            overlay, plan.width, plan.height, clip_start_local, step=step, lang=lang,
            sei=sei_track or None, prefs=up,
        )

    async def _encode(self, job_id: int, plan: composer.ComposePlan, out_part: Path,
                      report: dict) -> None:
        log.info("compose job %s: %s", job_id, " ".join(plan.cmd[:24]) + " ...")
        proc = await asyncio.create_subprocess_exec(
            *plan.cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._procs[job_id] = proc
        if self._check_cancelled(job_id):
            # 取消恰好落在 create_subprocess_exec 期间：那时 _procs 里还没有它
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        stderr_tail: list[bytes] = []

        async def read_stderr():
            assert proc.stderr
            async for line in proc.stderr:
                stderr_tail.append(line)
                if len(stderr_tail) > 60:
                    stderr_tail.pop(0)

        stderr_task = asyncio.create_task(read_stderr())
        stalled = False
        try:
            assert proc.stdout
            last_pct = -1.0
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=STALL_TIMEOUT_S)
                except asyncio.TimeoutError:
                    stalled = True
                    break
                if not line:
                    break
                m = _OUT_TIME_RE.search(line)
                if m:
                    pct = min(int(m.group(1)) / 1e6 / max(plan.duration, 0.1) * 100, 99.9)
                    if pct - last_pct >= 1:
                        last_pct = pct
                        await store.update_job(job_id, progress=round(pct, 1))
            if not stalled:
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

        if proc.returncode == 0 and not stalled:
            return
        if self._check_cancelled(job_id):
            raise JobCancelled
        err = b"".join(stderr_tail[-25:]).decode("utf-8", "replace")
        if stalled:
            raise RuntimeError(f"ffmpeg made no progress for {STALL_TIMEOUT_S}s and was stopped\n{err}")
        if proc.returncode in (-9, 137):
            # 没人点取消却被 SIGKILL：基本就是内存不够被 OOM killer 杀了
            raise RuntimeError(
                "ffmpeg was killed by the system (SIGKILL), most likely out of memory — "
                f"try a smaller layout or fewer cameras\n{err}")
        raise RuntimeError(err or f"ffmpeg exited with code {proc.returncode}")


jobs = JobManager()
