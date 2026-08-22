"""取证包导出：一次打一个 ZIP，把事故需要的全部材料装齐。

为什么值得单独做：出事之后要交给保险或警方的，从来不只是一段视频。
对方会问"什么时候、在哪、多快、谁在开"，而这些答案分散在四个地方 ——
原始片段在 U 盘、时间在文件名、速度在 SEI、位置和电量在 TeslaMate。
用户在最慌乱的时候要自己拼这些，拼错一处整份材料的可信度就没了。

包里有什么，以及为什么：
  raw/                原始片段，逐字节复制。**不重新编码** —— 转码过的视频
                      在取证上价值大打折扣，对方有理由质疑画面被处理过。
  event.json          特斯拉自己写的事件元数据（触发原因、估算经纬度、城市）
  telemetry.csv       逐帧 SEI（车速、方向盘、踏板、挡位、自驾状态）
  drive.csv           同时段的 TeslaMate 记录（SOC、功率、海拔、内外温度）
  manifest.json       清单 + 每个文件的 SHA-256 + 时钟偏移
  README.txt          给不懂技术的人看的一页纸说明

manifest 里的校验和不是装样子：交出去之后对方可以验证文件没被改过，
而"我们自己算的时钟偏移是多少秒"这种事必须写在纸面上 ——
片段文件名用的是车机本地时钟，和 TeslaMate 的 UTC 之间有偏移，
不声明的话两份材料的时间对不上，反而成了破绽。
"""

import csv
import hashlib
import io
import json
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

