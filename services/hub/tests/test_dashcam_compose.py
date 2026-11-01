"""合成命令（审计 D2 / D8 / D10 / D12 / D14 / D18 / L3 / L5），用真的 ffmpeg 跑。

  - D10：每路机位一个 concat demuxer 输入。120 个片段在 64 个 fd 的限制下照样能合成
         （老的"每段一个 -i"在同样限制下必然 Too many open files —— 一并演示）
  - D8 ：没有 moov 的截断片段不交给 ffmpeg，这一分钟跳过并写明原因，导出照常完成
  - D14：时间轴用实测时长；一路片段被截短时各机位在下一段重新对齐
  - D12：竖版单机位能合成，而且是 30fps
  - D18：路径里有 : ' [ ] , ; \\ 也能被 filtergraph 正确解析
  - L3 ：起点越过片尾直接报错，而不是产出空壳
  - D2 / L5：各家编码器的质量参数（VideoToolbox -q:v 是 1-100、QSV 没有 ultrafast、
         VAAPI 要 hwupload）

跑法：python3 services/hub/tests/test_dashcam_compose.py（需要 ffmpeg + libx264 和 hub 运行时依赖）
"""

import asyncio
import os
import resource
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
TMP = Path(tempfile.mkdtemp(prefix="omt-compose-"))
os.environ.update(DATA_DIR=str(TMP / "data"), TESLACAM_ROOT=str(TMP / "teslacam"))
(TMP / "data").mkdir()
try:
    import pydantic_settings  # noqa: F401
except ImportError:
    print("— skipped: needs the hub runtime (pydantic-settings); run with tests/run.sh --docker")
    sys.exit(0)
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    print("— skipped: ffmpeg/ffprobe not installed")
    sys.exit(0)

from app.dashcam import composer, ff, hwaccel, probe  # noqa: E402
from app.dashcam.scanner import Event, Segment  # noqa: E402

FAILS: list[str] = []
X264 = hwaccel.Encoder("libx264")


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def clip(path: Path, secs: float, size: str = "320x240") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ff.ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size={size}:rate=30:duration={secs}", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def ffprobe(path: Path, entries: str) -> list[str]:
    r = subprocess.run([ff.ffprobe(), "-v", "error", "-count_frames", "-select_streams", "v:0",
                        "-show_entries", entries, "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return r.stdout.replace("\n", ",").strip(",").split(",")


def event(base: Path, spec: list[dict[str, float]], name="SavedClips/2026-10-01_10-10-00") -> Event:
    """spec：每段 {机位: 时长}。时长 0 = 截断成没有 moov 的坏文件。"""
    cache: dict[float, Path] = {}
    segs = []
    t0 = datetime(2026, 10, 1, 10, 0)
    for i, cams in enumerate(spec):
        start = t0 + timedelta(minutes=i)
        seg = Segment(start=start)
        for cam, secs in cams.items():
            rel = f"{name}/{start:%Y-%m-%d_%H-%M-%S}-{cam}.mp4"
            dst = base / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if secs == 0:
                src = cache.setdefault(2.0, clip(TMP / "src" / "2.0.mp4", 2.0))
                dst.write_bytes(src.read_bytes()[: src.stat().st_size // 2])   # 截掉后半段（含 moov）
            else:
                src = cache.get(secs) or cache.setdefault(secs, clip(TMP / "src" / f"{secs}.mp4", secs))
                shutil.copyfile(src, dst)
            seg.cameras[cam] = rel
        segs.append(seg)
    return Event(id=name, type="saved", timestamp=segs[0].start, segments=segs)


def plan_for(ev: Event, base: Path, out: Path, **kw) -> composer.ComposePlan:
    paths = [base / rel for s in ev.segments for rel in s.cameras.values()]
    probes = asyncio.run(probe.probe_many(paths))
    return composer.build_plan(ev, base, None, out, quality="fast", probes=probes, encoder=X264, **kw)


def run(plan: composer.ComposePlan, nofile: int | None = None) -> subprocess.CompletedProcess:
    pre = (lambda: resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))) if nofile else None
    return subprocess.run(plan.cmd, capture_output=True, timeout=600, preexec_fn=pre)


def test_fd_limit():
    print("D10 打开文件数")
    base = TMP / "fd"
    cams = ("front", "back", "left_repeater", "right_repeater")
    ev = event(base, [dict.fromkeys(cams, 0.5) for _ in range(30)])
    out = TMP / "fd.mp4"
    plan = plan_for(ev, base, out)
    n_inputs = plan.cmd.count("-i")
    chk("120 个片段只有 4 个 -i", n_inputs == 4, n_inputs)
    r = run(plan, nofile=64)
    chk("RLIMIT_NOFILE=64 下合成成功", r.returncode == 0, "" if r.returncode == 0 else r.stderr.decode(errors="replace")[-200:])
    dur = float(ffprobe(out, "format=duration")[0] or 0)
    chk("输出 15 秒（30 × 0.5）", abs(dur - 15.0) < 0.2, dur)
    # 老做法：每段每路一个 -i + concat 滤镜。同样的限制下必然失败 —— 证明这个限制是生效的
    old = ["ffmpeg", "-v", "error", "-y"]
    for cam in cams:
        for s in ev.segments:
            old += ["-i", str(base / s.cameras[cam])]
    graph = ";".join(
        "".join(f"[{k * 30 + i}:v]" for i in range(30)) + f"concat=n=30:v=1:a=0[c{k}]" for k in range(4))
    old += ["-filter_complex", graph + ";[c0][c1][c2][c3]hstack=inputs=4[o]", "-map", "[o]",
            "-c:v", "libx264", "-preset", "ultrafast", str(TMP / "old.mp4")]
    r_old = subprocess.run(old, capture_output=True, timeout=300,
                           preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64)))
    chk("对照：老做法在同样限制下失败", r_old.returncode != 0,
        (r_old.stderr.decode(errors="replace").strip().splitlines() or ["rc=0"])[-1][:100])


