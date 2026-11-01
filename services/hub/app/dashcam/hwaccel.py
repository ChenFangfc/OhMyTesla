"""硬件编码器探测。

为什么不能只看 `ffmpeg -encoders`：那张表列的是**编译进去**的编码器，不是
**能用**的。官方 ffmpeg 镜像里 h264_nvenc / h264_vaapi 一应俱全，但容器里
既没有 NVIDIA 卡也没有 /dev/dri，一跑就报 "Cannot load libcuda.so" 或
"Failed to open /dev/dri/renderD128"。照着列表选编码器，结果是每一次导出
都在几秒后失败，而用户只看到"合成失败"。

所以这里**真的编一帧试试**，而且试的是**和合成时一模一样的参数**：同样的
质量旋钮、同样的滤镜尾巴（VAAPI 的 hwupload）。以前探测不带质量参数，于是
Intel Mac 上 VideoToolbox 探测通过、真正导出时却因为不支持 -q:v 全部失败；
VAAPI 探测里有 hwupload、合成命令里没有，探测通过等于每次导出必失败。

各家的质量旋钮完全不通用：libx264 用 -crf，nvenc 用 -cq，vaapi 用 -qp，
qsv 用 -global_quality，VideoToolbox 在 Apple Silicon 上认 -q:v（**0-100**，
不是 0-1 —— 以前给的 0.5 会被取整成 0，也就是最低画质），在 Intel Mac 上
只认码率。照抄 -crf 给别家会被忽略，导出出来是默认码率的糊图。
"""

import logging
import subprocess
from dataclasses import dataclass, field

from . import ff

log = logging.getLogger(__name__)

# QUALITY 里的 x264 preset → 各家 preset
_NVENC_PRESET = {"medium": "p5", "veryfast": "p3", "ultrafast": "p1"}
# QSV 没有 ultrafast（传进去直接报错）；它的档位是 veryfast…veryslow
_QSV_PRESET = {"medium": "medium", "veryfast": "veryfast", "ultrafast": "veryfast"}
# VideoToolbox 码率模式（Intel Mac）下每像素每帧的比特数
_VT_BPP = {"18": 0.15, "23": 0.10, "27": 0.06}


def _vt_q(crf: str) -> str:
    """crf → VideoToolbox -q:v（1-100）。

    实测（M 系列，同一段素材）：crf 18 ≈ q 64，crf 23 ≈ q 53，crf 27 ≈ q 44
    时 PSNR 与 libx264 veryfast 相当。
    """
    return str(max(20, min(90, round(105 - 2.25 * int(crf)))))


def _vt_bitrate(crf: str, width: int, height: int) -> str:
    bpp = _VT_BPP.get(crf, 0.10)
    return f"{max(1, round(bpp * width * height * 30 / 1000))}k"


@dataclass
class Encoder:
    name: str
    global_args: list[str] = field(default_factory=list)   # 放在 -i 之前
    filter_tail: str = ""                                  # 接在 filtergraph 最后
    mode: str = ""                                         # 同一编码器的不同质量模式

    def quality_args(self, crf: str, preset: str, width: int = 1920, height: int = 1080) -> list[str]:
        if self.name == "libx264":
            return ["-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]
        if self.name == "h264_nvenc":
            return ["-preset", _NVENC_PRESET.get(preset, "p4"),
                    "-rc", "vbr", "-cq", crf, "-b:v", "0", "-pix_fmt", "yuv420p"]
        if self.name == "h264_vaapi":
            return ["-qp", crf]
        if self.name == "h264_qsv":
            return ["-global_quality", crf, "-preset", _QSV_PRESET.get(preset, "medium")]
        if self.name == "h264_videotoolbox":
            if self.mode == "bitrate":
                return ["-b:v", _vt_bitrate(crf, width, height), "-pix_fmt", "yuv420p"]
            return ["-q:v", _vt_q(crf), "-pix_fmt", "yuv420p"]
        return []

    @property
    def hardware(self) -> bool:
        return self.name != "libx264"


# 候选按"画质/兼容性优先"排序
_CANDIDATES = [
    Encoder("h264_nvenc"),
    Encoder("h264_vaapi", ["-vaapi_device", "/dev/dri/renderD128"], "format=nv12,hwupload"),
    Encoder("h264_qsv", [], "format=nv12"),
    Encoder("h264_videotoolbox", mode="quality"),
    Encoder("h264_videotoolbox", mode="bitrate"),   # Intel Mac：qscale not available
]
_SOFTWARE = Encoder("libx264")

_probed: Encoder | None = None
_probe_done = False
_tried: list[dict] = []


def _works(enc: Encoder) -> bool:
    """真编几帧，参数和合成时一致。只有退出码为 0 才算可用。"""
    if not ff.available():
        return False
    vf = "format=yuv420p" + ("," + enc.filter_tail if enc.filter_tail else "")
    cmd = [
        ff.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *enc.global_args,
        "-f", "lavfi", "-i", "testsrc=s=320x240:r=30:d=0.2",
        "-vf", vf,
        "-c:v", enc.name, *enc.quality_args("23", "veryfast", 320, 240),
        "-frames:v", "3", "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20, check=False,  # noqa: S603 - 参数是列表形式、不过 shell
                           stdin=subprocess.DEVNULL)
        err = (r.stderr or b"").decode(errors="replace").strip()[:200]
        _tried.append({"encoder": enc.name, "mode": enc.mode, "ok": r.returncode == 0,
                       "error": "" if r.returncode == 0 else err})
        if r.returncode == 0:
            return True
        log.debug("hwaccel %s/%s unavailable: %s", enc.name, enc.mode, err)
        return False
    except Exception:
        log.debug("hwaccel %s probe failed", enc.name, exc_info=True)
        _tried.append({"encoder": enc.name, "mode": enc.mode, "ok": False, "error": "probe failed"})
        return False


def detect() -> Encoder:
    """进程内只探测一次。会起子进程，异步代码里请用 asyncio.to_thread 调。"""
    global _probed, _probe_done
    if _probe_done and _probed is not None:
        return _probed
    _tried.clear()
    for enc in _CANDIDATES:
        if _works(enc):
            log.info("hardware encoder available: %s %s", enc.name, enc.mode)
            _probed, _probe_done = enc, True
            return enc
    log.info("no usable hardware encoder, falling back to libx264")
    _probed, _probe_done = _SOFTWARE, True
    return _SOFTWARE


def force(enc: Encoder | None) -> None:
    """测试用：指定编码器（None = 重新探测）。"""
    global _probed, _probe_done
    _probed, _probe_done = enc, enc is not None


def encoder_args(crf: str, preset: str, width: int = 1920, height: int = 1080
                 ) -> tuple[list[str], list[str], str]:
    """给 composer 用：(全局前置参数, 编码参数, 实际用的编码器名)。"""
    enc = detect()
    return enc.global_args, ["-c:v", enc.name, *enc.quality_args(crf, preset, width, height)], enc.name


def status() -> dict:
    """给界面/排障用。没探测过就先探测。"""
    enc = detect()
    return {
        "encoder": enc.name,
        "mode": enc.mode or None,
        "hardware": enc.hardware,
        "candidates": list(dict.fromkeys(c.name for c in _CANDIDATES)),
        "tried": list(_tried),
        # 叠加层要 libass；没有它导出照样出，只是不带遥测叠加
        "overlay_supported": ff.has_filter("ass"),
    }
