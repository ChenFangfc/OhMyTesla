"""teslausb 接入（任务 B）。

  - POST /api/dashcam/teslausb/notify：识别 teslausb 的开始/完成/失败文案并记下；
    "完成"立刻重扫 + 跑一轮新录像监视（settled=True），"开始"不重扫
  - 设置：地址规范化、拒绝把账号密码写进 URL、GET 永远不回密码
  - POST /sync、GET /status 走 Pi 的 cgi-bin（带 Basic 认证）；密码错、Pi 不在家时
    给出能看懂的错误
  - status 里带上 hub 这边的素材目录视图（布局、ARCHIVE_UPLOADED 标记）

用一个本地 HTTP 服务假扮 Pi。路由挂在一个最小的 FastAPI 应用上，不起整个 hub。
跑法：python3 services/hub/tests/test_dashcam_teslausb.py（需要 hub 运行时依赖；缺依赖时跳过）
"""

import asyncio
import base64
import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
TMP = Path(tempfile.mkdtemp(prefix="omt-tusb-"))
ROOT = TMP / "teslacam"
os.environ.update(DATA_DIR=str(TMP / "data"), TESLACAM_ROOT=str(ROOT), TESLACAM_TZ="America/Chicago",
                  ENCRYPTION_KEY="test-only-key")
(TMP / "data").mkdir()
ROOT.mkdir()
try:
    import aiosqlite  # noqa: F401
    import fastapi  # noqa: F401
    import httpx
    import pydantic_settings  # noqa: F401
except ImportError:
    print("— skipped: needs the hub runtime (fastapi, httpx, aiosqlite); run with tests/run.sh --docker")
    sys.exit(0)

from app.alerts.store import store  # noqa: E402
from app.dashcam import teslausb  # noqa: E402
from app.dashcam.router import router  # noqa: E402
from app.dashcam.watcher import watcher  # noqa: E402
from fastapi import FastAPI  # noqa: E402

FAILS: list[str] = []
SYNCS: list[str] = []


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