def test_truncated_and_durations():
    print("D8 / D14 截断片段与真实时长")
    base = TMP / "trunc"
    # 段 0：两路 2 秒；段 1：front 3 秒、back 只有 1 秒（被截短但读得出）；
    # 段 2：back 没有 moov；段 3：两路 2 秒。事件时间轴 = [0,2) [2,5) [5,7) [7,9)
    ev = event(base, [{"front": 2, "back": 2}, {"front": 3, "back": 1},
                      {"front": 2, "back": 0}, {"front": 2, "back": 2}])
    out = TMP / "trunc.mp4"
    plan = plan_for(ev, base, out)
    chk("读不出来的那一分钟被跳过、写明原因",
        len(plan.dropped) == 1 and plan.dropped[0]["unreadable"][0]["camera"] == "back"
        and plan.dropped[0]["segment"] == "2026-10-01T10:02:00", plan.dropped)
    chk("截短的片段被报出来", [(s["camera"], s["duration"]) for s in plan.short_clips] == [("back", 1.0)],
        plan.short_clips)
    chk("时间轴按实测时长（2+3+2 秒，不是 3×60）", abs(plan.duration - 7.0) < 0.05
        and plan.segment_out_starts == [0.0, 2.0, 5.0], (plan.duration, plan.segment_out_starts))
    r = run(plan)
    chk("坏文件不再让整次导出失败", r.returncode == 0, "" if r.returncode == 0 else r.stderr.decode(errors="replace")[-200:])
    dur = float(ffprobe(out, "format=duration")[0] or 0)
    chk("输出时长 7 秒", abs(dur - 7.0) < 0.15, dur)
    # 截短的 back 之后两路要重新对齐：back 的下一段从 t=5.0 开始，而不是紧接着 3.0
    lst = next(p for p in plan.concat_lists if p.endswith(".back.ffconcat"))
    pts = subprocess.run([ff.ffprobe(), "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
                          "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0"],
                         capture_output=True, text=True).stdout.split()
    starts = sorted(float(x) for x in pts if x)
    gap = next((b for a, b in zip(starts, starts[1:], strict=False) if b - a > 0.5), None)
    chk("截短片段之后的那段准时从 5.0 秒开始（不往前错位）", gap is not None and abs(gap - 5.0) < 0.05, gap)

    w = plan_for(ev, base, TMP / "win.mp4", start_offset=1.0, end_offset=5.0)
    chk("区间 1-5 秒：输出 4 秒、首段跳过 1 秒", abs(w.duration - 4.0) < 0.01 and w.first_segment_skip == 1.0,
        (w.duration, w.first_segment_skip))
    w2 = plan_for(ev, base, TMP / "win2.mp4", start_offset=1.0, end_offset=8.0)
    chk("区间跨过被跳过的那一分钟：1+3+1 = 5 秒，按播放器的时间轴解释",
        abs(w2.duration - 5.0) < 0.01 and len(w2.dropped) == 1, (w2.duration, len(w2.dropped)))
    r = run(w2)
    d2 = float(ffprobe(TMP / "win2.mp4", "format=duration")[0] or 0) if r.returncode == 0 else 0
    chk("  而且 ffmpeg 真的输出 5 秒", abs(d2 - 5.0) < 0.15, d2)
    try:
        plan_for(ev, base, TMP / "x.mp4", start_offset=500)
        ok = False
    except ValueError as e:
        ok = "beyond the end" in str(e)
    chk("L3 起点越过片尾直接报错", ok)


