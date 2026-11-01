"""从 TeslaMate positions 表按时间戳提取遥测，并生成视频叠加层。

核心思路：TeslaCam 文件名携带车机本地时间；TeslaMate 的 positions.date
是 UTC 微秒级时间戳。把两者对齐后，就能给每一秒视频配上
速度 / 功率 / 电量 / 海拔 / 温度 / GPS 等数据。

叠加层用 ASS 字幕实现（libass 由 ffmpeg 内置渲染），
相比逐帧 drawtext 性能好得多，且支持中文（Noto Sans CJK）。
"""

import bisect
import logging
from datetime import datetime, timedelta, timezone

from .. import db
from ..i18n import units

# 时区换算挪到了 localtime.py（按每个时刻各自的夏令时规则算）。这里保留同名
# 入口：insights / alerts 的路由也在用 telemetry.local_tz()
from .localtime import local_naive_to_utc, local_tz  # noqa: F401 - 对外再导出

log = logging.getLogger(__name__)

NUMERIC_FIELDS = [
    "speed", "power", "battery_level", "usable_battery_level",
    "elevation", "odometer", "outside_temp", "inside_temp",
    "rated_battery_range_km", "latitude", "longitude",
]


async def clock_offset(car_id: int) -> float:
    """已校准的每车时钟偏移（秒）。视频文件名时间 + 偏移 = TeslaMate 时间。"""
    from ..alerts.store import store

    try:
        offsets = await store.get_setting("clock_offsets", {}) or {}
        return float(offsets.get(str(car_id), 0.0))
    except Exception:  # noqa: BLE001
        return 0.0


async def set_clock_offset(car_id: int, offset: float) -> None:
    from ..alerts.store import store

    offsets = await store.get_setting("clock_offsets", {}) or {}
    offsets[str(car_id)] = round(offset, 2)
    await store.set_setting("clock_offsets", offsets)


async def fetch_positions(car_id: int, start_utc: datetime, end_utc: datetime, pad_s: int = 15) -> list[dict]:
    # positions.date 是 timestamp WITHOUT time zone（值为 UTC）——
    # 必须绑定 naive datetime，取回后也要显式打上 UTC 再转 epoch
    rows = await db.fetch(
        """
        SELECT date, speed, power, battery_level, usable_battery_level,
               elevation, odometer, outside_temp, inside_temp,
               rated_battery_range_km, latitude, longitude
        FROM positions
        WHERE car_id = $1 AND date BETWEEN $2 AND $3
        ORDER BY date
        """,
        car_id,
        (start_utc - timedelta(seconds=pad_s)).astimezone(timezone.utc).replace(tzinfo=None),
        (end_utc + timedelta(seconds=pad_s)).astimezone(timezone.utc).replace(tzinfo=None),
    )
    out = []
    for r in rows:
        d = {"date": r["date"].replace(tzinfo=timezone.utc).timestamp()}
        for f in NUMERIC_FIELDS:
            v = r[f]
            d[f] = float(v) if v is not None else None
        out.append(d)
    return out


SLOW_FIELDS = {"battery_level", "usable_battery_level", "odometer", "rated_battery_range_km"}


def resample(samples: list[dict], t0: float, t1: float, step: float = 0.5, max_gap: float = 30.0) -> list[dict]:
    """把不规则的 positions 采样线性插值到固定步长时间轴上。

    逐字段找"这个字段自己"前后最近的有值采样（positions 里很多列是间歇
    写入的，相邻行的值经常是 NULL）：
    - 前后都有、且相隔不超过 max_gap → 线性插值；
    - 否则只在离某一侧不超过 max_gap/2 时沿用那一侧的值；
    - 都不满足 → None（停车休眠的两小时里不能显示 95 km/h）。
    慢变量（电量、里程、续航）例外：沿用最后已知值，休眠前后它们本来就没怎么变。

    以前的实现只看相邻两行，一侧为 NULL 时直接拿另一侧的值，跨多长的空档都照拿。
    """
    result: list[dict] = []
    if not samples:
        return result
    hold = max_gap / 2
    series: dict[str, tuple[list[float], list[float]]] = {}
    for f in NUMERIC_FIELDS:
        ts, vs = [], []
        for s in samples:
            v = s.get(f)
            if v is not None:
                ts.append(s["date"])
                vs.append(v)
        series[f] = (ts, vs)

    n = int((t1 - t0) / step + 1e-9) + 1 if t1 >= t0 else 0
    for k in range(n):
        t = t0 + k * step
        cur: dict = {"t": round(t - t0, 3)}
        for f, (ts, vs) in series.items():
            if not ts:
                cur[f] = None
                continue
            j = bisect.bisect_right(ts, t)          # ts[j-1] <= t < ts[j]
            prev = j - 1 if j > 0 else None
            nxt = j if j < len(ts) else None
            if prev is not None and nxt is not None and ts[prev] != t and ts[nxt] - ts[prev] <= max_gap:
                frac = (t - ts[prev]) / (ts[nxt] - ts[prev])
                cur[f] = vs[prev] + (vs[nxt] - vs[prev]) * frac
            elif prev is not None and (ts[prev] == t or f in SLOW_FIELDS or t - ts[prev] <= hold):
                cur[f] = vs[prev]
            elif nxt is not None and ts[nxt] - t <= hold:
                cur[f] = vs[nxt]
            else:
                cur[f] = None
        result.append(cur)
    return result


