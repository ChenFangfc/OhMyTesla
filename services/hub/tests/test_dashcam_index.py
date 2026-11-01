"""SEI 索引入库（审计 D3 / D4 / D7 / D13）。

  - D7：重解一段，不能把同一事件后面那些段的急刹事件一起删掉
  - D3：同一文件的 event_id 或 seg_start 变了（RecentClips 重新分组、改时区）
        之后照样能入库，不撞 clip_sei.path 主键、不留旧行、不留未提交的半截写入
  - D4：TESLACAM_TZ 没设时按每个时刻自己的夏令时规则换算，而不是"今天的偏移"
  - D13：proto3 的 0 km/h / P 挡是真实值，40→0 的急停算得出急刹

sei.extract / probe_fps_exact 打桩：这里测的是入库逻辑，不是 NAL 解析。
跑法：python3 services/hub/tests/test_dashcam_index.py（需要 hub 运行时依赖；缺依赖时跳过）
"""

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
TMP = Path(tempfile.mkdtemp(prefix="omt-index-"))
ROOT = TMP / "teslacam"
os.environ.update(DATA_DIR=str(TMP / "data"), TESLACAM_ROOT=str(ROOT), TESLACAM_TZ="America/Los_Angeles")
(TMP / "data").mkdir()
ROOT.mkdir()
try:
    import aiosqlite  # noqa: F401
    import pydantic_settings  # noqa: F401
except ImportError:
    print("— skipped: needs the hub runtime (aiosqlite, pydantic-settings); run with tests/run.sh --docker")
    sys.exit(0)

from app.alerts.store import store  # noqa: E402
from app.config import settings  # noqa: E402
from app.dashcam import index, localtime, sei  # noqa: E402
from app.dashcam.scanner import scanner  # noqa: E402

FAILS: list[str] = []


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def brake_samples():
    # 0.5 秒一采样，t=10.0 处 90 → 80 km/h（5.56 m/s²，一次急刹）
    return [{"t": i * 0.5, "speed_kmh": 90.0 if i * 0.5 < 10 else 80.0, "ap": "", "lat": 1.0, "lon": 2.0}
            for i in range(120)]


sei.extract = lambda path, step=0.5, strict=False: brake_samples()
sei.probe_fps_exact = lambda path: 36.0


def touch(rel):
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 100)
    return p


async def rows():
    c = await store.db.execute_fetchall("SELECT event_id, path, seg_start FROM clip_sei ORDER BY path")
    e = await store.db.execute_fetchall("SELECT event_id, path FROM sei_event ORDER BY path")
    return [dict(r) for r in c], [dict(r) for r in e]


async def main():
    await store.open()
    try:
        print("D7 重解一段不误删别段的事件")
        for m in ("10-00-00", "10-01-00", "10-02-00"):
            touch(f"RecentClips/2026-08-05_{m}-front.mp4")
        r1 = await index.reindex()
        c, e = await rows()
        chk("首次入库 3 段 3 个急刹", r1["segments_indexed"] == 3 and len(c) == 3 and len(e) == 3, r1)
        p0 = ROOT / "RecentClips/2026-08-05_10-00-00-front.mp4"
        st = p0.stat()
        os.utime(p0, (st.st_atime, st.st_mtime + 5))
        scanner._scanned_at = 0
        r2 = await index.reindex()
        c, e = await rows()
        chk("只重解了第 0 段", r2["segments_indexed"] == 1, r2)
        chk("另外两段的急刹还在（以前 3 → 1）", len(e) == 3, len(e))

        print("D3 RecentClips 重新分组（event_id 变了）")
        touch("RecentClips/2026-08-05_09-58-50-front.mp4")   # 晚到的更早片段
        scanner._scanned_at = 0
        r3 = await index.reindex()
        c, e = await rows()
        ids = {x["event_id"] for x in c}
        chk("不报 UNIQUE 失败、4 段全部入库", "failed" not in r3 and r3["segments_indexed"] == 4, r3)
        chk("旧 event_id 的行都没了", ids == {"RecentClips/2026-08-05_09-58-50"}, ids)
        chk("急刹事件跟着换了事件、没有重复", len(e) == 4 and {x["event_id"] for x in e} == ids, len(e))
        chk("没有遗留未提交的事务", not store.db.in_transaction)

        print("D3 改了 TESLACAM_TZ（seg_start 变了）")
        before = {x["path"]: x["seg_start"] for x in c}
        settings.teslacam_tz = "UTC"
        r4 = await index.reindex()
        c, e = await rows()
        after = {x["path"]: x["seg_start"] for x in c}
        chk("非强制重建就能重新入库", "failed" not in r4 and r4["segments_indexed"] == 4, r4)
        chk("seg_start 整体平移了 7 小时（PDT → UTC）",
            all(abs(before[p] - after[p] - 7 * 3600) < 1e-6 for p in before), (len(before), len(after)))
        chk("行数不变、事件不重复", len(c) == 4 and len(e) == 4)
        r5 = await index.reindex(force=True)
        chk("force=True 也不失败", "failed" not in r5 and r5["segments_indexed"] == 4, r5)
        settings.teslacam_tz = "America/Los_Angeles"
    finally:
        await store.close()


