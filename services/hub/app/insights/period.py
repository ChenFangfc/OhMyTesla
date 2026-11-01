"""时间段参数与本地时区 —— 所有分析接口共用的一套约定。

TeslaMate 的时间列全是 naive UTC。凡是"按天/周/月"的东西都必须先换到
**用户所在时区**再切：芝加哥晚上 10 点的行程按 UTC 算是第二天，报销表、
日历、月度统计就会整体错一天（审计 F12/F17 实测 29 个事件落错了日子）。

接口约定（API.md 里有完整说明）：
  ?days=N               最近 N 天；days=0 表示全部
  ?from=2026-08-01      本地日期（含当天 00:00）
  ?to=2026-08-31        本地日期（含整天，即到 09-01 00:00 为止）
  ?from=2026-08-01T06:00:00-05:00   也接受带偏移的 ISO 时间、或毫秒时间戳
from/to 优先于 days。返回给前端的时间一律是带 +00:00 的 ISO 字符串。
"""

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

log = logging.getLogger(__name__)

_warned_tz: set[str] = set()


def local_tz_name() -> str:
    """给 Postgres 和 Python 共用的 IANA 时区名。

    优先级：TZ 设置项（config.tz）> 容器 TZ 环境变量 > TESLACAM_TZ > UTC。
    名字不合法就退回 UTC —— 宁可全站统一偏移，也不要让 SQL 抛 invalid time zone。
    """
    from ..config import settings

    for raw in (getattr(settings, "tz", ""), os.environ.get("TZ", ""),
                getattr(settings, "teslacam_tz", "")):
        name = (raw or "").strip()
        if not name:
            continue
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            if name not in _warned_tz:
                _warned_tz.add(name)
                log.warning("unknown timezone %r, ignoring", name)
    return "UTC"


def local_tz() -> ZoneInfo:
    return ZoneInfo(local_tz_name())


def iso(dt) -> str | None:
    """naive UTC → 带 +00:00 的 ISO 字符串（前端 new Date() 才不会当本地时间解析）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def to_local(dt) -> datetime | None:
    """naive UTC → 带时区的本地时间。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz())


# "全部历史"时 SQL 的下界（naive UTC，比任何 TeslaMate 数据都早）
EARLIEST = datetime(2000, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)


def utcnow() -> datetime:
    """naive UTC 的"现在"，直接和 TeslaMate 的列比较。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Period:
    start: datetime | None   # naive UTC，含
    end: datetime | None     # naive UTC，不含；None = 现在
    tz: str

    def bounds(self) -> tuple[datetime, datetime]:
        """SQL 用的 [start, end)；全部历史时 start 取一个足够早的时刻。"""
        return (self.start or EARLIEST, self.end or utcnow() + timedelta(days=1))

    def as_dict(self) -> dict:
        return {"from": iso(self.start), "to": iso(self.end), "tz": self.tz}

    @property
    def hours(self) -> float | None:
        if self.start is None:
            return None
        return ((self.end or utcnow()) - self.start).total_seconds() / 3600


def _parse_point(s: str, *, is_end: bool, tz: ZoneInfo) -> datetime:
    s = s.strip()
    if s.isdigit():                       # 毫秒（Grafana 风格）或秒时间戳
        v = int(s)
        secs = v / 1000 if v > 10_000_000_000 else v
        return datetime.fromtimestamp(secs, timezone.utc).replace(tzinfo=None)
    try:
        if len(s) == 10:                  # YYYY-MM-DD：本地日历日
            d = date.fromisoformat(s)
            if is_end:
                d = d + timedelta(days=1)  # to=某天 表示含这一整天
            return datetime.combine(d, time(0), tz).astimezone(timezone.utc).replace(tzinfo=None)
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise HTTPException(400, f"bad date {s!r}: use YYYY-MM-DD or ISO-8601") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)        # 不带偏移的时刻按本地时间理解
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_period(days: int | None = None, from_: str | None = None, to: str | None = None,
                 default_days: int | None = None) -> Period:
    """from/to 优先；否则 days（0 = 全部）；都没给就 default_days（None = 全部）。"""
    tzname = local_tz_name()
    tz = ZoneInfo(tzname)
    start = end = None
    if from_ or to:
        start = _parse_point(from_, is_end=False, tz=tz) if from_ else None
        end = _parse_point(to, is_end=True, tz=tz) if to else None
    else:
        n = default_days if days is None else days
        if n is not None and int(n) > 0:
            # 上限 50 年：再大就是传错了，而 timedelta 会溢出成 500
            start = utcnow() - timedelta(days=min(int(n), 365 * 50))
    if start and end and start >= end:
        raise HTTPException(400, "'from' must be before 'to'")
    return Period(start=start, end=end, tz=tzname)


def clamp(v, lo: int, hi: int, default: int) -> int:
    """把 limit 这类参数夹进合理区间。负数、零、超大值都不该变成 SQL 错误。"""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(v, hi))