# ---------------------------------------------------------------- ASS 生成

def _ass_time(seconds: float) -> str:
    seconds = max(seconds, 0)
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


_LABELS = {
    "zh": {"power": "功率", "soc": "电量", "range": "续航", "elev": "海拔",
           "temp": "外温", "odo": "里程", "nodata": "· 无遥测数据（车辆休眠）",
           "steer": "方向盘", "pedal": "油门", "brake": "刹车"},
    "en": {"power": "PWR", "soc": "SOC", "range": "RNG", "elev": "ELEV",
           "temp": "TEMP", "odo": "ODO", "nodata": "· no telemetry (vehicle asleep)",
           "steer": "STEER", "pedal": "PEDAL", "brake": "BRAKE"},
}
# 注：单位名不写进这张表——它由用户的单位偏好决定，与语言无关
# （英文 + 公制的加拿大用户要的是 "km/h" 配英文标签）。


def build_ass(
    overlay: list[dict],
    width: int,
    height: int,
    clip_start_local: datetime,
    step: float = 0.5,
    lang: str = "zh",
    sei: list[dict] | None = None,
    prefs: dict | None = None,
) -> str:
    """生成叠加 ASS 字幕。

    底部半透明信息条: TeslaMate 遥测（速度/功率/电量/海拔/温度/里程/时钟/GPS）。
    左上角仪表簇: SEI 逐帧遥测（档位/AP 状态/方向盘/踏板/转向灯/横向 G），
    仅当片段含 SEI（固件 2025.44.25+ 且行驶中）时出现。
    SEI 速度精度更高（30+ Hz），有 SEI 时大速度数字优先用 SEI 值。
    """
    L = _LABELS.get(lang, _LABELS["zh"])
    # 烧进视频的单位必须跟用户偏好走。这是全系统唯一**不可逆**的地方：
    # ASS 被压进 H.264 之后改不了，而原始片段可能几天后就被车覆盖了。
    P = prefs or {"length": "km", "temp": "C", "pressure": "bar"}
    bar_h = int(height * 0.105)
    bar_y = height - bar_h
    font = "Noto Sans CJK SC"

    big = int(bar_h * 0.62)
    small = int(bar_h * 0.30)
    tiny = int(bar_h * 0.24)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Panel,{font},20,&H50000000,&H50000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Big,{font},{big},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,7,0,0,0,1
