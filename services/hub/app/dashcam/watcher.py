"""录像目录监视器。

配合 teslausb（树莓派 Wi-Fi 自动同步）或手动拷贝：当 SentryClips /
SavedClips 里出现新的事件目录时，推送告警（附缩略图 + 站内链接）。

设计要点:
- 基线持久化到 SQLite（watcher_known），hub 重启期间同步进来的事件也能告警；
- 挂载点瞬时消失（teslausb 归档周期 / CIFS 抖动）导致的空扫描不动基线，
  恢复后不会把历史事件当新事件轰炸；
- 同一轮发现多个新事件合并成一条告警（避开冷却机制的丢弃）。
"""

import asyncio
import contextlib
import logging

from ..alerts.engine import engine
from ..alerts.store import store
from ..livestate import live
from .scanner import scanner

log = logging.getLogger(__name__)

SCAN_INTERVAL_S = 60


class ClipWatcher:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._known: set[str] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="clip-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _default_car(self) -> str:
        # 只认 TeslaMate 的数字 car_id，跳过 fleet 的 "vin:..." 键
        return next((k for k in live.cars if k.isdigit()), "0")

    async def _run(self) -> None:
        saved = await store.get_setting("watcher_known")
        if saved is not None:
            self._known = set(saved)
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                log.exception("clip watcher tick failed")
            await asyncio.sleep(SCAN_INTERVAL_S)

    async def _tick(self) -> None:
        if scanner.base_path() is None:
            return  # 挂载点不在（U 盘拔了 / 同步中），不动基线
        events = await asyncio.to_thread(scanner.scan, True)
        ids = {e.id for e in events if e.type in ("sentry", "saved")}
        if not ids and self._known:
            return  # 瞬时空扫描（目录还在但内容读不到），同样不动基线
        if self._known is None:
            self._known = ids  # 首次部署：已有历史事件不告警
            await store.set_setting("watcher_known", sorted(ids))
            return
        new_ids = ids - self._known
        self._known = ids
        await store.set_setting("watcher_known", sorted(ids))
        if not new_ids:
            return

        base = scanner.base_path()
        new_events = [e for eid in sorted(new_ids) if (e := scanner.get(eid))]
        from ..config import settings as _settings
        from ..i18n.prefs import get as _get_prefs
        from ..i18n.router import t as _t

        loc = (await _get_prefs())["locale"]
        # 推送是在手机上看的，链接必须是能点开的绝对地址。没配 PUBLIC_URL
        # 就退回相对路径，并在末尾说明一次为什么点不动。
        root = (_settings.public_url or "").rstrip("/")

        lines = []
        attach = None
        for ev in new_events[:8]:
            kind = _t("dashcam.sentry" if ev.type == "sentry" else "dashcam.saved", loc)
            desc = f"[{kind}] {ev.timestamp}"
            if ev.city:
                desc += f" · {ev.city}"
            lines.append(desc)
            lines.append(f"  {root}/dashcam/play?id={ev.id}")
            if attach is None and base and ev.thumb:
                attach = str(base / ev.thumb)
        if len(new_events) > 8:
            lines.append(_t("watcher.more_events", loc, n=len(new_events)))
        if not root:
            lines.append(_t("watcher.set_public_url", loc))

        await engine._fire(
            "new_sentry_clip", self._default_car(),
            extra="\n".join(lines), attach=attach,
        )


watcher = ClipWatcher()
