"""取证包（审计 D1 / D6）。

  - D1：流式打包，内存峰值和事件大小无关（以前整包在内存里拼，峰值 ≈ 片段总量 × 2.4）
  - 打出来的 ZIP 完整，raw/ 下每个文件的 SHA-256 和原文件、manifest 一致
  - D6：README 里偏移的方向写对（车机时钟 + 偏移 = 真实时间）；telemetry.csv 的
        timestamp_utc 真的按偏移换算过；drive.csv 的查询窗口两端都加了偏移
  - 下载方中途断开时，打包线程会停下来

跑法：python3 services/hub/tests/test_dashcam_evidence.py（需要 hub 运行时依赖；缺依赖时跳过）
"""

import asyncio
import hashlib
import io
import json
import os
import resource
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
TMP = Path(tempfile.mkdtemp(prefix="omt-evidence-"))
ROOT = TMP / "teslacam"
os.environ.update(DATA_DIR=str(TMP / "data"), TESLACAM_ROOT=str(ROOT), TESLACAM_TZ="America/Chicago")
(TMP / "data").mkdir()
ROOT.mkdir()
try:
    import aiosqlite  # noqa: F401
    import pydantic_settings  # noqa: F401
except ImportError:
    print("— skipped: needs the hub runtime (aiosqlite, pydantic-settings); run with tests/run.sh --docker")
    sys.exit(0)

from app import db  # noqa: E402
from app.alerts.store import store  # noqa: E402
from app.dashcam import evidence, sei, telemetry  # noqa: E402
from app.dashcam.localtime import local_naive_to_utc  # noqa: E402

FAILS: list[str] = []
MB = 1 << 20


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def peak_rss_mb() -> float:
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v / MB if sys.platform == "darwin" else v / 1024


WINDOWS: list[tuple] = []


async def fake_positions(car_id, start_utc, end_utc, pad_s=15):
    WINDOWS.append((start_utc, end_utc, pad_s))
    t = start_utc.timestamp()
    return [{"date": t + k, "speed": 30.0, "battery_level": 80.0} for k in range(3)]


async def fetchrow(*a, **k):
    return {"id": 1}


async def collect(stream, limit_chunks: int | None = None) -> bytes:
    buf = io.BytesIO()
    n = 0
    async for chunk in stream:
        buf.write(chunk)
        n += 1
        if limit_chunks and n >= limit_chunks:
            await stream.aclose()
            break
    return buf.getvalue()


async def main():
    await store.open()
    telemetry.fetch_positions = fake_positions
    db.fetchrow = fetchrow
    # 已知答案的 SEI 采样：每段 t = 0, 0.2, 0.4
    sei.extract = lambda path, step=0.5, strict=False: [
        {"t": round(k * 0.2, 2), "gear": "D", "speed_kmh": 40.0} for k in range(3)]
    try:
        ev_dir = ROOT / "SentryClips/2026-10-30_21-15-00"
        ev_dir.mkdir(parents=True)
        sizes = {}
        for m in ("21-13-00", "21-14-00"):
            for cam in ("front", "back"):
                p = ev_dir / f"2026-10-30_{m}-{cam}.mp4"
                with p.open("wb") as f:                      # 随机字节：压不小，最能体现内存问题
                    for _ in range(60):
                        f.write(os.urandom(MB))
                sizes[p.name] = p.stat().st_size
        (ev_dir / "event.json").write_text(json.dumps({"timestamp": "2026-10-30T21:15:00", "city": "Springfield"}))
        await telemetry.set_clock_offset(1, 90.0)

        print("D1 流式打包")
        before = peak_rss_mb()
        stream, name = await evidence.build("SentryClips/2026-10-30_21-15-00")
        out = TMP / name
        t0 = time.monotonic()
        with out.open("wb") as f:
            async for chunk in stream:
                f.write(chunk)
        took = time.monotonic() - t0
        grew = peak_rss_mb() - before
        total = sum(sizes.values()) / MB
        chk(f"打包 {total:.0f} MB 片段，内存峰值只涨 {grew:.0f} MB（< 64 MB）", grew < 64, f"{took:.1f}s")
        z = zipfile.ZipFile(out)
        chk("ZIP 完整（testzip 无坏条目）", z.testzip() is None)
        man = json.loads(z.read("manifest.json"))
        ok = True
        for fname in sizes:
            arc = f"raw/{fname}"
            h = hashlib.sha256()
            with z.open(arc) as f:
                while b := f.read(MB):
                    h.update(b)
            src = hashlib.sha256((ev_dir / fname).read_bytes()).hexdigest()
            ok &= h.hexdigest() == src == man["files"][arc]["sha256"]
        chk("raw/ 下每个文件的 SHA-256 = 原文件 = manifest", ok)
        chk("读不出来的片段照样原样收录并标注", all(
            man["files"][f"raw/{n}"].get("readable") is False for n in sizes), man.get("unreadable_clips_note"))

        print("D6 时钟偏移")
        readme = z.read("README.txt").decode()
        chk("README 写的是 车机时钟 + 偏移 = 真实时间", "车机时钟 + 偏移 = 真实时间" in readme
            and "vehicle clock + offset = true time" in readme and "+90.0" in readme)
        chk("不再声称两份数据'已按此对齐'却不做换算", "已按此对齐" not in readme)
        rows = z.read("telemetry.csv").decode().splitlines()
        head = rows[0].split(",")
        first = dict(zip(head, rows[1].split(","), strict=False))
        want = datetime.fromtimestamp(local_naive_to_utc(datetime(2026, 10, 30, 21, 13)).timestamp() + 90,
                                      timezone.utc)
        chk("telemetry.csv 的 timestamp_utc = 文件名时间 + t + 偏移",
            datetime.fromisoformat(first["timestamp_utc"]) == want, (first["timestamp_utc"], want.isoformat()))
        s, e, pad = WINDOWS[-1]
        want_s = local_naive_to_utc(datetime(2026, 10, 30, 21, 13)).timestamp() + 90
        want_e = local_naive_to_utc(datetime(2026, 10, 30, 21, 14)).timestamp() + 60 + 90
        chk("drive.csv 查询窗口两端都加了偏移（以前起点减、终点不加）",
            abs(s.timestamp() - want_s) < 1e-6 and abs(e.timestamp() - want_e) < 1e-6,
            (s.isoformat(), e.isoformat()))
        drive = z.read("drive.csv").decode().splitlines()
        chk("drive.csv 带 timestamp_utc 列", drive[0].startswith("timestamp_utc,"), drive[0][:40])

        print("下载中途断开")
        stream, _ = await evidence.build("SentryClips/2026-10-30_21-15-00")
        part = await collect(stream, limit_chunks=3)
        for _ in range(50):
            if not any(t.name == "evidence-zip" for t in threading.enumerate()):
                break
            await asyncio.sleep(0.1)
        chk("断开后打包线程退出", not any(t.name == "evidence-zip" for t in threading.enumerate()),
            f"got {len(part) // MB} MB before closing")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
    print()
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    sys.exit(1 if FAILS else 0)
