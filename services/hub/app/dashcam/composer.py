"""ffmpeg 多机位合成命令构建。

布局:
  grid       —— 所有机位等分网格 (2 列，5-6 机位时 3 列)
  front_pip  —— 前摄全屏，其余机位画中画排在底部
  back_pip   —— 后摄全屏（倒车/后方事件），其余画中画

每个机位: 各 1 分钟分段先 concat，再统一 trim / 缩放，最后 xstack/overlay
拼合，叠加 ASS 遥测字幕，输出 H.264 mp4。
"""

import math
from dataclasses import dataclass
from pathlib import Path

from .scanner import CAMERAS, Event

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


@dataclass
class ComposePlan:
    cmd: list[str]
    width: int
    height: int
    duration: float          # 预估输出时长（秒）
    cameras: list[str]
    dropped_segments: int
    segments: list           # 实际参与合成的 Segment 列表（按时间序）
    encoder: str = "libx264"  # 实际用的编码器，界面上要说清楚快慢是从哪来的


def _ordered_cameras(event: Event, requested: list[str] | None) -> list[str]:
    available = {c for s in event.segments for c in s.cameras}
    wanted = requested or CAMERAS
    return [c for c in CAMERAS if c in available and c in wanted]


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
) -> ComposePlan:
    """only_segment: 分段起始时间的 ISO 字符串——只合成该分段。

    与 start_offset 不同，它不受"掉段后时间轴压缩"影响，是前端
    "仅当前分段"导出的可靠定位方式。
    """
    cams = _ordered_cameras(event, cameras)
    if not cams:
        raise ValueError("no cameras available in this event")

    # 只使用所有已选机位齐备的分段，保证时间轴对齐
    segs = [s for s in event.segments if all(c in s.cameras for c in cams)]
    dropped = len(event.segments) - len(segs)
    if only_segment:
        segs = [s for s in segs if s.start.isoformat() == only_segment]
        if not segs:
            raise ValueError(f"segment {only_segment} not found with all selected cameras")
        start_offset, end_offset = 0.0, None
    if not segs:
        raise ValueError("no segments contain all selected cameras")

    total_dur = len(segs) * 60.0
    t_end = min(end_offset, total_dur) if end_offset else total_dur
    duration = max(t_end - start_offset, 1.0)

    inputs: list[str] = []
    filters: list[str] = []
    idx = 0
    cam_streams: dict[str, str] = {}

    for cam in cams:
        n = len(segs)
        refs = []
        for seg in segs:
            inputs += ["-i", str(base / seg.cameras[cam])]
            refs.append(f"[{idx}:v]")
            idx += 1
        concat_out = f"c_{cam}"
        filters.append(f"{''.join(refs)}concat=n={n}:v=1:a=0[{concat_out}]")
        trim_out = f"t_{cam}"
        filters.append(
            f"[{concat_out}]trim=start={start_offset}:end={t_end},"
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

    if ass_path is not None:
        # argv 直接传给 ffmpeg（无 shell），滤镜参数按 ffmpeg 转义规则处理特殊字符
        escaped = str(ass_path)
        for ch in ("\\", ":", ",", "'", "[", "]", ";"):
            escaped = escaped.replace(ch, "\\" + ch)
        filters.append(f"[{compose_out}]ass=filename={escaped}[outv]")
        final = "outv"
    else:
        final = compose_out

    crf, preset = QUALITY.get(quality, QUALITY["standard"])
    # 有可用的硬件编码器就用，没有退回 libx264。探测是真编一帧试出来的，
    # 不是看 `ffmpeg -encoders` 那张表 —— 那张表列的是编译进去的，
    # 不是这台机器上能跑的。详见 hwaccel.py。
    from .hwaccel import encoder_args

    hw_global, enc_args, enc_name = encoder_args(crf, preset)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        *hw_global,
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", f"[{final}]",
        "-an",
        *enc_args,
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        str(out_path),
    ]
    return ComposePlan(
        cmd=cmd, width=width, height=height, duration=duration,
        cameras=cams, dropped_segments=dropped, segments=segs,
        encoder=enc_name,
    )
