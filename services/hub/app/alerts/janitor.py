"""定期清理：数据库旧记录 + 磁盘上的派生文件。

这个模块存在的理由很简单：在它之前，本项目**没有任何东西会删除任何东西**。

- `alerts` / `sentry_sessions` / `compose_jobs` 三张表只增不删（只有一个
  「清空全部告警」的按钮），而它们和电价方案、token 覆盖共用同一个 SQLite
  文件，写入又是串行的 —— 表越大，全应用的每一次写就越慢。
- `exports/`（合成成品）、`preview/`（HEVC 转码）、缩略图缓存全部按内容命名、
  永不回收。一个 6 机位事件合出来几百 MB，NAS 或树莓派上很快就满。
- 失败的合成任务还会在 `exports/` 里留下 `.ass` 字幕文件（只有成功路径删），
  而那个目录是公开挂载的。

派生文件都能重新生成，所以按修改时间过期删除是安全的：录像原件在
TeslaCam 目录里，不归这里管，一个字节都不会碰。
"""

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from ..config import settings
from .store import store

log = logging.getLogger(__name__)

# 一天扫一次就够——这些东西的增长以天计，不以分钟计
INTERVAL_S = 6 * 60 * 60
# 启动后先等一会儿再干活，别和首屏抢 IO
STARTUP_DELAY_S = 300


def _sweep_dir(path: Path, keep_days: int, suffixes: tuple[str, ...] | None = None) -> tuple[int, int]:
    """删掉目录里过期的文件。返回 (删除个数, 释放字节数)。"""
    if keep_days <= 0 or not path.is_dir():
        return 0, 0
    cutoff = time.time() - keep_days * 86400
    n = freed = 0
    for f in path.iterdir():
        if not f.is_file():
            continue
        if suffixes and f.suffix.lower() not in suffixes:
            continue
        try:
            st = f.stat()
            if st.st_mtime >= cutoff:
                continue
            size = st.st_size
            f.unlink()
        except OSError:  # noqa: S112 - 文件正被写入时跳过是正常路径，上面已有 debug 日志
            continue        # 正在被读/已经没了，下一轮再说
        n += 1
        freed += size
    return n, freed


def _sweep_orphan_subtitles(exports: Path) -> int:
    """删掉没有对应成品的 .ass 字幕。

    合成失败或被取消时不会走到删字幕那一步，于是它们会永远留在
    公开挂载的 exports 目录里。成品还在的就别动（重新合成时会复用）。
    """
    if not exports.is_dir():
        return 0
    n = 0
    for f in exports.glob("*.ass"):
        try:
            if f.with_suffix(".mp4").exists():
                continue
            # 刚生成的可能正在被 ffmpeg 读，留一小时安全期
            if time.time() - f.stat().st_mtime < 3600:
                continue
            f.unlink()
            n += 1
        except OSError:
            log.debug("suppressed", exc_info=True)
            continue
    return n


class Janitor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._last: dict = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="janitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def state(self) -> dict:
        return dict(self._last)

    async def _run(self) -> None:
        await asyncio.sleep(STARTUP_DELAY_S)
        while True:
            try:
                self._last = await self.sweep()
            except Exception:
                log.exception("janitor sweep failed")
            await asyncio.sleep(INTERVAL_S)

    async def sweep(self) -> dict:
        result: dict = {"at": time.time()}

        rows = await store.prune(settings.history_retention_days)
        if rows:
            result["rows"] = rows

        keep = settings.media_retention_days
        files = freed = 0
        for d, sfx in (
            (settings.exports_dir, (".mp4", ".ass")),
            (settings.preview_dir, (".mp4",)),
            (Path(settings.preview_dir) / "thumbs", None),
        ):
            n, b = _sweep_dir(Path(d), keep, sfx)
            files += n
            freed += b

        # SEI 索引跟着素材走，不按年龄清。扫不到素材目录时一行都不动 ——
        # U 盘没插的时候把索引清空，等于每次插拔都要全库重解。
        try:
            from ..dashcam.scanner import _teslacam_base, scanner

            base = _teslacam_base()
            if base is not None:
                # A fresh scan, off the event loop: pruning against a stale
                # cached list would drop the index of clips that just arrived.
                alive = {
                    rel
                    for ev in await scanner.ascan(force=True)
                    for seg in ev.segments
                    for rel in seg.cameras.values()
                }
                # A folder that exists but holds no clips at all is far more
                # likely an unmounted share or a drive mid-swap than "every clip
                # deleted" — pruning then wiped the whole index (audit F14).
                if not alive:
                    log.info("TeslaCam folder has no clips; leaving the SEI index alone")
                else:
                    sei_rows = await store.prune_sei_orphans(alive)
                    if sei_rows:
                        result.setdefault("rows", {}).update(sei_rows)
        except Exception:
            log.exception("SEI index prune failed")

        orphans = _sweep_orphan_subtitles(Path(settings.exports_dir))
        if files or orphans:
            result["files"] = files + orphans
            result["freed_mb"] = round(freed / 1_048_576, 1)
            log.info("janitor removed %d file(s), freed %.1f MB",
                     files + orphans, freed / 1_048_576)
        return result


janitor = Janitor()
