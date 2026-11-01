"""车机本地时间 ↔ UTC。

TeslaCam 文件名（`2026-08-05_10-30-01-front.mp4`）是车机本地时间，不带时区；
TeslaMate 的时间列是 UTC。两者之间的换算必须**按每个时刻各自的规则**做：
1 月 15 日 08:00 在芝加哥是 UTC-6，7 月 15 日 08:00 是 UTC-5。

以前没配 TESLACAM_TZ 时用的是 `datetime.now().astimezone().tzinfo` ——
那是一个**定死的、今天的**偏移量。夏天跑的进程会把冬天的录像整体算错一小时，
叠加层、HUD、CSV、取证包全部错位；每次夏令时切换还会让 SEI 索引的
seg_start 整体漂移一小时。这里改成解析出一个真正的时区（ZoneInfo），
解析不出名字时退回 `_SystemLocal` —— 它对每个时刻单独问操作系统的
localtime 规则，同样跟着夏令时走。

解析顺序：TESLACAM_TZ → TZ（hub 统计用的时区设置）→ 环境变量 TZ
→ /etc/localtime 的符号链接目标 → /etc/timezone → 系统 localtime 规则。
"""

import logging
import os
import time as _time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import settings

log = logging.getLogger(__name__)


class _SystemLocal(tzinfo):
    """跟随操作系统 localtime 规则的时区（Python 文档里的 LocalTimezone 配方）。

    和 `datetime.now().astimezone().tzinfo` 的区别：那个是"现在"的固定偏移，
    这个对每个时刻分别计算，所以冬天的时刻拿到冬令时偏移。
    """

    def _offset(self, dt: datetime) -> timedelta:
        if dt.tzinfo is self:
            dt = dt.replace(tzinfo=None)
        stamp = _time.mktime((dt.year, dt.month, dt.day, dt.hour, dt.minute,
                              dt.second, dt.weekday(), 0, -1))
        return timedelta(seconds=_time.localtime(stamp).tm_gmtoff)

    def utcoffset(self, dt):
        if dt is None:
            return timedelta(seconds=_time.localtime().tm_gmtoff)
        return self._offset(dt)

    def dst(self, dt):
        if dt is None:
            return timedelta(0)
        dt = dt.replace(tzinfo=None)
        stamp = _time.mktime((dt.year, dt.month, dt.day, dt.hour, dt.minute,
                              dt.second, dt.weekday(), 0, -1))
        return timedelta(hours=1) if _time.localtime(stamp).tm_isdst > 0 else timedelta(0)

    def tzname(self, dt):
        if dt is None:
            return _time.tzname[0]
        dt = dt.replace(tzinfo=None)
        stamp = _time.mktime((dt.year, dt.month, dt.day, dt.hour, dt.minute,
                              dt.second, dt.weekday(), 0, -1))
        return _time.localtime(stamp).tm_zone

    def fromutc(self, dt):
        # 由 UTC 换到本地：直接问 localtime，避免 tzinfo 默认算法在切换点上的歧义
        stamp = dt.replace(tzinfo=timezone.utc).timestamp()
        lt = _time.localtime(stamp)
        return (dt.replace(tzinfo=None) + timedelta(seconds=lt.tm_gmtoff)).replace(tzinfo=self)

    def __repr__(self) -> str:
        return "SystemLocal()"

    def __str__(self) -> str:
        return "system local time"


_SYSTEM_LOCAL = _SystemLocal()
# (配置快照, 时区, 来源)。配置可能在运行时被改（测试、设置页），所以按快照缓存
_resolved: tuple[tuple, tzinfo, str] | None = None


def _zone(name: str | None) -> ZoneInfo | None:
    name = (name or "").strip()
    if not name:
        return None
    # TZ 环境变量允许 POSIX 规则串（"CST6CDT"）和前导冒号（":America/Chicago"）
    name = name.removeprefix(":")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _zone_from_system() -> tuple[ZoneInfo, str] | None:
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        target = ""
    if "zoneinfo/" in target:
        z = _zone(target.split("zoneinfo/", 1)[1])
        if z:
            return z, "/etc/localtime"
    try:
        z = _zone(Path("/etc/timezone").read_text().strip())
    except OSError:
        z = None
    return (z, "/etc/timezone") if z else None


def resolve() -> tuple[tzinfo, str]:
    """(时区, 来源说明)。来源会在 /api/dashcam/status 里报出来，方便排查"差一小时"。"""
    global _resolved
    snap = (settings.teslacam_tz, getattr(settings, "tz", ""), os.environ.get("TZ", ""))
    if _resolved and _resolved[0] == snap:
        return _resolved[1], _resolved[2]

    tz: tzinfo | None = None
    source = ""
    if settings.teslacam_tz:
        tz = _zone(settings.teslacam_tz)
        if tz:
            source = "TESLACAM_TZ"
        else:
            log.warning("invalid TESLACAM_TZ %r, falling back", settings.teslacam_tz)
    if tz is None and getattr(settings, "tz", ""):
        tz = _zone(settings.tz)
        source = "TZ setting" if tz else ""
    if tz is None and os.environ.get("TZ"):
        tz = _zone(os.environ["TZ"])
        source = "TZ environment" if tz else ""
    if tz is None:
        found = _zone_from_system()
        if found:
            tz, source = found
    if tz is None:
        tz, source = _SYSTEM_LOCAL, "system localtime rules"
    _resolved = (snap, tz, source)
    log.info("TeslaCam local time zone: %s (from %s)", tz, source)
    return tz, source


def local_tz() -> tzinfo:
    """车机本地时区。永远是按时刻计算偏移的时区，不会是定死的偏移量。"""
    return resolve()[0]


def describe() -> dict:
    tz, source = resolve()
    now = datetime.now(timezone.utc).astimezone(tz)
    return {"name": str(getattr(tz, "key", None) or tz), "source": source,
            "utc_offset_now": now.strftime("%z")}


def local_naive_to_utc(dt: datetime) -> datetime:
    """车机本地 naive 时刻 → aware UTC。已经带时区的原样换算。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=local_tz()).astimezone(timezone.utc)


def utc_to_local_naive(value: float | datetime) -> datetime:
    """epoch 秒或 aware datetime → 车机本地 naive 时刻。"""
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        aware = datetime.fromtimestamp(float(value), timezone.utc)
    return aware.astimezone(local_tz()).replace(tzinfo=None)


def to_local_naive(dt: datetime) -> datetime:
    """event.json 里的时间戳可能带偏移（"…Z"、"…+02:00"），统一成车机本地 naive。

    混着 aware 和 naive 的 datetime 一排序就 TypeError，/events 整页 500。
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(local_tz()).replace(tzinfo=None)
