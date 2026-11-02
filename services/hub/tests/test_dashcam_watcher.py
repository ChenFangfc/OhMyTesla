"""新哨兵录像提醒（审计 D5 / L7 + 2026-10 的"推送要短"规则）。

  - 送达之后才算"已知"：fire_custom 返回 False 就留着、退避后重试，送达后不再重复
  - 目录落定才发：teslausb 先传 mp4、最后才是 event.json / thumb.png；
    缺这两个时要等 15 秒没变化，且 .teslausbtmp 里没有正在传的文件
  - 提醒带缩略图（image=）和站内链接（url=），标题一行、正文一行
  - 只提醒 SentryClips；7 天前的旧事件（导入旧素材）只进基线不提醒
  - 一轮里很多个就合并成一条；基线没变时不写库
  - 兼容老的 fire_custom 签名（没有 image/url 参数）

fire_custom 打桩：告警引擎在别处重构，这里只测监视器自己的逻辑。
跑法：python3 services/hub/tests/test_dashcam_watcher.py（需要 hub 运行时依赖；缺依赖时跳过）
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
TMP = Path(tempfile.mkdtemp(prefix="omt-watch-"))
ROOT = TMP / "teslacam"
os.environ.update(DATA_DIR=str(TMP / "data"), TESLACAM_ROOT=str(ROOT), TESLACAM_TZ="America/Chicago",
                  PUBLIC_URL="")
(TMP / "data").mkdir()
ROOT.mkdir()
try:
    import aiosqlite  # noqa: F401
    import pydantic_settings  # noqa: F401
except ImportError:
    print("— skipped: needs the hub runtime (aiosqlite, pydantic-settings); run with tests/run.sh --docker")
    sys.exit(0)

from app.alerts.store import store  # noqa: E402
from app.dashcam import watcher as wmod  # noqa: E402

FAILS: list[str] = []
CALLS: list[dict] = []
RESULT = {"ok": True}


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


async def fake_fire(car_id, level, title, body, rule="sentry_activity", gate=None, *,
                    url=None, tag=None, image=None):
    CALLS.append({"title": title, "body": body, "rule": rule, "gate": gate, "url": url, "image": image})
    return RESULT["ok"]


async def old_fire(car_id, level, title, body, rule="sentry_activity", gate=None):
    CALLS.append({"title": title, "body": body, "rule": rule, "old": True})
    return True


def now_name(minutes_ago: float = 5) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d_%H-%M-%S")  # noqa: DTZ005


def add(kind: str, name: str, files: list[str], city: str = "Springfield") -> Path:
    d = ROOT / kind / name
    d.mkdir(parents=True, exist_ok=True)
    clip = name[:-2] + "00"
    for f in files:
        p = d / (f.replace("CLIP", clip))
        if f == "event.json":
            ts = datetime.strptime(name, "%Y-%m-%d_%H-%M-%S").isoformat()  # noqa: DTZ007
            p.write_text(json.dumps({"timestamp": ts, "city": city,
                                     "reason": "sentry_aware_object_detection"}))
        elif f == "thumb.png":
            p.write_bytes(b"\x89PNG fake thumbnail")
        else:
            p.write_bytes(b"x" * 1000)   # 太小，不会去跑 ffmpeg 抽帧
    return d


async def tick(w, **kw):
    from app.dashcam.scanner import scanner

    scanner._scanned_at = 0
    return await w._tick(**kw)


async def main():
    await store.open()
    from app.i18n import prefs

    await prefs.set_prefs({"locale": "en"})
    wmod.engine.fire_custom = fake_fire
    wmod.STABLE_S = 0.4
    wmod.RETRY_BASE_S = 0.3
    try:
        add("SentryClips", now_name(600), ["CLIP-front.mp4", "event.json", "thumb.png"])
        w = wmod.ClipWatcher()
        await w._load_known()
        r = await tick(w)
        chk("首次部署只建基线、不提醒", "baseline" in r and not CALLS, r)

        print("目录落定才发")
        ev = now_name(3)
        add("SentryClips", ev, ["CLIP-back.mp4", "CLIP-front.mp4"])         # mp4 先到
        r = await tick(w)
        chk("只有 mp4 时不发（还在传）", not CALLS and r["pending"] == 1, r)
        add("SentryClips", ev, ["event.json", "thumb.png"])                  # 最后两个文件到了
        r = await tick(w)
        chk("event.json + thumb.png 到齐立刻发一条", len(CALLS) == 1, r)
        c = CALLS[-1] if CALLS else {}
        chk("规则与开关都是 new_sentry_clip", c.get("rule") == "new_sentry_clip" and c.get("gate") == "new_sentry_clip")
        chk("标题一行：🎬 车名 — 事件", (c.get("title") or "").startswith("🎬 ")
            and c.get("title", "").endswith(" — Sentry Clip Saved"), c.get("title"))
        chk("正文只有一行（原因 · 地点 · 时间）", "\n" not in c.get("body", "\n")
            and "Threat detected" in c.get("body", "") and "Springfield" in c.get("body", ""), c.get("body"))
        chk("缩略图走 image=", (c.get("image") or "").endswith(f"SentryClips/{ev}/thumb.png"), c.get("image"))
        chk("站内链接走 url=", c.get("url") == f"/dashcam/play?id=SentryClips/{ev}", c.get("url"))
        await tick(w)
        await tick(w)
        chk("同一个事件只发一次", len(CALLS) == 1)
        from app.i18n.router import t

        chk("中文标题同样一行", t("watcher.sentry_clip", "zh", place="Springfield") == "哨兵录像已保存 · Springfield")

        print("送达失败 → 稍后重试（D5）")
        RESULT["ok"] = False
        ev2 = now_name(2)
        add("SentryClips", ev2, ["CLIP-front.mp4", "event.json", "thumb.png"])
        await tick(w)
        chk("送达失败时不进基线", f"SentryClips/{ev2}" not in w._known and len(CALLS) == 2)
        await tick(w)
        chk("退避期内不重复尝试", len(CALLS) == 2)
        RESULT["ok"] = True
        await asyncio.sleep(0.35)
        await tick(w)
        chk("退避到期后重试并送达", len(CALLS) == 3 and f"SentryClips/{ev2}" in w._known)
        await asyncio.sleep(0.35)
        await tick(w)
        chk("送达后不再重复", len(CALLS) == 3)

        print("没有 thumb.png / event.json 的目录")
        ev3 = now_name(1)
        d3 = add("SentryClips", ev3, ["CLIP-front.mp4"])
        tmpdir = ROOT / ".teslausbtmp"
        tmpdir.mkdir()
        (tmpdir / ".2026-10-31_10-00-00-back.mp4.x1y2").write_bytes(b"partial")
        await tick(w)
        await asyncio.sleep(0.5)
        await tick(w)
        chk("rsync 临时目录里有正在传的文件时不发", len(CALLS) == 3)
        (tmpdir / ".2026-10-31_10-00-00-back.mp4.x1y2").unlink()
        (d3 / "2026-10-31_10-00-00-back.mp4").write_bytes(b"y" * 1000)       # 目录又变了
        await tick(w)
        chk("目录刚变过不发", len(CALLS) == 3)
        await asyncio.sleep(0.5)
        await tick(w)
        chk("15 秒（测试里 0.4 秒）没变化后发出", len(CALLS) == 4 and CALLS[-1]["image"] is None,
            CALLS[-1] if len(CALLS) == 4 else len(CALLS))

        print("teslausb 说归档完成（settled）")
        ev4 = now_name(0.5)
        add("SentryClips", ev4, ["CLIP-front.mp4"])
        await tick(w, settled=True)
        chk("归档完成通知后不必再等 15 秒", len(CALLS) == 5)

        print("不该提醒的")
        add("SavedClips", now_name(0.2), ["CLIP-front.mp4", "event.json", "thumb.png"])
        add("SentryClips", (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d_%H-%M-%S"),  # noqa: DTZ005
            ["CLIP-front.mp4", "event.json", "thumb.png"])
        await tick(w)
        chk("手动保存（SavedClips）和 30 天前的旧事件都不提醒", len(CALLS) == 5)

        print("一次来很多个")
        for k in range(5):
            add("SentryClips", (datetime.now() - timedelta(minutes=40 + k)).strftime("%Y-%m-%d_%H-%M-%S"),  # noqa: DTZ005
                ["CLIP-front.mp4", "event.json", "thumb.png"], city=f"City{k}")
        await tick(w)
        c = CALLS[-1]
        chk("合并成一条", len(CALLS) == 6 and c["title"].endswith(" — 5 Sentry Clips Saved"), c["title"])
        chk("合并那条也是一行正文、最新的在前", "\n" not in c["body"] and "City0" in c["body"], c["body"])

        print("基线没变时不写库（L7）")
        writes = []
        real_set = store.set_setting

        async def counting(key, value):
            if key == "watcher_known":
                writes.append(1)
            await real_set(key, value)

        store.set_setting = counting
        for _ in range(3):
            await tick(w)
        store.set_setting = real_set
        chk("空转三轮没有写 watcher_known", not writes, len(writes))

        print("兼容老签名")
        w._fire_kwargs = None
        wmod.engine.fire_custom = old_fire
        add("SentryClips", now_name(0.1), ["CLIP-front.mp4", "event.json", "thumb.png"])
        await tick(w)
        chk("没有 image/url 参数的 fire_custom 也能调用", CALLS[-1].get("old") is True)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
    print()
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    sys.exit(1 if FAILS else 0)
