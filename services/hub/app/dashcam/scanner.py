"""扫描 TeslaCam 目录，解析事件与分段视频。

U 盘目录结构:
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

摄像头后缀: front / back / left_repeater / right_repeater
           / left_pillar / right_pillar (HW4)
"""

import json
import logging
import re
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

TYPE_DIRS = {"SentryClips": "sentry", "SavedClips": "saved", "RecentClips": "recent"}


def parse_ts(name: str) -> datetime:
    """'2026-08-05_10-30-01' -> naive datetime（车机本地时间）"""
    return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S")  # noqa: DTZ007 - 刻意解析无时区的本地时间（车机文件名 / HH:MM 时段），加 %z 反而是错的


@dataclass
class Segment:
    start: datetime  # naive local
    cameras: dict[str, str] = field(default_factory=dict)  # camera -> relpath

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), "cameras": self.cameras}


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

    @property
    def start(self) -> datetime:
        return self.segments[0].start if self.segments else self.timestamp

    @property
    def end(self) -> datetime:
        if not self.segments:
            return self.timestamp
        return self.segments[-1].start + timedelta(seconds=60)

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


def _teslacam_base() -> Path | None:
    root = Path(settings.teslacam_root)
    if not root.exists():
        return None
    if (root / "TeslaCam").is_dir():
        return root / "TeslaCam"
    if any((root / d).is_dir() for d in TYPE_DIRS):
        return root
    return None


def _collect_segments(folder: Path, base: Path) -> list[Segment]:
    by_start: dict[str, Segment] = {}
    for f in folder.iterdir():
        m = _FILE_RE.match(f.name)
        if not m or f.stat().st_size == 0:
            continue
        start_str, camera = m.group(1), m.group(2)
        seg = by_start.setdefault(start_str, Segment(start=parse_ts(start_str)))
        seg.cameras[camera] = str(f.relative_to(base))
    return sorted(by_start.values(), key=lambda s: s.start)


def _load_event_folder(folder: Path, base: Path, ev_type: str) -> Event | None:
    segments = _collect_segments(folder, base)
    if not segments:
        return None
    ev = Event(
        id=str(folder.relative_to(base)),
        type=ev_type,
        timestamp=parse_ts(folder.name) if _FOLDER_RE.match(folder.name) else segments[0].start,
        segments=segments,
    )
    meta = folder / "event.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text())
            if not isinstance(data, dict):
                raise ValueError("event.json is not an object")  # noqa: TRY004,TRY301 - 统一抛 ValueError：路由层用 except ValueError 转 400，换成 TypeError 就成 500
            if ts := data.get("timestamp"):
                ev.timestamp = datetime.fromisoformat(str(ts).replace("Z", ""))
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


def _group_recent(recent_dir: Path, base: Path) -> list[Event]:
    """RecentClips 是散装片段，按时间连续性（间隔 ≤ 90s）分组成伪事件。"""
    segments = _collect_segments(recent_dir, base)
    events: list[Event] = []
    current: list[Segment] = []
    for seg in segments:
        if current and (seg.start - current[-1].start).total_seconds() > 90:
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


class Scanner:
    def __init__(self) -> None:
        self._events: dict[str, Event] = {}
        self._scanned_at: float = 0

    def scan(self, force: bool = False) -> list[Event]:
        if not force and time.time() - self._scanned_at < 30 and self._events:
            return list(self._events.values())

        base = _teslacam_base()
        events: dict[str, Event] = {}
        if base is None:
            log.info("TeslaCam root %s not found or empty", settings.teslacam_root)
            self._events = {}
            self._scanned_at = time.time()
            return []

        for dir_name, ev_type in TYPE_DIRS.items():
            d = base / dir_name
            if not d.is_dir():
                continue
            if ev_type == "recent":
                for ev in _group_recent(d, base):
                    events[ev.id] = ev
                continue
            for folder in d.iterdir():
                # 只认特斯拉命名规范的事件目录，杂项目录（也包括恶意命名）一律跳过
                if not folder.is_dir() or not _FOLDER_RE.match(folder.name):
                    continue
                try:
                    ev = _load_event_folder(folder, base, ev_type)
                except OSError as e:
                    log.warning("skip unreadable event folder %s: %s", folder, e)
                    continue
                if ev:
                    events[ev.id] = ev

        self._events = events
        self._scanned_at = time.time()
        log.info("scanned TeslaCam: %d events", len(events))
        return list(events.values())

    def get(self, event_id: str) -> Event | None:
        self.scan()
        return self._events.get(event_id)

    def base_path(self) -> Path | None:
        return _teslacam_base()


scanner = Scanner()
