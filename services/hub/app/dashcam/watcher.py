"""录像目录监视器。

配合 teslausb（树莓派 Wi-Fi 自动同步）或手动拷贝：当 SentryClips 里出现新的
事件目录时，以 `new_sentry_clip` 规则交给告警引擎（附缩略图 + 站内链接）——
推不推到手机由引擎按规则决定（默认只进站内记录）。

产品规则：手机推送必须短，只给哨兵触发和重大事件。所以标题是一行
（"哨兵录像已保存 · 城市"），正文也只有一行（触发原因 · 时刻），链接走 url
参数、缩略图走 image 参数，不往正文里塞多行文字。SavedClips 是车主自己存的，
不发告警。

设计要点:
- 基线持久化到 SQLite（watcher_known），hub 重启期间同步进来的事件也能告警；
- 挂载点瞬时消失（teslausb 归档周期 / CIFS 抖动）导致的空扫描不动基线，
  恢复后不会把历史事件当新事件轰炸；
- **送达之后才算"已知"**。以前新事件先进基线再发送，而发送可能因为冷却、
  静默时段、暂停告警或通道故障被丢掉 —— 那个事件就再也不会被提起。
  现在 fire_custom 返回 False 就留在待发列表里，按退避重试；
- **等目录落定再发**。teslausb 按文件名排序传，mp4 在前、event.json /
  thumb.png 在最后，一个 2 GB 的事件要传好几分钟。目录里有了 thumb.png 和
  event.json，或者 15 秒内没有任何变化（且 rsync 临时目录里没有正在传的文件），
  才发 —— 推送里才能带上缩略图、触发原因和城市；
- 每个事件只发一次。同一轮落定的不超过 3 个就逐条发（各带自己的缩略图），
  更多就合并成一条（缩略图和链接用最新的那个）。
"""

import asyncio
import contextlib
import inspect
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from ..alerts.engine import engine
from ..alerts.store import store
from ..livestate import live
from .scanner import Event, parse_ts, scanner

log = logging.getLogger(__name__)

SCAN_INTERVAL_S = 60
STABLE_S = 15                    # 目录内容这么久没变就算传完了
RETRY_BASE_S = 60
RETRY_MAX_S = 1800
PENDING_MAX_AGE_S = 24 * 3600    # 一直送不出去（规则关着、长期暂停）就放弃，免得哪天一次补发几十条
MAX_EVENT_AGE = timedelta(days=7)  # 比这更老的事件出现时（导入旧素材）不推送，只记进基线
MAX_SEPARATE = 3                 # 一轮里不超过这么多个就逐条推送
TEMP_FRESH_S = 600               # .teslausbtmp 里 10 分钟内动过的文件 = 正在传
RULE = "new_sentry_clip"
ALERT_TYPES = ("sentry",)        # SavedClips 是车主手动存的，不提醒


@dataclass
class _Pending:
    first_seen: float
    sig: tuple | None = None
    sig_since: float = 0.0
    attempts: int = 0
    next_try: float = 0.0


def _folder_signature(folder: Path) -> tuple[tuple, bool]:
    """(目录内容快照, 是否有正在写的临时文件)。快照 = 每个条目的 (名字, 大小)。"""
    entries = []
    temp = False
    with os.scandir(folder) as it:
        for e in it:
            try:
                size = e.stat().st_size
            except OSError:
                size = -1
            entries.append((e.name, size))
            if e.name.startswith("."):
                temp = True        # rsync 默认的 .name.XXXXXX 临时文件
    return tuple(sorted(entries)), temp


def _transfer_in_progress(base: Path) -> bool:
    """teslausb 的 rsync 把正在传的文件放在共享根目录的 .teslausbtmp/ 里。"""
    from ..config import settings

    now = time.time()
    for d in {base / ".teslausbtmp", Path(settings.teslacam_root) / ".teslausbtmp"}:
        if not d.is_dir():
            continue
        with contextlib.suppress(OSError), os.scandir(d) as it:
            for e in it:
                with contextlib.suppress(OSError):
                    if now - e.stat().st_mtime < TEMP_FRESH_S:
                        return True
    return False


def _event_age(ev: Event) -> timedelta:
    try:
        ts = parse_ts(Path(ev.id).name)
    except ValueError:
        ts = ev.timestamp
    return datetime.now() - ts  # noqa: DTZ005 - 事件时间是车机本地 naive 时间，这里只做粗略的天数比较