def test_dst_without_teslacam_tz():
    print("D4 没设 TESLACAM_TZ 时按日期取夏令时规则")
    settings.teslacam_tz = ""
    old_tz, old_env = settings.tz, os.environ.get("TZ")
    try:
        for zone, jan, jul in (("America/Los_Angeles", "16:00", "15:00"), ("Europe/Berlin", "07:00", "06:00")):
            settings.tz = ""
            os.environ["TZ"] = zone
            time.tzset()
            a = localtime.local_naive_to_utc(datetime(2026, 1, 15, 8, 0)).strftime("%H:%M")
            b = localtime.local_naive_to_utc(datetime(2026, 7, 15, 8, 0)).strftime("%H:%M")
            chk(f"{zone}: 1 月 08:00 → {jan}Z，7 月 08:00 → {jul}Z", (a, b) == (jan, jul), (a, b))
        # 解析不出时区名字时的兜底也要跟着夏令时走
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        sl = localtime._SystemLocal()
        a = datetime(2026, 1, 15, 8, 0).replace(tzinfo=sl).astimezone(timezone.utc).strftime("%H:%M")
        b = datetime(2026, 7, 15, 8, 0).replace(tzinfo=sl).astimezone(timezone.utc).strftime("%H:%M")
        chk("系统 localtime 兜底同样按日期换算", (a, b) == ("16:00", "15:00"), (a, b))
    finally:
        settings.tz = old_tz
        if old_env is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_env
        time.tzset()
        settings.teslacam_tz = "America/Los_Angeles"


def test_zero_speed_is_real():
    print("D13 0 km/h 是真实值")
    # 解码器现在把缺失的速度当 0（proto3 默认值）。0.5 秒内 40 → 0：11.1 m/s² 的急停
    samples = [{"t": 0.0, "speed_kmh": 40.0, "ap": ""}, {"t": 0.5, "speed_kmh": 0.0, "ap": ""},
               {"t": 1.0, "speed_kmh": 0.0, "ap": ""}, {"t": 1.5, "speed_kmh": 0.0, "ap": ""}]
    agg, ev = index._analyse(samples, 0.0)
    brakes = [x for x in ev if x["kind"] == "hard_brake"]
    chk("急停算出急刹", len(brakes) == 1 and abs(brakes[0]["magnitude"] - 22.22) < 0.01, brakes)
    chk("均速把停着的时间算进去", agg["speed_avg"] == 10.0, agg["speed_avg"])


if __name__ == "__main__":
    asyncio.run(main())
    test_dst_without_teslacam_tz()
    test_zero_speed_is_real()
    print()
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    sys.exit(1 if FAILS else 0)