def test_vertical_single_camera():
    print("D12 竖版")
    base = TMP / "vert"
    ev = event(base, [{"front": 2, "back": 2}, {"front": 2, "back": 2}])
    for cams, label in ((["front"], "单机位"), (None, "双机位")):
        out = TMP / f"vert_{label}.mp4"
        plan = plan_for(ev, base, out, layout="vertical", cameras=cams)
        r = run(plan)
        info = ffprobe(out, "stream=width,height,r_frame_rate,nb_read_frames") if r.returncode == 0 else []
        chk(f"竖版{label}：1080x1920、30fps、120 帧", info[:4] == ["1080", "1920", "30/1", "120"],
            info or r.stderr.decode(errors="replace")[-150:])


def test_filter_escaping():
    print("D18 filtergraph 转义")
    src = clip(TMP / "src" / "tiny.mp4", 0.2, "64x48")
    for name in ("co:lon", "O'Brien", "a,b[c];d", "back\\slash", "mix: it's [a,b]; \\x"):
        d = TMP / "esc" / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / "x.mp4"
        shutil.copyfile(src, f)
        # movie 滤镜和 ass 滤镜用同一套参数解析，本机没有 libass 也能验证
        r = subprocess.run([ff.ffmpeg(), "-v", "error", "-y", "-filter_complex",
                            f"movie=filename={ff.escape_filter_value(str(f))}[v]", "-map", "[v]",
                            "-frames:v", "1", "-f", "null", "-"], capture_output=True)
        chk(f"路径 {name!r}", r.returncode == 0, r.stderr.decode(errors="replace")[-120:])


def test_encoder_args():
    print("D2 / L5 编码器参数")
    vt = hwaccel.Encoder("h264_videotoolbox", mode="quality")
    qs = {q: int(vt.quality_args(crf, p)[1]) for q, (crf, p) in composer.QUALITY.items()}
    chk("VideoToolbox -q:v 落在 1-100 量程（以前是 0.5 → 取整成 0）",
        all(20 <= v <= 90 for v in qs.values()) and qs["high"] > qs["standard"] > qs["fast"], qs)
    vtb = hwaccel.Encoder("h264_videotoolbox", mode="bitrate").quality_args("23", "veryfast", 1920, 1440)
    chk("Intel Mac 走码率模式", vtb[0] == "-b:v" and vtb[1].endswith("k"), vtb)
    qsv = hwaccel.Encoder("h264_qsv", [], "format=nv12")
    chk("QSV 不会拿到 ultrafast", "ultrafast" not in qsv.quality_args("27", "ultrafast"),
        qsv.quality_args("27", "ultrafast"))
    base = TMP / "enc"
    ev = event(base, [{"front": 0.5}])
    va = hwaccel.Encoder("h264_vaapi", ["-vaapi_device", "/dev/dri/renderD128"], "format=nv12,hwupload")
    paths = [base / rel for s in ev.segments for rel in s.cameras.values()]
    plan = composer.build_plan(ev, base, None, TMP / "va.mp4", probes=asyncio.run(probe.probe_many(paths)),
                               encoder=va)
    graph = plan.cmd[plan.cmd.index("-filter_complex") + 1]
    chk("VAAPI 合成命令里有 hwupload（和探测时一致）", graph.endswith("format=nv12,hwupload[encin]")
        and plan.cmd[plan.cmd.index("-map") + 1] == "[encin]", graph[-60:])
    chk("VAAPI 设备参数在输入之前", plan.cmd.index("-vaapi_device") < plan.cmd.index("-i"))


if __name__ == "__main__":
    test_fd_limit()
    test_truncated_and_durations()
    test_vertical_single_camera()
    test_filter_escaping()
    test_encoder_args()
    print()
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    sys.exit(1 if FAILS else 0)
