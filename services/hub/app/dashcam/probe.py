"""判断一个片段能不能被解码，以及能不能直接在浏览器里播。

为什么需要：新固件的行车记录仪片段可能是**加密**的（车机 安全 设置里可关）。
加密片段的文件名、大小、目录结构跟正常片段一模一样 —— 扫描器照样能数出
"3 个事件、每个 2 分钟"，但里面一帧也解不出来。用户看到的是"有条目、没画面"，
除非我们明说，否则他会以为是这个软件坏了。

同样要区分开的还有 HEVC：那是能解码的，只是 Safari 以外的浏览器放不了，
应用有转码预览兜底。两者的处置完全不同，所以必须分开报。
"""

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# 探测结果缓存：路径 -> (mtime, 结果)。ffprobe 一次几十毫秒，
# 但列表页一屏就有几十个片段，不缓存会把扫描拖慢一个数量级。
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_MAX = 4000

PLAYABLE_IN_BROWSER = {"h264", "vp8", "vp9", "av1"}


async def probe(path: Path) -> dict:
    """返回 {ok, codec, reason}。

    reason: "" | "unreadable"（解不开，多半是加密）| "needs_transcode"（HEVC）
    """
    try:
        st = path.stat()
    except OSError:
        return {"ok": False, "codec": None, "reason": "missing"}

    key = str(path)
    hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime:
        return hit[1]

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
    except (OSError, asyncio.TimeoutError) as e:
        log.debug("ffprobe failed on %s: %s", path.name, e)
        return {"ok": False, "codec": None, "reason": "unreadable"}

    codec = None
    try:
        streams = json.loads(out or b"{}").get("streams") or []
        if streams:
            codec = streams[0].get("codec_name")
    except ValueError:
        pass

    if not codec:
        # 文件在、有大小，但连视频流都识别不出来 —— 加密片段就是这个表现
        result = {"ok": False, "codec": None, "reason": "unreadable"}
    elif codec in PLAYABLE_IN_BROWSER:
        result = {"ok": True, "codec": codec, "reason": ""}
    else:
        result = {"ok": True, "codec": codec, "reason": "needs_transcode"}

    if len(_cache) > _CACHE_MAX:
        _cache.clear()
    _cache[key] = (st.st_mtime, result)
    return result


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
        return {"readable": False, "reason": "unreadable", "codec": None}
    needs = [r for r in good if r["reason"] == "needs_transcode"]
    return {
        "readable": True,
        "reason": "needs_transcode" if needs else "",
        "codec": good[0]["codec"],
    }
