"""判断一个片段能不能被解码、能不能直接在浏览器里播，以及它**实际**有多长。

为什么需要：新固件的行车记录仪片段可能是**加密**的（车机 安全 设置里可关）。
加密片段的文件名、大小、目录结构跟正常片段一模一样 —— 扫描器照样能数出
"3 个事件、每个 2 分钟"，但里面一帧也解不出来。用户看到的是"有条目、没画面"，
除非我们明说，否则他会以为是这个软件坏了。

同样要区分开的还有 HEVC：那是能解码的，只是 Safari 以外的浏览器放不了，
应用有转码预览兜底。两者的处置完全不同，所以必须分开报。

时长也从这里来。以前所有地方都假设一段 = 60 秒；事件的最后一段、teslausb
快照时被截断的片段都更短，于是第一段短片之后叠加层、HUD、SEI 全部错位。
ffprobe 一次几十毫秒，结果按 (路径, 大小, mtime) 缓存进 data 目录，
重启之后也不用重探。
"""

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from ..config import settings
from . import ff

log = logging.getLogger(__name__)

PLAYABLE_IN_BROWSER = {"h264", "vp8", "vp9", "av1"}

PROBE_TIMEOUT_S = 12
# 超时的结果只记一小会儿：卡住多半是 CIFS 抖动，过一会儿可能就好了；
# 但不能不记，否则每次 /status 都会再起一批注定超时的 ffprobe。
_TIMEOUT_TTL_S = 300
_CACHE_MAX = 100_000

_lock = threading.Lock()
# 路径 -> [size, mtime, result, 写入时刻]
_cache: dict[str, list] = {}
_loaded = False
_dirty = 0
_save_handle: asyncio.TimerHandle | None = None


def _cache_file() -> Path:
    return Path(settings.data_dir) / "dashcam" / "probe-cache.json"


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    f = _cache_file()
    if not f.exists():
        return
    try:
        data = json.loads(f.read_text())
        if isinstance(data, dict):
            _cache.update({k: v for k, v in data.items() if isinstance(v, list) and len(v) == 4})
    except (OSError, ValueError):
        log.warning("probe cache unreadable, starting empty", exc_info=True)


def save() -> None:
    """把缓存落盘（原子替换）。同步，调用方自己决定放不放线程。"""
    global _dirty
    with _lock:
        if not _dirty:
            return
        # 超时类的临时结果不落盘
        snapshot = {k: v for k, v in _cache.items() if v[2].get("reason") != "timeout"}
        _dirty = 0
    f = _cache_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(snapshot, separators=(",", ":")))
        tmp.replace(f)
    except OSError:
        log.warning("could not write probe cache", exc_info=True)


def _schedule_save() -> None:
    """有新结果时 10 秒后合并写一次盘。不在事件循环里就当场写。"""
    global _save_handle
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 线程里：由 probe_many / 下一次异步调用负责落盘
    if _save_handle is None or _save_handle.cancelled():
        def fire() -> None:
            global _save_handle
            _save_handle = None
            loop.run_in_executor(None, save)
        _save_handle = loop.call_later(10, fire)


def _lookup(key: str, st: os.stat_result) -> dict | None:
    with _lock:
        _load()
        hit = _cache.get(key)
    if not hit or hit[0] != st.st_size or hit[1] != st.st_mtime:
        return None
    result = hit[2]
    if result.get("reason") == "timeout" and time.time() - hit[3] > _TIMEOUT_TTL_S:
        return None
    return result


