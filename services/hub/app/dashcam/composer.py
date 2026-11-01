"""ffmpeg 多机位合成命令构建。

布局:
  grid       —— 所有机位等分网格 (2 列，5-6 机位时 3 列)
  front_pip  —— 前摄全屏，其余机位画中画排在底部
  back_pip   —— 后摄全屏（倒车/后方事件），其余画中画
  vertical   —— 9:16 竖版，主画面在上、其余竖排在下（手机/社交平台）
  wide       —— 全景条，所有机位横向一字排开（事故复盘一眼扫完）

每个机位一个 concat demuxer 输入（列表文件里逐段写 `duration`），再统一
trim / 缩放，最后 xstack/overlay 拼合，叠加 ASS 遥测字幕，输出 H.264 mp4。

为什么不用 concat 滤镜：它要求每一段都是一个独立的 `-i`，6 机位 45 分钟就是
270 个输入，同时打开 270 个文件 —— macOS launchd 默认的 256 个 fd 直接
"Too many open files"，Docker 的 1024 撑到 170 分钟也一样。demuxer 一路一个
输入，按顺序打开文件。

时间轴：每段用**实测时长**（ffprobe），而不是假设 60 秒。一路机位的某段比
其它机位短（被截断）时，列表里仍按该段的实际长度写 `duration`，各机位在
下一段开头重新对齐，不会从此一路错开。

读不出来的片段（没有 moov 的截断文件、加密文件）不交给 ffmpeg —— 以前一个
坏文件就让整次导出 rc 183 失败。这一分钟被跳过，原因写进 plan.dropped。
"""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import ff
from .scanner import CAMERAS, NOMINAL_SEGMENT_S, Event

CAMERA_NAMES_ZH = {
    "front": "前", "back": "后",
    "left_repeater": "左后视", "right_repeater": "右后视",
    "left_pillar": "左B柱", "right_pillar": "右B柱",
}

QUALITY = {
    "high": ("18", "medium"),
    "standard": ("23", "veryfast"),
    "fast": ("27", "ultrafast"),
}

# 一路机位比该段最长的那路短这么多秒以上，报成"片段不完整"
_SHORT_CLIP_S = 1.5


@dataclass
class ComposePlan:
    cmd: list[str]
    width: int
    height: int
    duration: float          # 输出时长（秒）
    cameras: list[str]
    dropped_segments: int    # 跳过的分段数（兼容老字段）
    segments: list           # 实际参与合成的 Segment 列表（按时间序）
    encoder: str = "libx264"  # 实际用的编码器，界面上要说清楚快慢是从哪来的
    # 每个参与合成的分段：它自己的 t=0 落在输出视频的第几秒（第一段可能为负）
    segment_out_starts: list[float] = field(default_factory=list)
    segment_durations: list[float] = field(default_factory=list)
    # 首段里从第几秒开始取
    first_segment_skip: float = 0.0
    dropped: list[dict] = field(default_factory=list)
    short_clips: list[dict] = field(default_factory=list)
    concat_lists: list[str] = field(default_factory=list)   # 需要清理的列表文件
    overlay: bool = False


def _ordered_cameras(event: Event, requested: list[str] | None) -> list[str]:
    available = {c for s in event.segments for c in s.cameras}
    wanted = requested or CAMERAS
    return [c for c in CAMERAS if c in available and c in wanted]


def _info(probes: dict | None, path: Path) -> dict | None:
    if probes is None:
        return None
    return probes.get(path) or probes.get(str(path))


def event_timeline(event: Event, base: Path, probes: dict | None) -> tuple[list[float], list[float]]:
    """整个事件的时间轴：(每段起点偏移, 每段时长)，段与段首尾相接。

    这是播放器看到的时间轴（/api/dashcam/event 的 segments[].offset），
    start_offset / end_offset 都按它解释 —— 所以有分段被跳过时，区间
    导出依然对应用户在播放器里选的那一段，不会整体前移。
    """
    offsets, durs = [], []
    t = 0.0
    for seg in event.segments:
        ds = []
        for rel in seg.cameras.values():
            info = _info(probes, base / rel)
            if info and info.get("duration"):
                ds.append(info["duration"])
        d = max(ds) if ds else (seg.duration or NOMINAL_SEGMENT_S)
        offsets.append(t)
        durs.append(d)
        t += d
    return offsets, durs


