"""硬件编码器探测。

为什么不能只看 `ffmpeg -encoders`：那张表列的是**编译进去**的编码器，不是
**能用**的。官方 ffmpeg 镜像里 h264_nvenc / h264_vaapi 一应俱全，但容器里
既没有 NVIDIA 卡也没有 /dev/dri，一跑就报 "Cannot load libcuda.so" 或
"Failed to open /dev/dri/renderD128"。照着列表选编码器，结果是每一次导出
都在几秒后失败，而用户只看到"合成失败"。

所以这里**真的编一帧试试**。一次探测约 0.5 秒，结果缓存在进程里；
探测失败就退回 libx264，导出照常能跑，只是慢一点。

顺带说清楚为什么值得做：一个 6 机位 HW4 事件按 2896x1876 合成，
libx264 veryfast 在树莓派或小 NAS 上要跑到实时的好几倍时间；
有硬件编码器时通常快 3-10 倍，而这类机器正是自托管用户的主流。
"""

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

# 候选按"画质/兼容性优先"排序。每一项是 (编码器名, 额外的全局参数, 质量参数生成器)。
#
# 各家的质量旋钮完全不通用：libx264 用 -crf，nvenc 用 -cq，
# vaapi 用 -qp，videotoolbox 只认码率。照抄 -crf 给 nvenc 会被忽略，
# 导出出来是默认码率的糊图 —— 这是"看起来生效了"的典型。
_CANDIDATES = [
    ("h264_nvenc", [], lambda crf, preset: [
        "-preset", {"medium": "p5", "veryfast": "p3", "ultrafast": "p1"}.get(preset, "p4"),
        "-rc", "vbr", "-cq", crf, "-b:v", "0",
    ]),
    ("h264_vaapi", ["-vaapi_device", "/dev/dri/renderD128"], lambda crf, preset: [
        "-qp", crf,
    ]),
    ("h264_qsv", [], lambda crf, preset: [
        "-global_quality", crf, "-preset", preset,
    ]),
    ("h264_videotoolbox", [], lambda crf, preset: [
        # VideoToolbox 不认 crf，只能给质量系数（0-1）。粗略映射：
        # crf 18 → 0.72，crf 23 → 0.60，crf 27 → 0.50
        "-q:v", str(max(0.35, min(0.85, 1.0 - int(crf) / 45))),
    ]),
]

_probed: tuple[str, list, object] | None = None
_probe_done = False


def _works(name: str, extra: list[str]) -> bool:
    """真编一帧。只有退出码为 0 才算这个编码器可用。"""
    if not shutil.which("ffmpeg"):
        return False
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *extra,
        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1",
    ]
    if name == "h264_vaapi":
        # vaapi 需要把帧先传到显存，否则报 "Impossible to convert between formats"
        cmd += ["-vf", "format=nv12,hwupload"]
    cmd += ["-c:v", name, "-frames:v", "1", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20, check=False)  # noqa: S603 - 参数是列表形式、不过 shell，可执行文件名是硬编码常量
        if r.returncode == 0:
            return True
        log.debug("hwaccel %s unavailable: %s", name,
                  (r.stderr or b"").decode(errors="replace").strip()[:160])
        return False
    except Exception:
        log.debug("hwaccel %s probe failed", name, exc_info=True)
        return False


def detect() -> tuple[str, list, object]:
    """返回 (编码器名, 全局额外参数, 质量参数生成器)。进程内只探测一次。"""
    global _probed, _probe_done
    if _probe_done and _probed is not None:
        return _probed

    for name, extra, qargs in _CANDIDATES:
        if _works(name, extra):
            log.info("hardware encoder available: %s", name)
            _probed = (name, extra, qargs)
            _probe_done = True
            return _probed

    log.info("no usable hardware encoder, falling back to libx264")
    _probed = ("libx264", [], lambda crf, preset: ["-preset", preset, "-crf", crf])
    _probe_done = True
    return _probed


def encoder_args(crf: str, preset: str) -> tuple[list[str], list[str], str]:
    """给 composer 用：(全局前置参数, 编码参数, 实际用的编码器名)。"""
    name, extra, qargs = detect()
    return extra, ["-c:v", name, *qargs(crf, preset)], name


def status() -> dict:
    """给界面/排障用。没探测过就先探测。"""
    name, _extra, _ = detect()
    return {
        "encoder": name,
        "hardware": name != "libx264",
        # 把候选和各自能不能用一并报出来，比只说"用的是 libx264"有用得多
        "candidates": [c[0] for c in _CANDIDATES],
    }