def _store(key: str, st: os.stat_result, result: dict) -> None:
    global _dirty
    with _lock:
        if len(_cache) >= _CACHE_MAX:
            for k in list(_cache)[: _CACHE_MAX // 5]:   # 丢掉最早写入的 20%
                _cache.pop(k, None)
        _cache[key] = [st.st_size, st.st_mtime, result, time.time()]
        _dirty += 1


def _cmd(path: Path) -> list[str]:
    return [ff.ffprobe(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,avg_frame_rate,duration"
            ":format=duration",
            "-of", "json", str(path)]


def _interpret(out: bytes | None) -> dict:
    codec = width = height = None
    duration = fps = None
    try:
        data = json.loads(out or b"{}")
        streams = data.get("streams") or []
        if streams:
            s = streams[0]
            codec = s.get("codec_name")
            width, height = s.get("width"), s.get("height")
            num, _, den = str(s.get("avg_frame_rate") or "").partition("/")
            with contextlib.suppress(ValueError, ZeroDivisionError):
                f = float(num) / float(den or 1)
                fps = f if 1 < f < 120 else None
            with contextlib.suppress(TypeError, ValueError):
                duration = float(s.get("duration"))
        if duration is None:
            with contextlib.suppress(TypeError, ValueError):
                duration = float((data.get("format") or {}).get("duration"))
    except ValueError:
        log.debug("suppressed", exc_info=True)

    if duration is not None and duration <= 0:
        duration = None
    if not codec:
        # 文件在、有大小，但连视频流都识别不出来 —— 加密片段和截断（没有
        # moov）的片段都是这个表现，具体是哪种由 decrypt.is_encrypted 再分
        return {"ok": False, "codec": None, "reason": "unreadable", "duration": None,
                "fps": None, "width": None, "height": None}
    return {
        "ok": True, "codec": codec,
        "reason": "" if codec in PLAYABLE_IN_BROWSER else "needs_transcode",
        "duration": round(duration, 3) if duration else None,
        "fps": fps, "width": width, "height": height,
    }


def probe_sync(path: Path) -> dict:
    """同步版本，给已经在线程里的代码用（SEI 解析、取证打包）。"""
    try:
        st = path.stat()
    except OSError:
        return {"ok": False, "codec": None, "reason": "missing", "duration": None, "fps": None}
    key = str(path)
    hit = _lookup(key, st)
    if hit is not None:
        return hit
    try:
        r = subprocess.run(_cmd(path), capture_output=True,  # noqa: S603 - 参数列表、不过 shell
                           timeout=PROBE_TIMEOUT_S, check=False)
        result = _interpret(r.stdout)
    except subprocess.TimeoutExpired:   # subprocess.run 超时会自己杀掉子进程
        result = {"ok": False, "codec": None, "reason": "timeout", "duration": None, "fps": None}
    except OSError as e:
        log.debug("ffprobe failed on %s: %s", path.name, e)
        return {"ok": False, "codec": None, "reason": "unreadable", "duration": None, "fps": None}
    _store(key, st, result)
    return result


async def probe(path: Path) -> dict:
    """返回 {ok, codec, reason, duration, fps, width, height}。

    reason: "" | "unreadable"（解不开：加密或截断）| "needs_transcode"（HEVC）
            | "missing" | "timeout"
    """
    try:
        st = path.stat()  # noqa: ASYNC240 - 本地 stat，微秒级
    except OSError:
        return {"ok": False, "codec": None, "reason": "missing", "duration": None, "fps": None}

    key = str(path)
    hit = _lookup(key, st)
    if hit is not None:
        return hit

    try:
        proc = await asyncio.create_subprocess_exec(
            *_cmd(path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        log.debug("ffprobe failed to start on %s: %s", path.name, e)
        return {"ok": False, "codec": None, "reason": "unreadable", "duration": None, "fps": None}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT_S)
        result = _interpret(out)
    except asyncio.TimeoutError:
        # 以前超时就直接 return，ffprobe 进程留在后台继续跑；每次 /status
        # 还会再起一批。这里杀掉、收尸，并把"超时"短暂记进缓存。
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        log.debug("ffprobe timed out on %s", path.name)
        result = {"ok": False, "codec": None, "reason": "timeout", "duration": None, "fps": None}
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    _store(key, st, result)
    _schedule_save()
    return result


async def probe_many(paths: list[Path], concurrency: int = 4) -> dict[Path, dict]:
    """并发探测一批文件（有上限，别把小主机的 CPU 打满），探完落一次盘。"""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(p: Path) -> tuple[Path, dict]:
        async with sem:
            return p, await probe(p)

    results = dict(await asyncio.gather(*(one(p) for p in dict.fromkeys(paths))))
    if _dirty:
        await asyncio.to_thread(save)
    return results


async def probe_event(base: Path, segments: list) -> dict:
    """抽查一个事件里的片段，给出整体可读性。

    只抽查首段和末段：一个事件里的片段来自同一次录制，加密与否是一致的，
    逐个探测几十个文件只是在浪费时间。
    """
    picks: list[Path] = []
    for seg in (segments[:1] + segments[-1:]) if segments else []:
        for rel in list(seg.cameras.values())[:1]:
            picks.append(base / rel)
    if not picks:
        return {"readable": True, "reason": "", "codec": None}

    results = await asyncio.gather(*(probe(p) for p in picks))
    good = [r for r in results if r["ok"]]
    if not good:
        reason = "timeout" if all(r["reason"] == "timeout" for r in results) else "unreadable"
        return {"readable": False, "reason": reason, "codec": None}
    needs = [r for r in good if r["reason"] == "needs_transcode"]
    return {
        "readable": True,
        "reason": "needs_transcode" if needs else "",
        "codec": good[0]["codec"],
    }