Style: Small,{font},{small},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1
Style: Label,{font},{tiny},&H00AAAAAA,&H00AAAAAA,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines: list[str] = []
    total = overlay[-1]["t"] + step if overlay else 0

    # SEI 按时间桶索引，便于逐步查最近样本
    sei_by_bucket: dict[int, dict] = {}
    if sei:
        for s in sei:
            # 存和取必须用同一种取整（以前存用 int()、取用 round()，
            # 帧率略高于整数时有九成的步会提前半秒显示）
            sei_by_bucket.setdefault(round(s["t"] / step), s)

    # 底部半透明背景条（整段时长一条即可）
    lines.append(
        f"Dialogue: 0,{_ass_time(0)},{_ass_time(total)},Panel,,0,0,0,,"
        f"{{\\pos(0,{bar_y})\\p1\\alpha&H50&\\c&H000000&}}"
        f"m 0 0 l {width} 0 l {width} {bar_h} l 0 {bar_h}{{\\p0}}"
    )

    pad = int(width * 0.015)
    col_time_x = width - pad
    bar_y + int(bar_h * 0.16)
    bar_y + int(bar_h * 0.60)

    for k, s in enumerate(overlay):
        t_start = s["t"]
        # 结束时刻不越过下一步的开始：分段交界处两步挨得很近时，
        # 两条字幕同时显示会叠成一团
        t_end = t_start + step
        if k + 1 < len(overlay):
            t_end = min(t_end, overlay[k + 1]["t"])
        if t_end <= t_start:
            continue
        st, en = _ass_time(t_start), _ass_time(t_end)
        speed = s.get("speed")
        power = s.get("power")
        soc = s.get("usable_battery_level") or s.get("battery_level")
        rng = s.get("rated_battery_range_km")
        elev = s.get("elevation")
        temp = s.get("outside_temp")
        odo = s.get("odometer")
        lat, lon = s.get("latitude"), s.get("longitude")

        se = sei_by_bucket.get(round(t_start / step))
        if se and se.get("speed_kmh") is not None:
            speed = se["speed_kmh"]  # SEI 逐帧速度更精确

        # 大号速度（无数据时显示 --）
        sp_v, sp_u = units.speed(speed, P)
        speed_txt = f"{sp_v:.0f}" if sp_v is not None else "--"
        lines.append(
            f"Dialogue: 2,{st},{en},Big,,0,0,0,,"
            f"{{\\pos({pad},{bar_y + int(bar_h * 0.08)})}}{speed_txt}"
        )
        lines.append(
            f"Dialogue: 2,{st},{en},Label,,0,0,0,,"
            f"{{\\pos({pad + int(big * 1.9)},{bar_y + int(bar_h * 0.52)})}}{sp_u}"
        )

        # 数据列: 功率 / 电量 / 续航 / 海拔 / 外温 / 里程
        cols: list[tuple[str, str]] = []
        if power is not None:
            cols.append((L["power"], f"{power:+.0f} kW"))
        if soc is not None:
            txt = f"{soc:.0f}%"
            if rng:
                rv, ru = units.dist(rng, P, 0)
                txt += f" · {rv:.0f} {ru}"
            cols.append((L["soc"], txt))
        if elev is not None:
            # 海拔：英里制用英尺
            if P.get("length") == "mi":
                cols.append((L["elev"], f"{elev * 3.28084:.0f} ft"))
            else:
                cols.append((L["elev"], f"{elev:.0f} m"))
        if temp is not None:
            tv, tu = units.temp(temp, P)
            cols.append((L["temp"], f"{tv:.1f}{tu}"))
        if odo is not None:
            ov, ou = units.dist(odo, P, 0)
            cols.append((L["odo"], f"{ov:,.0f} {ou}"))
        if not cols:
            cols.append(("", L["nodata"]))

        x = pad + int(big * 3.2)
        col_w = int((width - x - width * 0.22) / max(len(cols), 1))
        for label, value in cols:
            if label:
                lines.append(
                    f"Dialogue: 2,{st},{en},Label,,0,0,0,,{{\\pos({x},{bar_y + int(bar_h * 0.10)})}}{label}"
                )
            lines.append(
                f"Dialogue: 2,{st},{en},Small,,0,0,0,,{{\\pos({x},{bar_y + int(bar_h * 0.42)})}}{value}"
            )
            x += col_w

        # 右侧: 真实时钟 + GPS（优先用样本自带的墙钟——跨分段间隙时线性推算会错）
        clock = s.get("clock") or (clip_start_local + timedelta(seconds=t_start)).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(
            f"Dialogue: 2,{st},{en},Small,,0,0,0,,"
            f"{{\\an9\\pos({col_time_x},{bar_y + int(bar_h * 0.10)})}}{clock}"
        )
        if lat is not None and lon is not None:
            lines.append(
                f"Dialogue: 2,{st},{en},Label,,0,0,0,,"
                f"{{\\an9\\pos({col_time_x},{bar_y + int(bar_h * 0.55)})}}{lat:.5f}, {lon:.5f}"
            )

        # ---- 左上角 SEI 仪表簇（档位 / AP / 转向灯 / 方向盘 / 踏板 / G 值）----
        if se:
            top_y = int(height * 0.018)
            gear_fs = int(bar_h * 0.50)
            info_fs = int(bar_h * 0.26)
            gear = se.get("gear") or ""
            if gear:
                gear_col = "\\c&H2659EB&" if gear == "R" else "\\c&HFFFFFF&"
                lines.append(
                    f"Dialogue: 2,{st},{en},Big,,0,0,0,,"
                    f"{{\\pos({pad},{top_y})\\fs{gear_fs}{gear_col}}}{gear}"
                )
            badges = []
            if se.get("ap"):
                badges.append(("&H7AAF1B&", se["ap"]))
            if se.get("blinker_left"):
                badges.append(("&H0CA30C&", "◀"))
            if se.get("blinker_right"):
                badges.append(("&H0CA30C&", "▶"))
            if badges:
                badge_txt = "  ".join(f"{{\\c{c}}}{t}" for c, t in badges)
                lines.append(
                    f"Dialogue: 2,{st},{en},Small,,0,0,0,,"
                    f"{{\\pos({pad + int(gear_fs * 1.1)},{top_y + int(gear_fs * 0.12)})\\fs{info_fs}}}{badge_txt}"
                )
            info_parts = []
            if se.get("steering_angle") is not None:
                info_parts.append(f"{L['steer']} {se['steering_angle']:+.0f}°")
            if se.get("accel_pedal") is not None:
                info_parts.append(f"{L['pedal']} {se['accel_pedal']:.0f}%")
            ax, ay = se.get("accel_x"), se.get("accel_y")
            if ax is not None and ay is not None:
                info_parts.append(f"{(ax * ax + ay * ay) ** 0.5 / 9.81:.2f}g")
            info_txt = "  ·  ".join(info_parts)
            if se.get("brake"):
                info_txt += f"  {{\\c&H3B3BD0&}}● {L['brake']}"
            if info_txt:
                lines.append(
                    f"Dialogue: 2,{st},{en},Small,,0,0,0,,"
                    f"{{\\pos({pad},{top_y + int(gear_fs * 1.15)})\\fs{info_fs}}}{info_txt}"
                )

    lines.extend(_track_widget(overlay, width, height, pad, step))

    return header + "\n".join(lines) + "\n"


