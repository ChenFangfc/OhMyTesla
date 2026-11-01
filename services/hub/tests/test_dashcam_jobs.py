"""合成任务（审计 D9 / D14 / L3 / L4 / L6），真的跑 worker 和 ffmpeg。

  - D9 ：ffmpeg 没有 libass 时导出照常完成、不带叠加层，并在任务报告里说明；
         有 libass 时叠加层真的烧进去（输出和不带叠加层的版本不一样）
  - D14：叠加层的时钟逐段计算 —— 两段之间有 90 秒空档时，第二段显示它自己的时刻
  - L3 ：ffmpeg rc 0 但没有画面的输出判为失败
  - L4 ：失败/取消后不留 .part.mp4、.ffconcat、.ass
  - L6 ：排队中被取消的任务不会被 worker 改回 running；运行中被取消记为 cancelled；
         没人取消却被 SIGKILL 记为失败并提示内存不足

跑法：python3 services/hub/tests/test_dashcam_jobs.py（需要 ffmpeg + libx264 和 hub 运行时依赖）
本机 ffmpeg 没有 libass 时，可用 FFMPEG_BIN=/path/to/ffmpeg-with-libass 覆盖来验证叠加层。
"""

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
TMP = Path(tempfile.mkdtemp(prefix="omt-jobs-"))
ROOT = TMP / "teslacam"
os.environ.update(DATA_DIR=str(TMP / "data"), TESLACAM_ROOT=str(ROOT), TESLACAM_TZ="America/Chicago")
(TMP / "data").mkdir()
ROOT.mkdir()
try:
    import aiosqlite  # noqa: F401
    import pydantic_settings  # noqa: F401
except ImportError:
    print("— skipped: needs the hub runtime (aiosqlite, pydantic-settings); run with tests/run.sh --docker")
    sys.exit(0)
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    print("— skipped: ffmpeg/ffprobe not installed")
    sys.exit(0)

from app.alerts.store import store  # noqa: E402
from app.config import settings  # noqa: E402
from app.dashcam import ff, hwaccel, telemetry  # noqa: E402
from app.dashcam import jobs as jobs_mod
from app.dashcam.localtime import local_naive_to_utc  # noqa: E402

FAILS: list[str] = []
jm = jobs_mod.jobs


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def clip(path: Path, secs: float, size="320x240") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ff.ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size={size}:rate=30:duration={secs}", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(path)], check=True)


def leftovers() -> list[str]:
    return sorted(p.name for p in settings.exports_dir.iterdir()
                  if p.suffix in (".ass", ".ffconcat") or p.name.endswith(".part.mp4"))


async def wait(job_id: int, timeout=120) -> dict:
    for _ in range(timeout * 10):
        j = await store.get_job(job_id)
        if j["status"] in ("done", "failed", "cancelled"):
            j["report"] = (await jobs_mod.get_reports()).get(str(job_id))
            return j
        await asyncio.sleep(0.1)
    raise TimeoutError(job_id)


POSITIONS: list[dict] = []


async def fake_positions(car_id, start_utc, end_utc, pad_s=15):
    return [p for p in POSITIONS if start_utc.timestamp() - pad_s <= p["date"] <= end_utc.timestamp() + pad_s]