class FakePi(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Basic " + base64.b64encode(b"pi:secret").decode():
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/cgi-bin/trigger_sync.sh":
            SYNCS.append(self.path)
            body, ct = b"<html><body><p>Sync triggered</p></body></html>", "text/html"
        elif self.path == "/cgi-bin/status.sh":
            # status.sh 的值全是字符串
            body = json.dumps({"cpu_temp": "48312", "num_snapshots": "7", "snapshot_oldest": "1761800000",
                               "snapshot_newest": "1761900000", "total_space": "250000000000",
                               "free_space": "91000000000", "uptime": "86400.55", "drives_active": "yes",
                               "wifi_ssid": "Home", "wifi_strength": "58/70", "wifi_ip": "192.168.0.77",
                               "ether_ip": "", "ether_speed": "", "throttled": "0x0"}).encode()
            ct = "application/json"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_classify():
    print("teslausb 文案识别")
    c = teslausb.classify("Archiving completed successfully. Archived 14 files in 1m22s")
    chk("完成", (c["kind"], c["ok"], c.get("files"), c.get("took")) == ("finish", True, 14, "1m22s"), c)
    c = teslausb.classify("Error during archiving. Archived 3 files in 40s")
    chk("失败", (c["kind"], c["ok"], c.get("files")) == ("finish", False, 3), c)
    c = teslausb.classify("Archiving 12 file(s) including 2 event folder(s) starting at Sat Oct 31 18:00:00 CDT 2026")
    chk("开始", (c["kind"], c.get("files_total")) == ("start", 12), c)
    c = teslausb.classify("Archiving 2 track mode file(s) and 5 file(s) including 1 event folder(s) starting at …")
    chk("开始（含赛道模式）", c["kind"] == "start", c)


async def main():
    await store.open()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakePi)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pi = f"127.0.0.1:{srv.server_address[1]}"
    app = FastAPI()
    app.include_router(router)
    pokes: list[bool] = []
    real_poke = watcher.poke

    async def poke(settled=False):
        pokes.append(settled)
        return await real_poke(settled=settled)

    watcher.poke = poke
    # 容器里跑时 hub 配着真的推送通道：这里绝不能真的发出去
    from app.dashcam import watcher as wmod

    fired: list = []

    async def no_push(*a, **k):
        fired.append(a)
        return True

    wmod.engine.fire_custom = no_push
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as c:
            print("归档通知")
            (ROOT / "SentryClips").mkdir()
            (ROOT / "ARCHIVE_UPLOADED").write_text("")
            r = await c.post("/api/dashcam/teslausb/notify",
                             data={"msg": "Archiving 4 file(s) including 1 event folder(s) starting at now"})
            chk("开始通知：200、不重扫", r.status_code == 200 and r.json()["rescan"] is False, r.text)
            ev = ROOT / "SentryClips/2026-10-31_18-40-10"
            ev.mkdir()
            (ev / "2026-10-31_18-39-00-front.mp4").write_bytes(b"x" * 2000)
            r = await c.post("/api/dashcam/teslausb/notify",
                             data={"msg": "Archiving completed successfully. Archived 4 files in 12s"})
            chk("完成通知：立刻返回 200", r.status_code == 200 and r.json() == {
                "ok": True, "recorded": "finish", "rescan": True}, r.text)
            await asyncio.sleep(0.5)
            from app.dashcam.scanner import scanner

            chk("后台重扫看见了新事件", "SentryClips/2026-10-31_18-40-10" in scanner._events
                and teslausb.last_ingest.get("new_events") == 1, teslausb.last_ingest)
            chk("并跑了一轮新录像监视，且告诉它归档已完成（settled）", pokes == [True], pokes)
            r = await c.post("/api/dashcam/teslausb/notify", data={"msg": "Error during archiving. Archived 1 files in 5s"})
            await asyncio.sleep(0.3)
            chk("失败通知也重扫，但不当作已落定", pokes == [True, False], pokes)
            st = (await c.get("/api/dashcam/teslausb/status")).json()
            chk("status：最近一次归档是失败的那次", st["last_archive"]["ok"] is False
                and st["last_archive"]["message"].startswith("Error during archiving"), st["last_archive"])
            chk("status：素材目录是根目录布局、看得见 ARCHIVE_UPLOADED",
                st["ingest"]["layout"] == "root" and "ARCHIVE_UPLOADED" in st["ingest"]["archive_markers"],
                st["ingest"]["layout"])
            chk("没配 Pi 时 configured=false、不报错", st["configured"] is False and st["pi"] is None)

            print("Pi 设置")
            r = await c.post("/api/dashcam/teslausb/sync")
            chk("没配地址时 sync 返回 409", r.status_code == 409, r.status_code)
            r = await c.post("/api/dashcam/teslausb/settings", json={"pi_url": "http://u:p@10.0.0.5"})
            chk("拒绝把账号密码写进 URL", r.status_code == 422, r.status_code)
            r = await c.post("/api/dashcam/teslausb/settings", json={"pi_url": "ftp://10.0.0.5"})
            chk("只接受 http(s)", r.status_code == 422, r.status_code)
            r = await c.post("/api/dashcam/teslausb/settings",
                             json={"pi_url": pi, "username": "pi", "password": "wrong"})
            chk("地址规范化成 http://host:port", r.json()["pi_url"] == f"http://{pi}", r.json())
            got = (await c.get("/api/dashcam/teslausb/settings")).json()
            chk("GET 不回密码", "password" not in got)
            raw = await store.get_setting("teslausb")
            chk("Pi 的密码加密存放", str(raw.get("password", "")).startswith("enc:"), raw.get("password"))
            tok = got.get("notify_token") or ""
            chk("设置里给出通知令牌（树莓派不用存门户密码）", len(tok) >= 24, got)
            r = await c.post("/api/dashcam/teslausb/notify-token")
            chk("可以换新令牌", r.status_code == 200 and r.json()["notify_token"] not in ("", tok), r.text)
            await _check_token_gate(r.json()["notify_token"], tok)
            st = (await c.get("/api/dashcam/teslausb/status")).json()
            chk("密码错：status 里说清楚", st["pi"] is None and "password" in (st["pi_error"] or ""), st["pi_error"])
            r = await c.post("/api/dashcam/teslausb/sync")
            chk("密码错：sync 返回 502 和原因", r.status_code == 502 and "password" in r.text, r.text)
            await c.post("/api/dashcam/teslausb/settings", json={"password": "secret"})
            r = await c.post("/api/dashcam/teslausb/sync")
            chk("sync 打到 Pi 的 trigger_sync.sh", r.status_code == 200 and SYNCS == ["/cgi-bin/trigger_sync.sh"],
                r.text)
            st = (await c.get("/api/dashcam/teslausb/status")).json()
            p = st["pi"] or {}
            chk("status 解析出 Pi 的状态（单位换好）",
                p.get("cpu_temp_c") == 48.3 and p["snapshots"]["count"] == 7 and p["disk"]["free_bytes"] == 91000000000
                and p["drives_active"] is True and p["wifi"]["ssid"] == "Home", {k: p.get(k) for k in ("cpu_temp_c",)})
            chk("status 记下最后一次触发同步", (st["last_sync_request"] or {}).get("ok") is True)

            print("Pi 不在家")
            srv.shutdown()
            srv.server_close()
            st = (await c.get("/api/dashcam/teslausb/status")).json()
            chk("连不上时给出原因而不是 500", st["pi"] is None and "connect" in (st["pi_error"] or ""), st["pi_error"])
            r = await c.post("/api/dashcam/teslausb/sync")
            chk("sync 连不上返回 502", r.status_code == 502, r.status_code)
    finally:
        watcher.poke = real_poke
        await store.close()


async def _check_token_gate(current: str, old: str) -> None:
    """main.py lets the Pi in with its token, on /notify only, and nothing else."""
    from starlette.requests import Request

    from app.main import _machine_token_ok

    def req(path, token, method="POST"):
        headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
        return Request({"type": "http", "method": method, "path": path, "headers": headers,
                        "query_string": b""})

    chk("令牌能进 /notify", await _machine_token_ok(req("/api/dashcam/teslausb/notify", current)))
    chk("换掉的旧令牌进不去", not await _machine_token_ok(req("/api/dashcam/teslausb/notify", old)))
    chk("令牌进不了别的地址", not await _machine_token_ok(req("/api/tesla/command", current)))
    chk("令牌进不了 Pi 设置", not await _machine_token_ok(req("/api/dashcam/teslausb/settings", current, "GET")))
    chk("没带令牌不放行", not await _machine_token_ok(req("/api/dashcam/teslausb/notify", "")))


if __name__ == "__main__":
    test_classify()
    asyncio.run(main())
    print()
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    sys.exit(1 if FAILS else 0)
