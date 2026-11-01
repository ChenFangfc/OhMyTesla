"""事件缩略图：优先车机自带的 thumb.png，没有就从片段里抽一帧并缓存。

Tesla 只给 SentryClips / SavedClips 附 thumb.png，RecentClips 从来没有，
老固件和半截同步过来的事件目录也可能没有。/api/dashcam/thumb 和新录像推送
（watcher）共用这一份逻辑。
"""

import asyncio
import contextlib
import hashlib
import secrets
from pathlib import Path

from ..config import settings
from . import ff

_MIN_BYTES = 512


def _first_readable(base: Path, ev) -> Path | None:
    """挑一个用来抽帧的片段：优先前摄，其次任意机位。"""
    for seg in ev.segments:
        for cam in ("front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar"):
            rel = seg.cameras.get(cam)
            if rel:
                f = (base / rel).resolve()
                if f.is_file() and f.stat().st_size > 10_000:
                    return f
    return None


def vehicle_thumb(base: Path, ev) -> Path | None:
    """车机自带的 thumb.png（校验过路径不越界）。"""
    if not ev.thumb:
        return None
    f = (base / ev.thumb).resolve()
    if f.is_file() and f.is_relative_to(base.resolve()) and f.stat().st_size > 0:
        return f
    return None


async def _grab(src: Path, tmp: Path, seek: str | None) -> None:
    # -update 1 必须有：没有它 ffmpeg 会把输出当成图片序列，
    # 因为文件名里没有 %03d 这类模式而报错退出
    args = [ff.ffmpeg(), "-v", "error", "-nostdin", "-y"]
    if seek:
        args += ["-ss", seek]
    args += ["-i", str(src), "-frames:v", "1", "-update", "1", "-vf", "scale=480:-2",
             "-q:v", "5", "-f", "image2", str(tmp)]
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=25)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise


async def event_thumbnail(base: Path, ev) -> tuple[Path, str] | None:
    """(图片路径, MIME)。抽不出来返回 None；超时抛 asyncio.TimeoutError。"""
    own = vehicle_thumb(base, ev)
    if own is not None:
        return own, "image/png"

    cache = Path(settings.preview_dir) / "thumbs"
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / (hashlib.sha256(ev.id.encode()).hexdigest()[:20] + ".jpg")
    if dest.is_file() and dest.stat().st_size > _MIN_BYTES:
        return dest, "image/jpeg"

    src = _first_readable(base, ev)
    if src is None:
        return None
    # 临时名要带随机后缀。固定用 .part 的话，两个浏览器标签同时请求同一个
    # 事件的缩略图，两个 ffmpeg 会往同一个文件里写
    tmp = dest.with_suffix(f".{secrets.token_hex(4)}.part")
    try:
        await _grab(src, tmp, "2")
        if not tmp.is_file() or tmp.stat().st_size < _MIN_BYTES:
            # 片段不足 2 秒时 -ss 2 会落在片尾之后，退回第一帧再试
            tmp.unlink(missing_ok=True)
            await _grab(src, tmp, None)
        if not tmp.is_file() or tmp.stat().st_size < _MIN_BYTES:
            return None
        tmp.replace(dest)                             # 原子替换，避免读到写一半的文件
        return dest, "image/jpeg"
    finally:
        tmp.unlink(missing_ok=True)