def build_plan(
    event: Event,
    base: Path,
    ass_path: Path | None,
    out_path: Path,
    cameras: list[str] | None = None,
    layout: str = "grid",
    quality: str = "standard",
    start_offset: float = 0.0,
    end_offset: float | None = None,
    only_segment: str | None = None,
    probes: dict | None = None,
    work_dir: Path | None = None,
    encoder=None,
) -> ComposePlan:
    """probes: {文件路径: probe 结果}（由 jobs 预先并发探测好）。None = 不知道，按名义时长算。

    only_segment: 分段起始时间的 ISO 字符串——只合成该分段。
    encoder: hwaccel.Encoder；None = 用 hwaccel.detect() 的结果（会起子进程，别在事件循环里调）。
    """
    cams = _ordered_cameras(event, cameras)
    if not cams:
        raise ValueError("no cameras available in this event")

    offsets, durs = event_timeline(event, base, probes)
    total = (offsets[-1] + durs[-1]) if offsets else 0.0

    idx = list(range(len(event.segments)))
    if only_segment:
        idx = [i for i in idx if event.segments[i].start.isoformat() == only_segment]
        if not idx:
            raise ValueError(f"segment {only_segment} not found")
        win_lo, win_hi = offsets[idx[0]], offsets[idx[0]] + durs[idx[0]]
    else:
        start_offset = max(float(start_offset or 0.0), 0.0)
        if start_offset >= total:
            raise ValueError(
                f"start_offset {start_offset:.1f}s is beyond the end of the event ({total:.1f}s)")
        win_lo = start_offset
        win_hi = min(float(end_offset), total) if end_offset else total
        if win_hi <= win_lo:
            raise ValueError("end_offset must be greater than start_offset")

    # 只用所有已选机位都齐、而且都读得出来的分段，保证时间轴对齐
    kept: list[int] = []
    dropped: list[dict] = []
    short: list[dict] = []
    seg_dur: dict[int, float] = {}
    for i in idx:
        seg = event.segments[i]
        if offsets[i] + durs[i] <= win_lo or offsets[i] >= win_hi:
            continue  # 不在所选区间里
        missing = [c for c in cams if c not in seg.cameras]
        unreadable = []
        cam_durs = {}
        for c in cams:
            if c in missing:
                continue
            info = _info(probes, base / seg.cameras[c])
            if info is not None and not info.get("ok"):
                unreadable.append({"camera": c, "path": seg.cameras[c],
                                   "reason": info.get("reason") or "unreadable"})
            elif info is not None and info.get("duration"):
                cam_durs[c] = info["duration"]
        if missing or unreadable:
            dropped.append({"segment": seg.start.isoformat(), "missing": missing,
                            "unreadable": unreadable})
            continue
        d = max(cam_durs.values()) if cam_durs else durs[i]
        seg_dur[i] = d
        for c, cd in cam_durs.items():
            if d - cd > _SHORT_CLIP_S:
                short.append({"segment": seg.start.isoformat(), "camera": c,
                              "duration": round(cd, 2), "expected": round(d, 2)})
        kept.append(i)

    if not kept:
        if only_segment:
            raise ValueError(f"segment {only_segment} is missing or unreadable for the selected cameras")
        raise ValueError("no segments in the selected range contain all selected cameras")

    # 输出时间轴：参与合成的分段首尾相接
    out_starts: list[float] = []
    t = 0.0
    for i in kept:
        out_starts.append(t)
        t += seg_dur[i]
    first, last = kept[0], kept[-1]
    trim_lo = max(win_lo - offsets[first], 0.0)
    trim_hi = out_starts[-1] + min(win_hi - offsets[last], seg_dur[last])
    duration = max(trim_hi - trim_lo, 0.04)

    work_dir = work_dir or out_path.parent
    stem = out_path.name.removesuffix(".mp4").removesuffix(".part")
    inputs: list[str] = []
    filters: list[str] = []
    lists: list[str] = []
    cam_streams: dict[str, str] = {}

    for n, cam in enumerate(cams):
        lines = ["ffconcat version 1.0"]
        for i in kept:
            src = os.path.abspath(base / event.segments[i].cameras[cam])
            lines.append(f"file {ff.concat_quote(src)}")
            lines.append(f"duration {seg_dur[i]:.3f}")
        lst = work_dir / f"{stem}.{cam}.ffconcat"
        lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
        lists.append(str(lst))
        inputs += ["-f", "concat", "-safe", "0", "-i", str(lst)]
        trim_out = f"t_{cam}"
        filters.append(
            f"[{n}:v]trim=start={trim_lo:.3f}:end={trim_hi:.3f},"
            f"setpts=PTS-STARTPTS[{trim_out}]"
        )
        cam_streams[cam] = trim_out

    def scale(src: str, dst: str, w: int, h: int) -> None:
        w -= w % 2  # yuv420p 要求偶数尺寸
        h -= h % 2
        filters.append(
            f"[{src}]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30[{dst}]"
        )

    if layout in ("front_pip", "back_pip"):
        main_cam = "front" if layout == "front_pip" else "back"
        if main_cam not in cams:
            main_cam = cams[0]
        width, height = 1920, 1440
        pip_w, pip_h, margin = 464, 348, 12
        bar_h = int(height * 0.105)

        scale(cam_streams[main_cam], "main", width, height)
        others = [c for c in cams if c != main_cam]
        current = "main"
        positions = [
            (margin, height - bar_h - pip_h - margin),
            ((width - pip_w) // 2, height - bar_h - pip_h - margin),
            (width - pip_w - margin, height - bar_h - pip_h - margin),
            (margin, margin),
            (width - pip_w - margin, margin),
        ]
        for i, cam in enumerate(others[: len(positions)]):
            scale(cam_streams[cam], f"pip_{cam}", pip_w, pip_h)
            nxt = f"ov{i}"
            x, y = positions[i]
            filters.append(f"[{current}][pip_{cam}]overlay={x}:{y}[{nxt}]")
            current = nxt
        compose_out = current
    elif layout == "vertical":
        # 9:16 竖版，给手机和社交平台。做法是主画面顶部、其余机位竖着叠 ——
        # 直接把 16:9 塞进 9:16 会剩下两条巨大的黑边，等于把画面缩小到三分之一。
        # 1080x1920 是各平台的通用安全尺寸。
        main_cam = "front" if "front" in cams else cams[0]
        width, height = 1080, 1920
        others = [c for c in cams if c != main_cam][:3]
        if not others:
            # 只有一路时把它铺满，别留一大片黑（以前对同一路 scale 两次，
            # 同一个输出标签绑两遍，单机位竖版必定 "Error binding filtergraph"）
            scale(cam_streams[main_cam], "vmain", width, height)
            compose_out = "vmain"
        else:
            main_h = 810                      # 1080 宽下的 4:3 主画面
            strip_h = (height - main_h) // len(others)
            scale(cam_streams[main_cam], "vmain", width, main_h)
            # 用 pad 把主画面延长成整张画布，而不是拿 color 源当底：
            # color 源默认 25fps，overlay 的输出帧率跟着主输入走，
            # 以前竖版导出因此一律是 25fps
            filters.append(f"[vmain]pad={width}:{height}:0:0:color=black[vc0]")
            current = "vc0"
            for i, cam in enumerate(others):
                scale(cam_streams[cam], f"vs_{cam}", width, strip_h)
                nxt = f"vc{i + 1}"
                filters.append(f"[{current}][vs_{cam}]overlay=0:{main_h + i * strip_h}[{nxt}]")
                current = nxt
            compose_out = current

    elif layout == "wide":
        # 全景条：所有机位横向一字排开，用于"一眼扫过全部视角"。
        # 常见于事故复盘 —— 网格布局要来回看四个角，横排只需要扫一行。
        cell_w, cell_h = 640, 480
        width, height = cell_w * len(cams), cell_h
        for cam in cams:
            scale(cam_streams[cam], f"w_{cam}", cell_w, cell_h)
        if len(cams) == 1:
            compose_out = f"w_{cams[0]}"
        else:
            spec = "|".join(f"{i * cell_w}_0" for i in range(len(cams)))
            refs = "".join(f"[w_{c}]" for c in cams)
            filters.append(f"{refs}xstack=inputs={len(cams)}:layout={spec}:fill=black[wide]")
            compose_out = "wide"

    else:  # grid
        n = len(cams)
        cols = 1 if n == 1 else (2 if n <= 4 else 3)
        rows = math.ceil(n / cols)
        cell_w = 1920 // cols
        cell_h = cell_w * 3 // 4
        width, height = cell_w * cols, cell_h * rows
        for cam in cams:
            scale(cam_streams[cam], f"cell_{cam}", cell_w, cell_h)
        if n == 1:
            compose_out = f"cell_{cams[0]}"
        else:
            layout_spec = "|".join(
                f"{(i % cols) * cell_w}_{(i // cols) * cell_h}" for i in range(n)
            )
            refs = "".join(f"[cell_{c}]" for c in cams)
            filters.append(
                f"{refs}xstack=inputs={n}:layout={layout_spec}:fill=black[stacked]"
            )
            compose_out = "stacked"

    final = compose_out
    if ass_path is not None:
        # argv 直接传给 ffmpeg（无 shell），但 filtergraph 自己要解两层转义
        filters.append(f"[{final}]ass=filename={ff.escape_filter_value(str(ass_path))}[outv]")
        final = "outv"

    crf, preset = QUALITY.get(quality, QUALITY["standard"])
    # 有可用的硬件编码器就用，没有退回 libx264。探测是真编几帧试出来的，
    # 不是看 `ffmpeg -encoders` 那张表 —— 那张表列的是编译进去的，
    # 不是这台机器上能跑的。详见 hwaccel.py。
    if encoder is None:
        from .hwaccel import detect

        encoder = detect()
    if encoder.filter_tail:
        # VAAPI 要先把帧传进显存（format=nv12,hwupload），否则编码器拿不到能用的帧
        filters.append(f"[{final}]{encoder.filter_tail}[encin]")
        final = "encin"

    cmd = [
        ff.ffmpeg(), "-hide_banner", "-nostdin", "-nostats", "-y",
        *encoder.global_args,
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", f"[{final}]",
        "-an",
        "-c:v", encoder.name, *encoder.quality_args(crf, preset, width, height),
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        str(out_path),
    ]
    return ComposePlan(
        cmd=cmd, width=width, height=height, duration=round(duration, 3),
        cameras=cams, dropped_segments=len(dropped), segments=[event.segments[i] for i in kept],
        encoder=encoder.name,
        segment_out_starts=[round(s - trim_lo, 3) for s in out_starts],
        segment_durations=[seg_dur[i] for i in kept],
        first_segment_skip=round(trim_lo, 3),
        dropped=dropped, short_clips=short, concat_lists=lists,
        overlay=ass_path is not None,
    )
