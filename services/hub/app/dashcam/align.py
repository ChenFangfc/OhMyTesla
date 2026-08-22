"""时钟漂移自动校正。

TeslaCam 文件名时间与 TeslaMate 遥测时间可能存在数秒到数十秒的偏差
（车机时钟、录制启动延迟）。这里用 SEI 逐帧速度序列与 positions 速度
序列做滑动互相关（最小化 RMSE），求出每车的精确偏移并持久化，
之后所有遥测叠加/播放器 HUD 自动应用。
"""

import asyncio
import bisect
import logging
from datetime import datetime, timezone

from . import sei, telemetry
from .scanner import Event

log = logging.getLogger(__name__)


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


async def auto_align(event: Event, base, car_id: int) -> dict:
    """互相关求偏移。返回 {offset, rmse, samples}；数据不足时抛 ValueError。"""
    sei_series: list[tuple[float, float]] = []
    for seg in event.segments:
        rel = seg.cameras.get("front") or next(iter(seg.cameras.values()), None)
        if not rel:
            continue
        samples = await asyncio.to_thread(sei.extract, base / rel, 0.5)
        base_ts = telemetry.local_naive_to_utc(seg.start).timestamp()
        for s in samples:
            if s.get("speed_kmh") is not None:
                sei_series.append((base_ts + s["t"], s["speed_kmh"]))

    if len(sei_series) < 60:
        raise ValueError(
            "该事件的视频没有足够的 SEI 遥测（旧固件、停车哨兵片段或加密片段），无法自动校准"
        )

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
        raise ValueError("该时间段 TeslaMate 没有足够的行驶遥测（车辆未在行驶？）")

    # 粗扫 ±120s @1s。不同偏移下与遥测的重叠样本数差异很大——
    # 只有极少重叠的偏移可能凭巧合 RMSE 更小，因此先求最大重叠数，
    # 再只在重叠 ≥ 60% 最大值的偏移里选最小 RMSE。
    coarse = [(float(off), *_rmse(sei_series, ts, vs, float(off))) for off in range(-120, 121)]
    max_n = max((n for _, r, n in coarse if r != float("inf")), default=0)
    if max_n == 0:
        raise ValueError("互相关失败：视频时间段与遥测无重叠")
    eligible = [(off, r, n) for off, r, n in coarse if n >= max_n * 0.6 and r != float("inf")]
    best_off, best_rmse, best_n = min(eligible, key=lambda x: x[1])

    # 细化 @0.25s（沿用同一重叠门槛；窗口锚定粗扫结果，不随更新漂移）
    fine_base = best_off
    for k in range(-8, 9):
        off = fine_base + k * 0.25
        r, n = _rmse(sei_series, ts, vs, off)
        if n >= max_n * 0.6 and r < best_rmse:
            best_off, best_rmse, best_n = off, r, n

    await telemetry.set_clock_offset(car_id, best_off)
    log.info("clock aligned: car=%s offset=%.2fs rmse=%.2f n=%d", car_id, best_off, best_rmse, best_n)
    return {
        "offset": round(best_off, 2),
        "rmse_kmh": round(best_rmse, 2),
        "samples": best_n,
    }
