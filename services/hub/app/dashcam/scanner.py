"""扫描 TeslaCam 目录，解析事件与分段视频。

支持三种来源，结构略有不同：

  车机 U 盘原样拷贝（TeslaCam/ 一层可有可无）:
    TeslaCam/
      RecentClips/                     # 散装 1 分钟片段
        2026-08-05_10-30-01-front.mp4
      SavedClips/                      # 手动保存的事件
        2026-08-05_10-35-22/
          event.json                   # {"timestamp": "...", "city": "...", "reason": "...", ...}
          thumb.png
          2026-08-05_10-25-01-front.mp4
          ...
      SentryClips/                     # 哨兵事件（结构同 SavedClips）

  teslausb 归档（CIFS/rsync 落到服务器上，没有 TeslaCam/ 这一层）:
      SentryClips/<事件>/…  SavedClips/<事件>/…
      RecentClips/<YYYY-MM-DD>/*.mp4   # teslausb 自己按天分的目录
      ARCHIVE_UPLOADED                 # 触发文件，不是事件
      .teslausbtmp/                    # rsync 的临时目录

  浏览器导入（/api/dashcam/import）：写进当前生效的那个根。

teslausb 的按天目录里还混着 Saved/Sentry 片段的副本（为了让"最近"时间线连续）。
同一个文件名（= 同一分钟同一机位）在事件目录里已经有了的，RecentClips 里的那份
丢掉 —— 和车机自己的行为一致（保存时车是把片段**移走**的），也避免 SEI 统计
把同一分钟算两遍。

摄像头后缀: front / back / left_repeater / right_repeater
           / left_pillar / right_pillar (HW4)
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)

CAMERAS = ["front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar"]

_FILE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(" + "|".join(CAMERAS) + r")\.mp4$"
)
_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TYPE_DIRS = {"SentryClips": "sentry", "SavedClips": "saved", "RecentClips": "recent"}

# 名义分段时长。只在**还没探测到真实时长**时兜底用（列表页的预估时长）。
NOMINAL_SEGMENT_S = 60.0
# RecentClips 相邻两段的起点间隔超过这么多秒就切成两个"伪事件"
RECENT_GAP_S = 90

_SKIP_LOG_MAX = 50


def parse_ts(name: str) -> datetime:
    """'2026-08-05_10-30-01' -> naive datetime（车机本地时间）。名字合法但日期不存在时抛 ValueError。"""
    return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S")  # noqa: DTZ007 - 刻意解析无时区的本地时间（车机文件名 / HH:MM 时段），加 %z 反而是错的


@dataclass
class Segment:
    start: datetime  # naive local
    cameras: dict[str, str] = field(default_factory=dict)  # camera -> relpath
    # 真实时长（秒），由 probe 填。None = 还没探测过
    duration: float | None = None

    def to_dict(self) -> dict:
        d = {"start": self.start.isoformat(), "cameras": self.cameras}
        if self.duration is not None:
            d["duration"] = self.duration
        return d


@dataclass
class Event:
    id: str          # 相对路径形式的唯一 ID，如 "SentryClips/2026-08-05_11-00-00"
    type: str        # sentry / saved / recent
    timestamp: datetime  # naive local（事件触发时刻或首段开始时刻）
    segments: list[Segment] = field(default_factory=list)
    city: str = ""
    reason: str = ""
    est_lat: float | None = None
    est_lon: float | None = None
    thumb: str | None = None  # relpath
    folder: str | None = None  # 事件目录 relpath（RecentClips 伪事件为 None）

    @property
    def start(self) -> datetime:
        return self.segments[0].start if self.segments else self.timestamp

    @property
    def end(self) -> datetime:
        if not self.segments:
            return self.timestamp
        last = self.segments[-1]
        return last.start + timedelta(seconds=last.duration or NOMINAL_SEGMENT_S)

    def to_dict(self, with_segments: bool = False) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp.isoformat(),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "city": self.city,
            "reason": self.reason,
            "est_lat": self.est_lat,
            "est_lon": self.est_lon,
            "thumb": self.thumb,
            "segment_count": len(self.segments),
            "cameras": sorted({c for s in self.segments for c in s.cameras}),
        }
        if with_segments:
            d["segments"] = [s.to_dict() for s in self.segments]
        return d


def _scandir(d: Path) -> list[os.DirEntry]:
    with os.scandir(d) as it:
        return sorted(it, key=lambda e: e.name)


def _has_entries(d: Path) -> bool:
    try:
        with os.scandir(d) as it:
            return any(not e.name.startswith(".") for e in it)
    except OSError:
        return False


def _has_clips(d: Path) -> bool:
    return any((d / name).is_dir() and _has_entries(d / name) for name in TYPE_DIRS)


def _teslacam_base() -> Path | None:
    """当前生效的素材根。

    `TeslaCam/` 和根目录都可能装着素材（U 盘原样拷贝 vs teslausb 归档）。
    以前只要 `TeslaCam/` 存在就选它 —— 而 /import 在空目录上恰好会建一个空的
    `TeslaCam/`，之后 teslausb 落在根目录的素材就整个看不见了。现在先看谁
    真的有东西，都空的时候才按目录在不在决定。
    """
    root = Path(settings.teslacam_root)
    if not root.is_dir():
        return None
    nested = root / "TeslaCam"
    nested_ok = nested.is_dir()
    if nested_ok and _has_clips(nested):
        return nested
    if _has_clips(root):
        return root
    if nested_ok:
        return nested
    if any((root / d).is_dir() for d in TYPE_DIRS):
        return root
    return None


class _Skips:
    """扫描时跳过的条目（坏文件名、读不了的目录）。只留前若干条给排障用。"""

    def __init__(self) -> None:
        self.count = 0
        self.items: list[dict] = []

    def add(self, path: str, reason: str) -> None:
        self.count += 1
        if len(self.items) < _SKIP_LOG_MAX:
            self.items.append({"path": path, "reason": reason})


def _add_file(by_start: dict[str, Segment], entry: os.DirEntry, base: Path,
              skips: _Skips, taken: set[str] | None = None) -> str | None:
    m = _FILE_RE.match(entry.name)
    if not m:
        return None
    try:
        if not entry.is_file() or entry.stat().st_size == 0:
            return None
    except OSError as e:
        skips.add(entry.path, f"stat failed: {e}")
        return None
    start_str, camera = m.group(1), m.group(2)
    if taken is not None and entry.name in taken:
        return None  # 事件目录里已经有这一分钟这一路了
    seg = by_start.get(start_str)
    if seg is None:
        try:
            seg = Segment(start=parse_ts(start_str))
        except ValueError:
            # 名字长得对、日期却不存在（2026-02-30、24-00-00）。以前一个这样的
            # 文件就让整次扫描抛 ValueError，所有素材接口 500、监视器每轮都失败。
            skips.add(entry.path, "invalid timestamp in file name")
            return None
        by_start[start_str] = seg
    # 先到先得：按天目录和平铺目录里有同名副本时，结果和遍历顺序无关
    seg.cameras.setdefault(camera, str(Path(entry.path).relative_to(base)))
    return entry.name


def _collect_segments(folder: Path, base: Path, skips: _Skips | None = None) -> list[Segment]:
    skips = skips or _Skips()
    by_start: dict[str, Segment] = {}
    for e in _scandir(folder):
        _add_file(by_start, e, base, skips)
    return sorted(by_start.values(), key=lambda s: s.start)


def _load_event_folder(folder: Path, base: Path, ev_type: str,
                       skips: _Skips | None = None) -> Event | None:
    from .localtime import to_local_naive

    skips = skips or _Skips()
    segments = _collect_segments(folder, base, skips)
    if not segments:
        return None
    try:
        ts = parse_ts(folder.name) if _FOLDER_RE.match(folder.name) else segments[0].start
    except ValueError:
        skips.add(str(folder), "invalid timestamp in folder name; using first clip")
        ts = segments[0].start
    ev = Event(
        id=str(folder.relative_to(base)),
        type=ev_type,
        timestamp=ts,
        segments=segments,
        folder=str(folder.relative_to(base)),
    )
    meta = folder / "event.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text())
            if not isinstance(data, dict):
                raise ValueError("event.json is not an object")  # noqa: TRY004,TRY301 - 统一抛 ValueError：路由层用 except ValueError 转 400，换成 TypeError 就成 500
            if ts := data.get("timestamp"):
                # 带 Z / 偏移的按它说的换算成车机本地时间；不带的就是本地时间
                raw = str(ts).strip()
                if raw.endswith("Z"):
                    raw = raw[:-1] + "+00:00"
                ev.timestamp = to_local_naive(datetime.fromisoformat(raw))
            ev.city = str(data.get("city") or "")
            ev.reason = str(data.get("reason") or "")
            for src, attr in (("est_lat", "est_lat"), ("est_lon", "est_lon")):
                try:
                    setattr(ev, attr, float(data[src]))
                except (KeyError, TypeError, ValueError):
                    log.debug("suppressed", exc_info=True)
        except Exception as e:  # noqa: BLE001 - 坏 event.json 不应拖垮整个扫描
            log.warning("bad event.json in %s: %s", folder, e)
    thumb = folder / "thumb.png"
    if thumb.exists():
        ev.thumb = str(thumb.relative_to(base))
    return ev


def _collect_recent(recent_dir: Path, base: Path, skips: _Skips, taken: set[str]) -> list[Segment]:
    """RecentClips：车机是平铺的，teslausb 是 RecentClips/<YYYY-MM-DD>/。两种都收。"""
    by_start: dict[str, Segment] = {}
    for e in _scandir(recent_dir):
        if e.name.startswith("."):
            continue
        try:
            is_dir = e.is_dir()
        except OSError as err:
            skips.add(e.path, f"stat failed: {err}")
            continue
        if is_dir:
            if not _DAY_RE.match(e.name):
                continue
            try:
                entries = _scandir(Path(e.path))
            except OSError as err:
                skips.add(e.path, f"unreadable folder: {err}")
                continue
            for f in entries:
                _add_file(by_start, f, base, skips, taken)
        else:
            _add_file(by_start, e, base, skips, taken)
    return sorted(by_start.values(), key=lambda s: s.start)


def _group_recent(segments: list[Segment]) -> list["Event"]:
    """RecentClips 是散装片段，按时间连续性（间隔 ≤ 90s）分组成伪事件。"""
    events: list[Event] = []
    current: list[Segment] = []
    for seg in segments:
        if current and (seg.start - current[-1].start).total_seconds() > RECENT_GAP_S:
            events.append(_recent_event(current))
            current = []
        current.append(seg)
    if current:
        events.append(_recent_event(current))
    return events


def _recent_event(segments: list[Segment]) -> Event:
    return Event(
        id=f"RecentClips/{segments[0].start.strftime('%Y-%m-%d_%H-%M-%S')}",
        type="recent",
        timestamp=segments[0].start,
        segments=segments,
    )


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class Scanner:
    CACHE_S = 30

    def __init__(self) -> None:
        self._events: dict[str, Event] = {}
        self._scanned_at: float = 0
        self._lock = threading.Lock()
        self._inflight: asyncio.Future | None = None
        self._inflight_started: float = 0
        self._refresh_task: asyncio.Task | None = None
        self.stats: dict = {}

    # ---------------- 同步扫描（会阻塞，在线程里调用） ----------------

    def _scan_blocking(self) -> list[Event]:
        with self._lock:
            started = time.time()
            base = _teslacam_base()
            events: dict[str, Event] = {}
            skips = _Skips()
            if base is None:
                log.info("TeslaCam root %s not found or empty", settings.teslacam_root)
                self._events = {}
                self._scanned_at = time.time()
                self.stats = {"base": None, "events": 0, "skipped": 0, "skipped_items": []}
                return []

            taken: set[str] = set()   # 事件目录里出现过的片段文件名
            for dir_name, ev_type in TYPE_DIRS.items():
                if ev_type == "recent":
                    continue
                d = base / dir_name
                if not d.is_dir():
                    continue
                try:
                    entries = _scandir(d)
                except OSError as e:
                    skips.add(str(d), f"unreadable folder: {e}")
                    continue
                for e in entries:
                    # 只认特斯拉命名规范的事件目录，杂项（.teslausbtmp、ARCHIVE_UPLOADED、
                    # 恶意命名）一律跳过
                    if not _FOLDER_RE.match(e.name):
                        continue
                    folder = Path(e.path)
                    try:
                        if not e.is_dir():
                            continue
                        ev = _load_event_folder(folder, base, ev_type, skips)
                    except OSError as err:
                        skips.add(str(folder), f"unreadable event folder: {err}")
                        continue
                    except Exception as err:  # 一个坏目录不能拖垮整个素材库
                        log.warning("skip event folder %s", folder, exc_info=True)
                        skips.add(str(folder), f"unexpected error: {err}")
                        continue
                    if ev:
                        events[ev.id] = ev
                        for seg in ev.segments:
                            for rel in seg.cameras.values():
                                taken.add(Path(rel).name)

            recent = base / "RecentClips"
            if recent.is_dir():
                try:
                    for ev in _group_recent(_collect_recent(recent, base, skips, taken)):
                        events[ev.id] = ev
                except OSError as e:
                    skips.add(str(recent), f"unreadable folder: {e}")
                except Exception as e:
                    log.warning("RecentClips scan failed", exc_info=True)
                    skips.add(str(recent), f"unexpected error: {e}")

            # 两种布局同时有素材时只能用一个（事件 ID 是相对生效根的路径）；
            # 把被挡住的那个报出来，别让它悄悄消失
            root = Path(settings.teslacam_root)
            other = root if base != root else root / "TeslaCam"
            shadowed = str(other) if other.is_dir() and _has_clips(other) else None

            # 保留已经探测到的时长（同一个文件对象被换掉时不用重探）
            for ev in events.values():
                old = self._events.get(ev.id)
                if not old:
                    continue
                old_durs = {s.start: (s.duration, s.cameras) for s in old.segments}
                for seg in ev.segments:
                    prev = old_durs.get(seg.start)
                    if prev and prev[1] == seg.cameras:
                        seg.duration = prev[0]

            self._events = events
            self._scanned_at = time.time()
            self.stats = {
                "base": str(base),
                "events": len(events),
                "skipped": skips.count,
                "skipped_items": skips.items,
                "shadowed_root": shadowed,
                "scan_seconds": round(time.time() - started, 3),
                "scanned_at": self._scanned_at,
            }
            if skips.count:
                log.info("scanned TeslaCam: %d events, %d entries skipped", len(events), skips.count)
            else:
                log.info("scanned TeslaCam: %d events", len(events))
            return list(events.values())

    def _fresh(self) -> bool:
        return bool(self._events) and time.time() - self._scanned_at < self.CACHE_S

    def scan(self, force: bool = False) -> list[Event]:
        """同步接口（老调用方用）。

        在事件循环里被调用、而缓存只是过期了的时候，先把旧结果给出去，再在
        后台线程里刷新 —— 整树扫描在 SMB 上要好几秒，不能卡住单进程的 uvicorn。
        新代码请用 `await scanner.ascan()`。
        """
        if not force and self._fresh():
            return list(self._events.values())
        if not force and self._events and _in_event_loop():
            self._schedule_refresh()
            return list(self._events.values())
        return self._scan_blocking()

    def _schedule_refresh(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            return
        if self._inflight is not None and not self._inflight.done():
            return
        self._refresh_task = asyncio.get_running_loop().create_task(self.ascan(force=True))

    async def ascan(self, force: bool = False) -> list[Event]:
        """异步扫描：在线程里跑，并发的调用合并成一次。

        force=True 保证拿到的是**这次调用之后才开始**的扫描结果
        （teslausb 通知"归档完成"之后要看见刚落盘的文件）。
        """
        requested = time.time()
        while True:
            if not force and self._fresh():
                return list(self._events.values())
            fut = self._inflight
            if fut is not None and not fut.done():
                await asyncio.shield(fut)
                if not force or self._inflight_started >= requested:
                    return list(self._events.values())
                continue
            self._inflight_started = time.time()
            self._inflight = asyncio.ensure_future(asyncio.to_thread(self._scan_blocking))
            return list(await asyncio.shield(self._inflight))

    def get(self, event_id: str) -> Event | None:
        self.scan()
        return self._events.get(event_id)

    async def aget(self, event_id: str) -> Event | None:
        await self.ascan()
        ev = self._events.get(event_id)
        if ev is None and time.time() - self._scanned_at > 2:
            # 可能是刚落盘的新事件（推送里的链接比下一轮扫描先到）
            await self.ascan(force=True)
            ev = self._events.get(event_id)
        return ev

    def base_path(self) -> Path | None:
        return _teslacam_base()


scanner = Scanner()


async def fill_durations(event: Event, base: Path, all_cameras: bool = False) -> None:
    """给事件的每一段填上真实时长（ffprobe，带缓存）。

    默认每段只探一路（前摄优先）：同一分钟各机位的长度几乎一样，
    探全部机位只在合成时才需要（那里要逐个确认文件读得出来）。
    """
    from . import probe

    picks: dict[int, list[Path]] = {}
    for i, seg in enumerate(event.segments):
        cams = list(seg.cameras) if all_cameras else (
            ["front"] if "front" in seg.cameras else list(seg.cameras)[:1])
        picks[i] = [base / seg.cameras[c] for c in cams]
    results = await probe.probe_many([p for ps in picks.values() for p in ps], concurrency=6)
    for i, seg in enumerate(event.segments):
        durs = [results[p]["duration"] for p in picks[i] if results[p].get("duration")]
        seg.duration = max(durs) if durs else None
