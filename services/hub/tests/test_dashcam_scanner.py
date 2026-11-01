"""素材扫描的健壮性（审计 D11 / L8 / D15）。

  - teslausb 的 RecentClips/<YYYY-MM-DD>/ 按天目录能被扫到，且和事件目录里
    同名的片段去重（teslausb 会把 Saved/Sentry 片段再链一份进 RecentClips）
  - 名字合法但日期不存在的目录/文件、读不了的目录，不会让整次扫描抛异常
  - 空的 TeslaCam/ 不会挡住根目录下的 teslausb 布局
  - .teslausbtmp/、ARCHIVE_UPLOADED、*~1.mp4、FSCK*.REC、event.mp4、._ 文件一律忽略
  - event.json 里带时区的时间戳换算成车机本地时间，排序不再 TypeError
  - 异步扫描：并发调用合并成一次；事件循环里的同步 scan() 不阻塞（先给旧结果）

跑法：python3 services/hub/tests/test_dashcam_scanner.py（需要 hub 运行时依赖；缺依赖时跳过）
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
TMP = Path(tempfile.mkdtemp(prefix="omt-scan-"))
ROOT = TMP / "teslacam"
os.environ.update(DATA_DIR=str(TMP / "data"), TESLACAM_ROOT=str(ROOT), TESLACAM_TZ="America/Chicago")
(TMP / "data").mkdir()
ROOT.mkdir()
try:
    import pydantic_settings  # noqa: F401
except ImportError:
    print("— skipped: needs the hub runtime (pydantic-settings); run with tests/run.sh --docker")
    sys.exit(0)

from app.dashcam import scanner as sc  # noqa: E402

FAILS: list[str] = []


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def put(rel: str, data: bytes = b"x" * 2048) -> Path:
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def fresh() -> list:
    sc.scanner._events = {}
    sc.scanner._scanned_at = 0
    return sc.scanner.scan(force=True)


def test_teslausb_layout():
    print("teslausb 归档布局")
    ev = "SentryClips/2026-10-30_21-15-00"
    for m in ("21-13-00", "21-14-00"):
        for cam in ("front", "back"):
            put(f"{ev}/2026-10-30_{m}-{cam}.mp4")
    put(f"{ev}/event.json", json.dumps({"timestamp": "2026-10-30T21:15:00", "city": "Springfield",
                                         "reason": "sentry_aware_object_detection"}).encode())
    put(f"{ev}/thumb.png", b"\x89PNG....")
    put(f"{ev}/event.mp4")                                   # 新固件的事件视频，不是机位
    put(f"{ev}/2026-10-30_21-14-00-front~1.mp4")             # fsck 恢复出来的副本
    put(f"{ev}/._2026-10-30_21-14-00-front.mp4")             # macOS AppleDouble
    for m in ("17-00-00", "17-01-00", "17-02-00"):
        put(f"RecentClips/2026-10-28/2026-10-28_{m}-front.mp4")
    # teslausb 把事件片段也链进按天目录：同名的要和事件目录去重
    put("RecentClips/2026-10-30/2026-10-30_21-13-00-front.mp4")
    put("RecentClips/2026-10-30/2026-10-30_21-12-00-front.mp4")
    put("ARCHIVE_UPLOADED", b"")
    put("SentryClips/ARCHIVE_UPLOADED", b"")
    put(".teslausbtmp/.2026-10-31_10-00-00-front.mp4.Ab12Cd")
    put("FSCK0000.REC")
    put("SentryClips/.teslausbtmp/junk.mp4")

    events = {e.id: e for e in fresh()}
    chk("根目录布局被识别（没有 TeslaCam/ 这一层）", sc.scanner.base_path() == ROOT)
    s = events.get(ev)
    chk("哨兵事件在", s is not None)
    chk("哨兵事件 2 段、每段 2 路", s and [sorted(x.cameras) for x in s.segments] == [["back", "front"]] * 2,
        s and [sorted(x.cameras) for x in s.segments])
    chk("event.json 读到城市/原因", s and s.city == "Springfield" and s.reason == "sentry_aware_object_detection")
    chk("thumb.png 记下", s and s.thumb == f"{ev}/thumb.png")
    r = events.get("RecentClips/2026-10-28_17-00-00")
    chk("按天目录 RecentClips/<日期>/ 被扫到（3 段连成一个）", r is not None and len(r.segments) == 3)
    dup = events.get("RecentClips/2026-10-30_21-12-00")
    chk("事件目录里已有的片段从 RecentClips 去重",
        dup is not None and [x.start.strftime("%H:%M") for x in dup.segments] == ["21:12"],
        dup and [x.start.strftime("%H:%M") for x in dup.segments])
    chk("杂项（ARCHIVE_UPLOADED、.teslausbtmp、REC、~1、event.mp4）没有变成事件",
        set(events) == {ev, "RecentClips/2026-10-28_17-00-00", "RecentClips/2026-10-30_21-12-00"},
        sorted(events))


def test_bad_names_do_not_abort():
    print("坏名字 / 读不了的目录")
    put("SentryClips/2026-08-05_24-00-00/2026-08-05_23-59-00-front.mp4")   # 目录名时间不存在
    put("RecentClips/2026-02-30_10-00-00-front.mp4")                       # 文件名日期不存在
    put("SavedClips/2026-13-01_00-00-00/2026-13-01_00-00-00-front.mp4")    # 月份 13
    locked = put("SavedClips/2026-10-01_08-00-00/2026-10-01_07-59-00-front.mp4").parent
    os.chmod(locked, 0)
    try:
        events = {e.id: e for e in fresh()}
        ok = True
    except Exception as e:  # noqa: BLE001
        ok, events = False, {}
        print("    raised:", repr(e))
    finally:
        os.chmod(locked, 0o755)
    chk("扫描不抛异常", ok)
    chk("坏目录名的事件按首段时间收录", "SentryClips/2026-08-05_24-00-00" in events)
    stats = sc.scanner.stats
    reasons = " | ".join(i["reason"] for i in stats.get("skipped_items", []))
    chk("跳过的条目带原因", stats.get("skipped", 0) >= 2 and "invalid timestamp" in reasons, reasons)
    if os.geteuid() != 0:   # root 不受权限位限制，容器里这一项没有意义
        chk("读不了的事件目录被跳过而不是让扫描失败", "SavedClips/2026-10-01_08-00-00" not in events)


def test_empty_teslacam_does_not_hide_root():
    print("空 TeslaCam/ 不挡根目录")
    (ROOT / "TeslaCam").mkdir()
    fresh()
    chk("TeslaCam/ 为空时仍用根目录", sc.scanner.base_path() == ROOT)
    put("TeslaCam/SavedClips/2026-10-02_09-00-00/2026-10-02_08-59-00-front.mp4")
    fresh()
    chk("TeslaCam/ 里有素材时用 TeslaCam/", sc.scanner.base_path() == ROOT / "TeslaCam")
    chk("被挡住的根目录布局会报出来", sc.scanner.stats.get("shadowed_root") == str(ROOT),
        sc.scanner.stats.get("shadowed_root"))
    import shutil

    shutil.rmtree(ROOT / "TeslaCam")


def test_aware_event_timestamp():
    print("event.json 带时区的时间戳")
    put("SavedClips/2026-08-05_10-35-22/2026-08-05_10-34-00-front.mp4")
    put("SavedClips/2026-08-05_10-35-22/event.json", b'{"timestamp": "2026-08-05T15:35:22Z"}')
    put("SavedClips/2026-08-06_09-00-00/2026-08-06_08-59-00-front.mp4")
    put("SavedClips/2026-08-06_09-00-00/event.json", b'{"timestamp": "2026-08-06T09:00:00+02:00"}')
    events = {e.id: e for e in fresh()}
    e1 = events["SavedClips/2026-08-05_10-35-22"]
    chk("Z 时间换算成车机本地（芝加哥夏令时 UTC-5）", e1.timestamp == datetime(2026, 8, 5, 10, 35, 22),
        e1.timestamp)
    chk("所有事件时间戳都是 naive", all(e.timestamp.tzinfo is None for e in events.values()))
    try:
        sorted(events.values(), key=lambda e: e.timestamp)
        ok = True
    except TypeError:
        ok = False
    chk("混合来源的时间戳可以排序（/events 不再 500）", ok)


def test_async_scan():
    print("异步扫描")
    calls = []
    real = sc.scanner._scan_blocking

    def counted():
        calls.append(1)
        time.sleep(0.2)
        return real()

    sc.scanner._scan_blocking = counted

    async def main():
        sc.scanner._scanned_at = 0
        a, b = await asyncio.gather(sc.scanner.ascan(), sc.scanner.ascan())
        n_concurrent = len(calls)
        # force=True 必须拿到"请求之后才开始"的扫描：teslausb 通知之后刚落盘的文件要看得见
        before = len(calls)
        sc.scanner._scanned_at = 0
        first = asyncio.ensure_future(sc.scanner.ascan())
        await asyncio.sleep(0.05)
        await asyncio.gather(first, sc.scanner.ascan(force=True))
        forced_extra = len(calls) - before
        # 缓存过期后，事件循环里的同步 scan() 应立即给出旧结果，并在后台刷新
        sc.scanner._scanned_at = 0
        t0 = time.monotonic()
        stale = sc.scanner.scan()
        blocked = time.monotonic() - t0
        await asyncio.sleep(0.5)
        return a, b, n_concurrent, stale, blocked, forced_extra

    a, b, n, stale, blocked, forced_extra = asyncio.run(main())
    sc.scanner._scan_blocking = real
    chk("两个并发的 ascan 合并成一次扫描且结果一致", n == 1 and len(a) == len(b), f"scans={n}")
    chk("进行中的扫描早于 force 请求时，force 会再扫一次", forced_extra == 2, f"scans={forced_extra}")
    chk("事件循环里的 scan() 不阻塞（先给旧结果）", blocked < 0.1 and len(stale) > 0, f"{blocked:.3f}s")
    chk("后台刷新确实跑了", len(calls) >= n + forced_extra + 1, f"scans={len(calls)}")


def test_import_base():
    print("导入目标目录")
    empty = TMP / "empty-root"
    empty.mkdir()
    from app.config import settings
    from app.dashcam import router

    old = settings.teslacam_root
    settings.teslacam_root = str(empty)
    try:
        base = router._import_base()
    finally:
        settings.teslacam_root = old
    chk("空根目录导入时不再建空的 TeslaCam/", base == empty and not (empty / "TeslaCam").exists(), base)


if __name__ == "__main__":
    test_teslausb_layout()
    test_bad_names_do_not_abort()
    test_empty_teslacam_does_not_hide_root()
    test_aware_event_timestamp()
    test_async_scan()
    test_import_base()
    print()
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    sys.exit(1 if FAILS else 0)