async def main():
    await store.open()
    from app.i18n import prefs

    await prefs.set_prefs({"locale": "en"})
    hwaccel.force(hwaccel.Encoder("libx264"))
    telemetry.fetch_positions = fake_positions
    jm._default_car_id = lambda: asyncio.sleep(0, result=1)
    has_ass = ff.has_filter("ass")

    # 事件 A：两段，back 的第二段没有 moov
    ev_a = ROOT / "SavedClips/2026-10-01_10-02-00"
    for m in ("10-00-00", "10-01-00"):
        for cam in ("front", "back"):
            clip(ev_a / f"2026-10-01_{m}-{cam}.mp4", 2)
    bad = ev_a / "2026-10-01_10-01-00-back.mp4"
    bad.write_bytes(bad.read_bytes()[:3000])
    # 事件 B：RecentClips，两段之间空 90 秒
    for m in ("09-00-00", "09-01-30"):
        clip(ROOT / f"RecentClips/2026-10-01_{m}-front.mp4", 2)
    # 事件 C：一段较长的片段，用来测运行中取消 / 被杀
    clip(ROOT / "SavedClips/2026-10-01_11-00-00/2026-10-01_10-59-00-front.mp4", 20, "1280x960")

    jm.start()
    try:
        print("D8/D9 截断片段 + 没有 libass")
        real_has = ff.has_filter
        ff.has_filter = lambda name: False if name == "ass" else real_has(name)
        j = await wait(await jm.submit("SavedClips/2026-10-01_10-02-00", {"layout": "grid", "quality": "fast"}))
        ff.has_filter = real_has
        rep = j["report"] or {}
        chk("任务完成", j["status"] == "done", j["error"])
        chk("报告：叠加层不可用并说明原因", rep.get("overlay") == "unavailable"
            and any("libass" in w for w in rep.get("warnings", [])), rep.get("warnings"))
        chk("报告：跳过的分段和原因", len(rep.get("dropped", [])) == 1
            and rep["dropped"][0]["unreadable"][0]["camera"] == "back", rep.get("dropped"))
        chk("没有残留文件", not leftovers(), leftovers())

        POSITIONS[:] = []
        t0 = local_naive_to_utc(datetime(2026, 10, 1, 10, 0)).timestamp()   # noqa: DTZ001 - 车机本地时间
        for k in range(200):
            POSITIONS.append({"date": t0 + k, "speed": 50.0 + k % 7, "power": 12.0, "battery_level": 80.0,
                              "usable_battery_level": 79.0, "elevation": 200.0, "odometer": 12345.0,
                              "outside_temp": 18.0, "inside_temp": 21.0, "rated_battery_range_km": 300.0,
                              "latitude": 40.1 + k * 1e-4, "longitude": -98.2 + k * 1e-4})
        if has_ass:
            print("D9 有 libass：叠加层真的烧进去")
            j1 = await wait(await jm.submit("SavedClips/2026-10-01_10-02-00",
                                            {"layout": "grid", "quality": "fast", "segment": "2026-10-01T10:00:00"}))
            j2 = await wait(await jm.submit("SavedClips/2026-10-01_10-02-00",
                                            {"layout": "grid", "quality": "fast", "overlay": False,
                                             "segment": "2026-10-01T10:00:00"}))
            chk("带叠加层的任务完成、报告 burned", j1["status"] == "done"
                and (j1["report"] or {}).get("overlay") == "burned", (j1["error"] or "")[-200:])
            frames = []
            for jj in (j1, j2):
                out = settings.exports_dir / Path(jj["output"] or "x").name
                r = subprocess.run([ff.ffmpeg(), "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1",
                                    "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
                frames.append(r.stdout)
            diff = sum(abs(a - b) for a, b in zip(frames[0], frames[1], strict=False)) / max(len(frames[0]), 1)
            chk("叠加层改变了画面", len(frames[0]) > 0 and diff > 1.0, f"mean |Δ| = {diff:.2f}")
            chk("没有残留 .ass", not leftovers(), leftovers())
        else:
            print("— 本机 ffmpeg 没有 libass，跳过烧录验证（容器里会跑；或用 FFMPEG_BIN 指定带 libass 的 ffmpeg）")

        print("D14 分段之间有空档时的叠加层时钟")
        evb = await jobs_mod.scanner.aget("RecentClips/2026-10-01_09-00-00")
        from app.dashcam import composer, probe

        paths = [ROOT / r for s in evb.segments for r in s.cameras.values()]
        plan = composer.build_plan(evb, ROOT, None, TMP / "gap.mp4", probes=await probe.probe_many(paths),
                                   encoder=hwaccel.Encoder("libx264"))
        POSITIONS[:] = []
        tb = local_naive_to_utc(datetime(2026, 10, 1, 9, 0)).timestamp()   # noqa: DTZ001
        POSITIONS.extend({"date": tb + k, "speed": 30.0, "battery_level": 70.0} | dict.fromkeys(
            ("power", "usable_battery_level", "elevation", "odometer", "outside_temp", "inside_temp",
             "rated_battery_range_km", "latitude", "longitude")) for k in range(200))
        ass = await jm._overlay(0, {"car_id": 1}, plan, ROOT, "en", await prefs.get(), {"warnings": []})
        chk("第二段（输出 2 秒处）显示 09:01:30，而不是线性外推的 09:00:02",
            ass is not None and ",0:00:02.00,0:00:02.50,Small,,0,0,0,,{\\an9" in ass
            and "2026-10-01 09:01:30" in ass and "2026-10-01 09:00:02" not in ass)

        print("L3 rc 0 但没有画面")
        real_encode = jm._encode

        async def no_output(job_id, plan, out_part, report):
            return None

        jm._encode = no_output
        j = await wait(await jm.submit("SavedClips/2026-10-01_10-02-00", {"overlay": False}))
        jm._encode = real_encode
        chk("判为失败并说明输出是空的", j["status"] == "failed" and "empty" in (j["error"] or ""), j["error"])

        print("L4 失败不留半成品")
        hwaccel.force(hwaccel.Encoder("h264_definitely_missing"))
        j = await wait(await jm.submit("SavedClips/2026-10-01_10-02-00", {"overlay": False}))
        hwaccel.force(hwaccel.Encoder("libx264"))
        chk("编码器不存在 → failed，带 ffmpeg 的报错", j["status"] == "failed" and "h264_definitely_missing"
            in (j["error"] or ""), (j["error"] or "")[-120:])
        chk("没有残留 .part.mp4 / .ffconcat", not leftovers(), leftovers())

        print("L6 取消与被杀")
        # 排队中取消：先占住 worker，再排一个，再取消它
        busy = await jm.submit("SavedClips/2026-10-01_11-00-00", {"overlay": False, "quality": "high"})
        queued = await jm.submit("SavedClips/2026-10-01_10-02-00", {"overlay": False})
        chk("排队中的任务可以取消", await jm.cancel(queued))
        for _ in range(100):
            if busy in jm._procs:
                break
            await asyncio.sleep(0.05)
        chk("运行中的任务可以取消", await jm.cancel(busy))
        jb, jq = await wait(busy), await wait(queued)
        chk("运行中取消 → cancelled（不是 failed）", jb["status"] == "cancelled", jb["status"])
        chk("排队中取消后不会被改回 running 再跑", jq["status"] == "cancelled" and not jq["output"], jq["status"])
        chk("取消后没有残留", not leftovers(), leftovers())

        victim = await jm.submit("SavedClips/2026-10-01_11-00-00", {"overlay": False, "quality": "high"})
        for _ in range(200):
            if victim in jm._procs:
                break
            await asyncio.sleep(0.05)
        os.kill(jm._procs[victim].pid, signal.SIGKILL)   # 模拟 OOM killer
        jv = await wait(victim)
        chk("没人取消却被 SIGKILL → failed 并提示内存", jv["status"] == "failed"
            and "out of memory" in (jv["error"] or ""), (jv["error"] or "")[:120])
    finally:
        await jm.stop()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
    print()
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    sys.exit(1 if FAILS else 0)
