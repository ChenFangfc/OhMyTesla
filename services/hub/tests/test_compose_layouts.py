"""四种布局的 filtergraph 必须能被 ffmpeg 真正解析。

只检查 Python 语法或字符串拼接是不够的：xstack 的 layout 规格、overlay 的
坐标、scale 的 force_divisible_by，任何一处写错都要等用户点了导出、
等 ffmpeg 跑起来才报错，而那时任务已经进队列了。
"""
import datetime
import os
import subprocess
import sys
import tempfile
for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p); break
from app.dashcam.composer import build_plan
from app.dashcam.scanner import Event, Segment
from pathlib import Path

CAMS = ["front", "back", "left_repeater", "right_repeater"]
base = Path(tempfile.mkdtemp())
# 造 4 个真实可解码的 1 秒小视频，让 ffmpeg 真的跑一遍滤镜
for c in CAMS:
    f = base / f"2026-01-01_00-00-00-{c}.mp4"
    subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i",
                    f"testsrc=size=640x480:rate=30:duration=1","-c:v","libx264",
                    "-pix_fmt","yuv420p",str(f)], check=True)

seg = Segment(start=datetime.datetime(2026,1,1,0,0,0),
              cameras={c: f"2026-01-01_00-00-00-{c}.mp4" for c in CAMS})
ev = Event(id="T", type="saved", timestamp=seg.start, segments=[seg])

fails=[]
for layout in ("grid","front_pip","back_pip","vertical","wide"):
    out = base / f"out_{layout}.mp4"
    plan = build_plan(ev, base, None, out, layout=layout, quality="fast")
    # 把输出时长砍到 0.3 秒，只验证滤镜图能跑通
    cmd = [a for a in plan.cmd]
    cmd.insert(cmd.index("-progress"), "-t"); cmd.insert(cmd.index("-progress"), "0.3")
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    ok = r.returncode == 0 and out.exists() and out.stat().st_size > 1000
    dims = ""
    if ok:
        pr = subprocess.run(["ffprobe","-v","error","-select_streams","v:0",
                             "-show_entries","stream=width,height","-of","csv=p=0",str(out)],
                            capture_output=True, text=True)
        dims = pr.stdout.strip()
    want = {"vertical": "1080,1920", "wide": "2560,480"}.get(layout)
    if ok and want and dims != want:
        ok = False
        dims += f"  ← 期望 {want}"
    print(f"  {'✅' if ok else '❌'} {layout:11} {dims}" +
          ("" if ok else "  " + (r.stderr or b'').decode(errors='replace').strip().splitlines()[-1][:120]))
    if not ok: fails.append(layout)
print()
print("全部布局可用" if not fails else f"失败: {fails}")
sys.exit(1 if fails else 0)