class ClipWatcher:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._known: set[str] | None = None
        self._pending: dict[str, _Pending] = {}
        self._lock: asyncio.Lock | None = None
        self._wake: asyncio.Event | None = None
        self._fire_kwargs: set[str] | None = None
        self.last_tick: dict = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="clip-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _default_car(self) -> str:
        # The car that most recently sent live data (the first key of live.cars
        # can be a car that left the account); TeslaCam doesn't say which car.
        # 只认 TeslaMate 的数字 car_id，跳过 fleet 的 "vin:..." 键
        live_ids = [k for k in live.cars if k.isdigit()]
        fresh = [k for k in live_ids if live.live_seen.get(k)]
        return max(fresh, key=lambda k: live.live_seen.get(k, 0)) if fresh else next(iter(live_ids), "0")

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _load_known(self) -> None:
        if self._known is None:
            saved = await store.get_setting("watcher_known")
            if saved is not None:
                self._known = set(saved)

    async def _run(self) -> None:
        await self._load_known()
        self._wake = asyncio.Event()
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("clip watcher tick failed")
            # 有正在落定的事件时 16 秒后再看一眼，不用等满一分钟
            delay = min(SCAN_INTERVAL_S, STABLE_S + 1) if self._pending else SCAN_INTERVAL_S
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            self._wake.clear()

    async def poke(self, settled: bool = False) -> dict:
        """立刻跑一轮（teslausb 通知"归档完成"时调用）。

        settled=True 表示归档进程说它这一轮已经传完：不必再等 15 秒确认目录没变。
        """
        await self._load_known()
        result = await self._tick(settled=settled)
        if self._wake is not None and self._pending:
            self._wake.set()       # 还有没落定的，让常驻循环尽快接着看
        return result

    async def _persist(self) -> None:
        await store.set_setting("watcher_known", sorted(self._known or ()))

    async def _tick(self, settled: bool = False) -> dict:
        async with self._get_lock():
            base = scanner.base_path()
            if base is None:
                return {"skipped": "teslacam root not found"}  # 挂载点不在，不动基线
            events = await scanner.ascan(force=True)
            current = {e.id: e for e in events if e.type in ALERT_TYPES}
            if not current and self._known:
                return {"skipped": "empty scan"}  # 瞬时空扫描（目录还在但内容读不到）
            if self._known is None:
                self._known = set(current)      # 首次部署：已有历史事件不告警
                await self._persist()
                return {"baseline": len(current)}

            now = time.time()
            changed = False
            for eid in list(self._pending):
                if eid not in current:
                    self._pending.pop(eid, None)   # 目录没了（被删、被移走）
            # 基线里早就不存在、而且已经很老的条目可以忘掉：就算重新出现，
            # 也会因为太老而只进基线不推送
            stale = {eid for eid in self._known - set(current)
                     if eid.split("/")[-1] < (datetime.now() - MAX_EVENT_AGE).strftime("%Y-%m-%d")}  # noqa: DTZ005 - 与车机本地时间比较
            if stale:
                self._known -= stale
                changed = True

            in_transfer = None
            ready: list[Event] = []
            for eid in sorted(set(current) - self._known):
                ev = current[eid]
                if _event_age(ev) > MAX_EVENT_AGE:
                    self._known.add(eid)          # 导入的旧素材：不算"新录像"
                    changed = True
                    continue
                p = self._pending.setdefault(eid, _Pending(first_seen=now))
                if now - p.first_seen > PENDING_MAX_AGE_S:
                    log.warning("clip watcher: giving up on alerting %s after %d attempt(s)",
                                eid, p.attempts)
                    self._known.add(eid)
                    self._pending.pop(eid, None)
                    changed = True
                    continue
                if p.next_try > now:
                    continue
                try:
                    sig, has_temp = await asyncio.to_thread(_folder_signature, base / (ev.folder or eid))
                except OSError as e:
                    log.debug("clip watcher: cannot list %s: %s", eid, e)
                    continue
                if sig != p.sig:
                    p.sig, p.sig_since = sig, now
                names = {n for n, _ in sig}
                complete = "thumb.png" in names and "event.json" in names
                if not complete:
                    if settled:
                        ok = not has_temp
                    else:
                        if in_transfer is None:
                            in_transfer = await asyncio.to_thread(_transfer_in_progress, base)
                        ok = (now - p.sig_since >= STABLE_S) and not has_temp and not in_transfer
                    if not ok:
                        continue
                ready.append(ev)

            sent = 0
            if ready:
                delivered = await self._deliver(ready, base)
                for ev in ready:
                    if ev.id in delivered:
                        self._known.add(ev.id)
                        self._pending.pop(ev.id, None)
                        changed = True
                        sent += 1
                    else:
                        p = self._pending[ev.id]
                        p.attempts += 1
                        p.next_try = now + min(RETRY_BASE_S * 2 ** (p.attempts - 1), RETRY_MAX_S)
            if changed:
                await self._persist()          # 只在基线真的变了时写库
            self.last_tick = {"at": now, "events": len(current), "pending": len(self._pending),
                              "ready": len(ready), "sent": sent}
            return dict(self.last_tick)

    # ---------------- 发送 ----------------

    def _supported_kwargs(self) -> set[str]:
        """fire_custom 接受哪些可选参数。告警引擎在重构，image/url 是新加的，
        老签名没有它们 —— 两种都要能跑。"""
        if self._fire_kwargs is None:
            try:
                params = inspect.signature(engine.fire_custom).parameters
                if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                    self._fire_kwargs = {"image", "url"}
                else:
                    self._fire_kwargs = {k for k in ("image", "url") if k in params}
            except (TypeError, ValueError):
                self._fire_kwargs = set()
        return self._fire_kwargs

    async def _fire(self, title: str, body: str, image: str | None, url: str | None) -> bool:
        # Switched off by the owner: handled. fire_custom answers False for an
        # Off rule, which would otherwise be retried for a day.
        with contextlib.suppress(Exception):
            if (await engine.rule_modes()).get(RULE) == "off":
                return True
        kwargs = {}
        ok_kw = self._supported_kwargs()
        if "image" in ok_kw:
            kwargs["image"] = image
        if "url" in ok_kw:
            kwargs["url"] = url
        try:
            return bool(await engine.fire_custom(
                self._default_car(), _rule_level(), title, body, rule=RULE, gate=RULE, **kwargs))
        except Exception:
            log.warning("clip watcher: alert delivery raised", exc_info=True)
            return False

    async def _image_for(self, base: Path, ev: Event) -> str | None:
        from . import thumbs

        try:
            hit = await thumbs.event_thumbnail(base, ev)
        except Exception:  # 抽不出缩略图也要照样发
            log.debug("thumbnail for %s failed", ev.id, exc_info=True)
            return None
        return str(hit[0]) if hit else None

    async def _deliver(self, ready: list[Event], base: Path) -> set[str]:
        """返回送达了的事件 ID。标题一行、正文一行，链接和缩略图走参数。"""
        from ..i18n.prefs import get as _get_prefs
        from ..i18n.router import t

        loc = (await _get_prefs())["locale"]
        ready = sorted(ready, key=lambda e: e.timestamp, reverse=True)   # 最新的在前
        delivered: set[str] = set()
        # Same shape as every push: "🎬 {car} — {event}" / "{place} · {time}".
        from ..alerts.sentry import car_label

        car = await car_label(self._default_car())

        def when(ev: Event) -> str:
            return ev.timestamp.strftime("%H:%M" if loc == "zh" else "%-I:%M %p")

        if len(ready) <= MAX_SEPARATE:
            for ev in ready:
                title = f"🎬 {car} — {t('watcher.sentry_clip_noplace', loc)}"
                body = " · ".join(x for x in (_reason_text(ev.reason, loc, t), ev.city, when(ev)) if x)
                image = await self._image_for(base, ev)
                if await self._fire(title, body, image, play_link(ev.id)):
                    delivered.add(ev.id)
            return delivered

        newest = ready[0]
        title = f"🎬 {car} — {t('watcher.sentry_clips', loc, n=len(ready))}"
        body = " · ".join(x for x in (newest.city, when(newest)) if x)
        image = await self._image_for(base, newest)
        if await self._fire(title, body, image, "/dashcam"):
            delivered = {ev.id for ev in ready}
        return delivered

    def status(self) -> dict:
        return {
            "known": len(self._known or ()),
            "pending": [
                {"event_id": eid, "since": p.first_seen, "attempts": p.attempts,
                 "next_try": p.next_try or None}
                for eid, p in sorted(self._pending.items())
            ],
            "last_tick": self.last_tick,
        }


_REASONS = {
    "sentry_aware_object_detection": "dashcam.reason.object_detected",
    "sentry_panic_alarm": "dashcam.reason.panic_alarm",
    "user_interaction_dashcam_icon_tapped": "dashcam.reason.saved_icon",
    "user_interaction_dashcam_panel_save": "dashcam.reason.saved_panel",
    "user_interaction_honk": "dashcam.reason.honk",
}


def _reason_text(reason: str, loc: str, t) -> str:
    if not reason:
        return ""
    key = _REASONS.get(reason)
    return t(key, loc) if key else reason


def play_link(event_id: str) -> str:
    """站内播放页的相对链接。告警引擎会按 PUBLIC_URL 补成绝对地址（没配就只给 Web 推送用）。"""
    return f"/dashcam/play?id={quote(event_id, safe='/')}"


def _rule_level() -> str:
    """跟随引擎规则表里 new_sentry_clip 的级别（引擎在重构，别在这里写死）。"""
    try:
        from ..alerts.engine import RULES

        return RULES[RULE].level
    except (ImportError, KeyError, AttributeError):
        return "info"


watcher = ClipWatcher()