def _track_widget(overlay: list[dict], width: int, height: int, pad: int, step: float) -> list[str]:
    """右上角轨迹小窗：整段路径（点阵折线）+ 随时间移动的当前位置点。

    ASS 矢量绘图只支持填充，不支持描边线条，所以路径用一串 2px 小方块
    表现；当前位置是一个大号高亮方块。无外部依赖、任何 libass 都能渲染。
    """
    import math

    coords = [
        (s["latitude"], s["longitude"], s["t"])
        for s in overlay
        if s.get("latitude") is not None and s.get("longitude") is not None
    ]
    if len({(c[0], c[1]) for c in coords}) < 8:
        return []
    lat0 = sum(c[0] for c in coords) / len(coords)
    kx = math.cos(math.radians(lat0))
    xs = [c[1] * kx for c in coords]
    ys = [c[0] for c in coords]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    if span * 111_000 < 80:  # 移动不足 ~80 米（停车哨兵）不画
        return []

    bw = int(width * 0.16)
    bh = bw
    bx = width - bw - pad
    by = int(height * 0.02)
    inner = int(bw * 0.10)
    x_min, y_min = min(xs), min(ys)

    def px(lat: float, lon: float) -> tuple[int, int]:
        x = (lon * kx - x_min) / span
        y = (lat - y_min) / span
        return (
            int(bx + inner + x * (bw - 2 * inner)),
            int(by + (bh - inner) - y * (bh - 2 * inner)),
        )

    total = overlay[-1]["t"] + step
    out: list[str] = []
    # 背板
    out.append(
        f"Dialogue: 0,{_ass_time(0)},{_ass_time(total)},Panel,,0,0,0,,"
        f"{{\\pos({bx},{by})\\p1\\alpha&H60&\\c&H000000&}}"
        f"m 0 0 l {bw} 0 l {bw} {bh} l 0 {bh}{{\\p0}}"
    )
    # 路径点阵（最多 ~180 个点）
    stride = max(1, len(coords) // 180)
    dots = []
    for lat, lon, _t in coords[::stride]:
        x, y = px(lat, lon)
        dots.append(f"m {x} {y} l {x + 2} {y} l {x + 2} {y + 2} l {x} {y + 2}")
    out.append(
        f"Dialogue: 1,{_ass_time(0)},{_ass_time(total)},Panel,,0,0,0,,"
        f"{{\\pos(0,0)\\p1\\alpha&H40&\\c&HFFFFFF&}}" + " ".join(dots) + "{\\p0}"
    )
    # 当前位置点（每步一条）
    r = max(3, bw // 60)
    for lat, lon, t in coords:
        x, y = px(lat, lon)
        out.append(
            f"Dialogue: 2,{_ass_time(t)},{_ass_time(t + step)},Panel,,0,0,0,,"
            f"{{\\pos(0,0)\\p1\\c&HD6782A&}}"
            f"m {x - r} {y - r} l {x + r} {y - r} l {x + r} {y + r} l {x - r} {y + r}{{\\p0}}"
        )
    return out
