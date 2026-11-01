"""时钟漂移自动校正。

TeslaCam 文件名时间与 TeslaMate 遥测时间可能存在数秒到数十秒的偏差
（车机时钟、录制启动延迟）。这里用 SEI 逐帧速度序列与 positions 速度
序列做滑动互相关（最小化 RMSE），求出每车的精确偏移并持久化，
之后所有遥测叠加/播放器 HUD 自动应用。
"""

import asyncio
import bisect
import logging
import statistics
from datetime import datetime, timezone

from . import sei, telemetry
from .localtime import local_naive_to_utc
from .scanner import Event


class AlignRefused(ValueError):
    """Calibration declined. `key`/`vars` let the router say why in the
    owner's language; str() is the Chinese text, for logs."""

    def __init__(self, key: str, **vars_) -> None:
        from ..i18n.router import t

        self.key, self.vars = key, vars_
        super().__init__(t(key, "zh", **vars_))

log = logging.getLogger(__name__)

# 接受一个拟合的门槛。SEI 车速和 TeslaMate 车速对得上时 RMSE 一般在 1-3 km/h；
# 拿互不相关的两条曲线硬算，也总能"找到"一个最小值（实测对噪声给出 −3.25 s、
# RMSE 18.6 km/h），而这个偏移一旦存下来，会挪动这辆车之后所有的叠加层、
# HUD、CSV 和取证包。所以：拟合不好就不存，告诉用户为什么。
MAX_RMSE_KMH = 6.0
# 最优偏移的 RMSE 必须明显好于其它偏移（中位数），否则说明曲线没有可辨识的特征
MAX_RMSE_RATIO = 0.5
# 车速几乎不变（巡航/静止）时任何偏移都"对得上"，无从判断
MIN_SPEED_STDEV_KMH = 3.0


def _interp(ts: list[float], vs: list[float], t: float) -> float | None:
    i = bisect.bisect_left(ts, t)
    if i <= 0 or i >= len(ts):
        return None
    t0, t1 = ts[i - 1], ts[i]
    if t1 - t0 > 15:  # 采样断档
        return None
    frac = (t - t0) / (t1 - t0) if t1 > t0 else 0
    return vs[i - 1] + (vs[i] - vs[i - 1]) * frac


def _rmse(sei_series: list[tuple[float, float]], ts: list[float], vs: list[float], offset: float) -> tuple[float, int]:
    err = 0.0
    n = 0
    for t, v in sei_series:
        pv = _interp(ts, vs, t + offset)
        if pv is None:
            continue
        err += (v - pv) ** 2
        n += 1
    if n < 20:
        return float("inf"), n
    return (err / n) ** 0.5, n


def _fit(sei_series: list[tuple[float, float]], ts: list[float], vs: list[float]) -> dict:
    """纯 CPU 的互相关（一小时的事件要半秒以上），由 auto_align 放进线程跑。"""
    # 粗扫 ±120s @1s。不同偏移下与遥测的重叠样本数差异很大——
    # 只有极少重叠的偏移可能凭巧合 RMSE 更小，因此先求最大重叠数，
    # 再只在重叠 ≥ 60% 最大值的偏移里选最小 RMSE。
    coarse = [(float(off), *_rmse(sei_series, ts, vs, float(off))) for off in range(-120, 121)]
    max_n = max((n for _, r, n in coarse if r != float("inf")), default=0)
    if max_n == 0:
        raise AlignRefused("align.no_overlap")
    eligible = [(off, r, n) for off, r, n in coarse if n >= max_n * 0.6 and r != float("inf")]
    best_off, best_rmse, best_n = min(eligible, key=lambda x: x[1])
    median_rmse = statistics.median(r for _, r, _ in eligible)

    # 细化 @0.25s（沿用同一重叠门槛；窗口锚定粗扫结果，不随更新漂移）
    fine_base = best_off
    for k in range(-8, 9):
        off = fine_base + k * 0.25
        r, n = _rmse(sei_series, ts, vs, off)
        if n >= max_n * 0.6 and r < best_rmse:
            best_off, best_rmse, best_n = off, r, n
    return {"offset": best_off, "rmse": best_rmse, "samples": best_n, "median_rmse": median_rmse}


async def auto_align(event: Event, base, car_id: int) -> dict:
    """互相关求偏移。返回 {offset, rmse_kmh, samples, ...}；数据不足或拟合不可信时抛 ValueError。"""
    sei_series: list[tuple[float, float]] = []
    for seg in event.segments:
        rel = seg.cameras.get("front") or next(iter(seg.cameras.values()), None)
        if not rel:
            continue
        samples = await asyncio.to_thread(sei.extract, base / rel, 0.5)
        base_ts = local_naive_to_utc(seg.start).timestamp()
        for s in samples:
            if s.get("speed_kmh") is not None:
                sei_series.append((base_ts + s["t"], s["speed_kmh"]))

    if len(sei_series) < 60:
        raise AlignRefused("align.no_sei")
    if statistics.pstdev(v for _, v in sei_series) < MIN_SPEED_STDEV_KMH:
        raise AlignRefused("align.flat_speed")

    t_lo = min(t for t, _ in sei_series) - 180
    t_hi = max(t for t, _ in sei_series) + 180
    positions = await telemetry.fetch_positions(
        car_id,
        datetime.fromtimestamp(t_lo, timezone.utc),
        datetime.fromtimestamp(t_hi, timezone.utc),
        pad_s=0,
    )
    ts = [p["date"] for p in positions if p.get("speed") is not None]
    vs = [p["speed"] for p in positions if p.get("speed") is not None]
    if len(ts) < 30:
        raise AlignRefused("align.no_telemetry")

    fit = await asyncio.to_thread(_fit, sei_series, ts, vs)
    best_off, best_rmse, best_n = fit["offset"], fit["rmse"], fit["samples"]
    quality = {"offset": round(best_off, 2), "rmse_kmh": round(best_rmse, 2), "samples": best_n,
               "median_rmse_kmh": round(fit["median_rmse"], 2)}

    if best_rmse > MAX_RMSE_KMH:
        log.info("clock align rejected (rmse %.2f): car=%s offset=%.2f", best_rmse, car_id, best_off)
        raise AlignRefused("align.poor_fit", off=f"{best_off:+.2f}", rmse=f"{best_rmse:.1f}",
                           max=f"{MAX_RMSE_KMH:g}")
    if fit["median_rmse"] > 0 and best_rmse > fit["median_rmse"] * MAX_RMSE_RATIO:
        log.info("clock align rejected (no distinct minimum): car=%s %s", car_id, quality)
        raise AlignRefused("align.no_minimum", off=f"{best_off:+.2f}", rmse=f"{best_rmse:.1f}",
                           median=f"{fit['median_rmse']:.1f}")

    await telemetry.set_clock_offset(car_id, best_off)
    log.info("clock aligned: car=%s offset=%.2fs rmse=%.2f n=%d", car_id, best_off, best_rmse, best_n)
    return {**quality, "accepted": True}