README = """OhmyTesla 取证包 / Evidence package
=====================================

事件 / Event: {event_id}
类型 / Type: {event_type}
触发时刻 / Triggered: {when} ({tz})
地点 / Location: {place}

这个压缩包由车主自己的 OhmyTesla 实例生成，所有处理都在本机完成。

目录 / Contents
---------------
raw/            行车记录仪原始片段，逐字节复制，未经任何重新编码。
event.json      车辆自己写入的事件元数据（触发原因、估算坐标）。
telemetry.csv   逐帧车辆遥测，从视频码流内嵌的 SEI 数据解出：
                车速、方向盘转角、油门/刹车、挡位、自动驾驶状态。
drive.csv       同一时段的行车记录（电量、功率、海拔、内外温度、坐标）。
manifest.json   文件清单与每个文件的 SHA-256 校验和。

时钟说明 / Clock
----------------
片段文件名使用车辆本机时钟；行车记录使用 UTC。
本包采用的偏移量：{offset} 秒（车机时钟 = 真实时间 + 偏移）。
两份数据的时间戳已按此对齐。

校验 / Verification
-------------------
manifest.json 里每个文件都附了 SHA-256。任何一位改动都会导致校验失败。
在 macOS / Linux 上核对：  shasum -a 256 <文件名>
"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def build(event_id: str, car_id: int | None = None) -> tuple[bytes, str]:
    """打包并返回 (zip 字节, 建议的文件名)。

    异步的部分（查库、取时钟偏移）在这里做完，真正阻塞的读文件 + 压缩
    扔进线程池 —— 一个 6 机位事件几百 MB，在事件循环里压会让整个门户
    在这几十秒里完全无响应（uvicorn 单进程，没有第二个 worker 顶上）。
    """
    from .. import db
    from . import telemetry
    from .scanner import _teslacam_base, scanner

    base = _teslacam_base()
    if base is None:
        raise ValueError("TeslaCam directory not found")
    ev = scanner.get(event_id)
    if ev is None:
        raise ValueError(f"event not found: {event_id}")

    if car_id is None:
        try:
            row = await db.fetchrow("SELECT id FROM cars ORDER BY id LIMIT 1")
            car_id = row["id"] if row else 1
        except db.DbUnavailable:
            car_id = 1

    offset = await telemetry.clock_offset(car_id)

    # 同时段的行车记录：这一步要查库，必须留在协程里
    drive_rows = []
    drive_err = None
    try:
        start_utc = telemetry.local_naive_to_utc(ev.segments[0].start) - timedelta(seconds=offset)
        end_utc = telemetry.local_naive_to_utc(ev.segments[-1].start) + timedelta(minutes=2)
        drive_rows = await telemetry.fetch_positions(car_id, start_utc, end_utc, pad_s=60)
    except Exception as e:
        log.warning("evidence: could not attach drive data", exc_info=True)
        drive_err = str(e)[:200]

    import asyncio

    return await asyncio.to_thread(_pack, ev, base, offset, drive_rows, drive_err)


def _pack(ev, base: Path, offset: float,
          drive_rows: list, drive_err: str | None) -> tuple[bytes, str]:
    """纯同步：读文件、算校验和、压缩。由 build() 在线程池里调用。"""
    from . import telemetry

    buf = io.BytesIO()
    manifest: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event_id": ev.id,
        "event_type": ev.type,
        "event_timestamp_local": ev.timestamp.isoformat(),
        "clock_offset_seconds": offset,
        "files": {},
        # 把口径写进包里。收件方不该去猜我们的 "speed" 是 km/h 还是 mph。
        "units": {"speed": "km/h", "distance": "km", "temperature": "C", "power": "kW"},
        "source": "OhmyTesla — self-hosted, processed locally",
    }

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        # ── 原始片段。压缩级别故意设成 1：mp4 本来就压过了，
        #    用 9 只会多花几分钟 CPU 换不到 1% 体积。
        for seg in ev.segments:
            for cam, rel in seg.cameras.items():
                src = base / rel
                if not src.is_file():
                    continue
                data = src.read_bytes()
                arc = f"raw/{Path(rel).name}"
                z.writestr(arc, data)
                manifest["files"][arc] = {
                    "sha256": _sha256(data), "bytes": len(data), "camera": cam,
                }

        # ── 车辆自己写的事件元数据
        meta = {"reason": ev.reason, "city": ev.city,
                "est_lat": ev.est_lat, "est_lon": ev.est_lon,
                "timestamp": ev.timestamp.isoformat(), "type": ev.type}
        blob = json.dumps(meta, ensure_ascii=False, indent=2).encode()
        z.writestr("event.json", blob)
        manifest["files"]["event.json"] = {"sha256": _sha256(blob), "bytes": len(blob)}

        # ── 逐帧 SEI
        rows = []
        for seg in ev.segments:
            picked = next((seg.cameras[c] for c in ("front", "F", "main") if c in seg.cameras),
                          next(iter(seg.cameras.values()), None))
            if not picked:
                continue
            from . import sei

            for smp in sei.extract(base / picked, step=0.2, strict=True):
                rows.append({"segment_start": seg.start.isoformat(), **smp})
        if rows:
            sio = io.StringIO()
            w = csv.DictWriter(sio, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
            blob = sio.getvalue().encode()
            z.writestr("telemetry.csv", blob)
            manifest["files"]["telemetry.csv"] = {
                "sha256": _sha256(blob), "bytes": len(blob), "samples": len(rows)}
        else:
            # 没有就明说，别让收件方以为是我们漏了
            manifest["telemetry_note"] = (
                "No SEI telemetry: parked Sentry clips never carry it, and neither do "
                "driving clips from firmware older than 2025.44.25.")

        # ── 同时段的 TeslaMate 记录（数据已由 build() 在协程里取好）
        if drive_rows:
            sio = io.StringIO()
            w = csv.DictWriter(sio, fieldnames=list(drive_rows[0].keys()))
            w.writeheader()
            w.writerows(drive_rows)
            blob = sio.getvalue().encode()
            z.writestr("drive.csv", blob)
            manifest["files"]["drive.csv"] = {
                "sha256": _sha256(blob), "bytes": len(blob), "samples": len(drive_rows)}
        elif drive_err:
            manifest["drive_note"] = f"TeslaMate data was unavailable: {drive_err}"

        # ── 说明书
        readme = README.format(
            event_id=ev.id, event_type=ev.type,
            when=ev.timestamp.isoformat(sep=" "), tz=str(telemetry.local_tz()),
            place=ev.city or (f"{ev.est_lat}, {ev.est_lon}" if ev.est_lat else "unknown"),
            offset=round(offset, 1),
        ).encode()
        z.writestr("README.txt", readme)
        manifest["files"]["README.txt"] = {"sha256": _sha256(readme), "bytes": len(readme)}

        # manifest 最后写：它要包含前面所有文件的校验和，自己不参与校验
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    stamp = ev.timestamp.strftime("%Y%m%d-%H%M%S")
    return buf.getvalue(), f"ohmytesla-evidence-{ev.type}-{stamp}.zip"
