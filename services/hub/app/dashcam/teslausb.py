"""teslausb 接入：归档完成通知 + 树莓派联动（触发同步、读状态）。

teslausb（marcone/teslausb）把车里的 U 盘换成一块树莓派，回到家连上 Wi-Fi 后
把新录像 rsync 到服务器的 Samba 共享里 —— 也就是 hub 的 TESLACAM_ROOT。
hub 本来每 60 秒扫一次目录，能看见新文件；这里补上三件定时扫描做不到的事：

1. **归档完成通知**  POST /api/dashcam/teslausb/notify（表单字段 msg）
   teslausb 的 NOTIFICATION_COMMAND_START / _FINISH 每轮归档各执行一次，配成
       curl -fsS -m 10 -H "Authorization: Bearer <通知令牌>" --data-urlencode "msg=$message" \\
            http://<hub>:8800/api/dashcam/teslausb/notify
   收到"完成"立刻重扫 + 跑一轮新录像监视（不等 60 秒，也不用等 15 秒确认
   目录落定——归档进程自己说传完了），并对新事件做 SEI 索引。"Error during
   archiving" 开头的照样重扫（可能传了一部分），并记成失败。这个地址用单独的
   通知令牌（设置页里显示、可重新生成），**不要**把门户密码放到树莓派上 ——
   门户密码能开车门，令牌只能让 hub 重扫一次目录。
2. **触发同步**  POST /api/dashcam/teslausb/sync → Pi 的 /cgi-bin/trigger_sync.sh。
   teslausb 只在"服务器从不可达变为可达"（车回到家）时归档；车停在车库里录到的
   哨兵事件，不手动触发就要等到下一次出门再回来。
3. **状态**  GET /api/dashcam/teslausb/status → Pi 的 /cgi-bin/status.sh（JSON）
   + 最近几次归档结果 + hub 这边看到的素材目录状态。

Pi 的地址（以及可选的 Web 界面用户名/密码，对应 teslausb 的 WEB_USERNAME /
WEB_PASSWORD）存在 hub 设置 `teslausb` 里。密码不会经 GET 返回。
"""

import asyncio
import base64
import contextlib
import json
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException
from pydantic import BaseModel

from ..alerts.store import store
from ..config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/teslausb", tags=["dashcam"])

SETTINGS_KEY = "teslausb"
TOKEN_KEY = "teslausb_notify_token"  # noqa: S105 — the settings key, not a secret
RUNS_KEY = "teslausb_runs"
RUNS_MAX = 20
PI_TIMEOUT_S = 8

_ingest_task: asyncio.Task | None = None
_ingest_again = False
_ingest_settled = False
last_ingest: dict = {}


# ---------------- 归档通知 ----------------

_FILES_RE = re.compile(r"Archived\s+(\d+)\s+(?:trackmode\s+)?files?", re.IGNORECASE)
_TOTAL_RE = re.compile(r"Archiving\s+(\d+)\s+(?:track mode\s+)?file", re.IGNORECASE)
_IN_RE = re.compile(r"\bin\s+((?:\d+h)?\s*(?:\d+m)?\s*(?:\d+s)?)\s*$", re.IGNORECASE)


def classify(msg: str) -> dict:
    """把 teslausb 的推送文案解析成结构化结果。

    文案来自 teslausb 的 archiveloop：
      开始: "Archiving 12 file(s) including 2 event folder(s) starting at …"
      完成: "Archiving completed successfully. Archived 12 files in 1m22s"
      失败: "Error during archiving. Archived 3 files in 40s"
    """
    m = (msg or "").strip()
    low = m.lower()
    out: dict = {"message": m[:500]}
    if low.startswith("error during archiving"):
        out.update(kind="finish", ok=False)
    elif low.startswith("archiving completed"):
        out.update(kind="finish", ok=True)
    elif _TOTAL_RE.match(m):
        out.update(kind="start", ok=None)
        out["files_total"] = int(_TOTAL_RE.match(m).group(1))
    else:
        # 自定义的 NOTIFICATION_COMMAND，或者空消息：当作"有东西到了"
        out.update(kind="other", ok=None)
    if (f := _FILES_RE.search(m)) and out["kind"] == "finish":
        out["files"] = int(f.group(1))
    if (d := _IN_RE.search(m)) and out["kind"] == "finish":
        out["took"] = d.group(1).strip()
    return out


async def _record_run(rec: dict) -> None:
    def push(cur):
        cur = list(cur or [])
        cur.insert(0, rec)
        return cur[:RUNS_MAX]

    await store.mutate_setting(RUNS_KEY, push, [])


async def _ingest(settled: bool) -> None:
    """重扫 → 新录像监视 → 新事件的 SEI 索引。并发的通知合并成一轮。"""
    global _ingest_again, _ingest_settled, last_ingest
    from . import index
    from .scanner import scanner
    from .watcher import watcher

    while True:
        _ingest_again = False
        started = time.time()
        before = {e.id for e in scanner._events.values()}
        try:
            events = await scanner.ascan(force=True)
            poke = await watcher.poke(settled=settled)
            new_ids = [e.id for e in events if e.id not in before]
            idx = await index.reindex(event_ids=new_ids) if new_ids else None
            last_ingest = {"at": started, "seconds": round(time.time() - started, 2),
                           "events": len(events), "new_events": len(new_ids),
                           "watcher": poke, "indexed": idx and idx.get("segments_indexed")}
        except Exception as e:
            log.exception("teslausb ingest failed")
            last_ingest = {"at": started, "error": str(e)[:300]}
        if not _ingest_again:
            return
        settled = _ingest_settled
        _ingest_settled = False


def _schedule_ingest(settled: bool) -> None:
    global _ingest_task, _ingest_again, _ingest_settled
    if _ingest_task is not None and not _ingest_task.done():
        _ingest_again = True
        _ingest_settled = _ingest_settled or settled
        return
    _ingest_task = asyncio.create_task(_ingest(settled), name="teslausb-ingest")


@router.post("/notify")
async def notify(msg: str = Form("")):
    """teslausb 的 NOTIFICATION_COMMAND_* 回调。立刻返回，重扫在后台做。

    teslausb 那边 curl 带 -m 10，这里不能等扫描/索引跑完再回。
    """
    rec = {"at": time.time(), **classify(msg)}
    await _record_run(rec)
    if rec["kind"] != "start":
        # 成功 = 这一轮传完了，事件目录可以直接视为落定；失败/未知则照常等 15 秒确认
        _schedule_ingest(settled=rec["kind"] == "finish" and rec["ok"] is True)
    log.info("teslausb notify: %s", rec)
    return {"ok": True, "recorded": rec["kind"], "rescan": rec["kind"] != "start"}


async def notify_token(create: bool = True) -> str:
    """The token the Pi sends to /notify. Created on first use."""
    tok = await store.get_setting(TOKEN_KEY, "") or ""
    if not tok and create:
        tok = secrets.token_urlsafe(24)
        await store.set_setting(TOKEN_KEY, tok)
    return tok


@router.post("/notify-token")
async def rotate_notify_token():
    """Issue a new notify token; the old one stops working at once."""
    tok = secrets.token_urlsafe(24)
    await store.set_setting(TOKEN_KEY, tok)
    return {"notify_token": tok}


# ---------------- 树莓派联动设置 ----------------

class PiSettings(BaseModel):
    pi_url: str | None = None      # "192.168.1.60"、"http://teslausb.local"、"http://10.0.0.5:8080"
    username: str | None = None    # teslausb 的 WEB_USERNAME（没开 Web 认证就留空）
    password: str | None = None    # None = 不改，"" = 清空


def normalize_pi_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    u = urlsplit(raw)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Pi address must be a host name or an http(s) URL")
    if u.username or u.password:
        raise ValueError("put the user name and password in their own fields, not in the URL")
    host = u.hostname
    if ":" in host:                      # IPv6 字面量
        host = f"[{host}]"
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme}://{host}{port}"


def _enc(v: str) -> str:
    from ..tesla import vault

    return "enc:" + base64.b64encode(vault.encrypt(v)).decode() if v else ""


def _dec(v) -> str:
    if isinstance(v, str) and v.startswith("enc:"):
        from ..tesla import vault

        try:
            return vault.decrypt(base64.b64decode(v[4:])) or ""
        except Exception:  # noqa: BLE001 — key changed: treat as unset
            return ""
    return v or ""


async def get_config() -> dict:
    """Pi settings with the web password decrypted (stored encrypted)."""
    cfg = await store.get_setting(SETTINGS_KEY, {}) or {}
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    cfg["password"] = _dec(cfg.get("password"))
    return cfg


def _public(cfg: dict, token: str = "") -> dict:
    return {"pi_url": cfg.get("pi_url") or "", "username": cfg.get("username") or "",
            "has_password": bool(cfg.get("password")), "notify_token": token}


@router.get("/settings")
async def get_settings():
    return _public(await get_config(), await notify_token())


@router.post("/settings")
async def set_settings(body: PiSettings):
    def apply(cur):
        cur = dict(cur or {})
        if body.pi_url is not None:
            cur["pi_url"] = normalize_pi_url(body.pi_url)
        if body.username is not None:
            cur["username"] = body.username.strip()
        if body.password is not None:
            cur["password"] = _enc(body.password)
        return cur

    try:
        await store.mutate_setting(SETTINGS_KEY, apply, {})
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _public(await get_config(), await notify_token())


async def _pi_get(cfg: dict, path: str):
    import httpx

    auth = None
    if cfg.get("username"):
        auth = httpx.BasicAuth(cfg["username"], cfg.get("password") or "")
    async with httpx.AsyncClient(timeout=PI_TIMEOUT_S, follow_redirects=False) as c:
        return await c.get(cfg["pi_url"] + path, auth=auth)


def _pi_error(e: Exception) -> str:
    import httpx

    if isinstance(e, httpx.TimeoutException):
        return "timed out talking to the Pi (is it home and on Wi-Fi?)"
    if isinstance(e, httpx.ConnectError):
        return "could not connect to the Pi (wrong address, or it is away from home)"
    return f"{e.__class__.__name__}: {e}"[:300]


@router.post("/sync")
async def trigger_sync():
    """让 teslausb 立刻跑一轮归档。

    它的实现是 touch /tmp/archive_is_unreachable —— archiveloop 看到"服务器
    掉线又回来"就开始归档；只有在它归档完、在家里空转的时候才生效。
    """
    cfg = await get_config()
    if not cfg.get("pi_url"):
        raise HTTPException(409, "teslausb Pi address is not configured")
    try:
        r = await _pi_get(cfg, "/cgi-bin/trigger_sync.sh")
    except Exception as e:  # 网络错误全部转成 502 给界面
        log.info("teslausb trigger_sync failed: %s", e)
        rec = {"at": time.time(), "ok": False, "error": _pi_error(e)}
        await store.set_setting("teslausb_last_sync", rec)
        raise HTTPException(502, rec["error"]) from e
    ok = r.status_code == 200
    rec = {"at": time.time(), "ok": ok, "status_code": r.status_code}
    if r.status_code == 401:
        rec["error"] = "the Pi asked for a password (set the teslausb web user name/password)"
    await store.set_setting("teslausb_last_sync", rec)
    if not ok:
        raise HTTPException(502, rec.get("error") or f"Pi answered HTTP {r.status_code}")
    return rec


def _parse_status(text: str) -> dict:
    """status.sh 输出的 JSON（值全是字符串）→ 带单位的结构。"""
    try:
        raw = json.loads(text)
    except ValueError:
        i = text.find("{")
        raw = json.loads(text[i:]) if i >= 0 else {}

    def num(k, scale=1.0):
        with contextlib.suppress(TypeError, ValueError):
            return float(raw.get(k)) * scale
        return None

    def intv(k):
        v = num(k)
        return int(v) if v is not None else None

    total, free = num("total_space"), num("free_space")
    return {
        "cpu_temp_c": round(num("cpu_temp", 0.001), 1) if num("cpu_temp") is not None else None,
        "uptime_s": num("uptime"),
        "drives_active": raw.get("drives_active") == "yes",
        "snapshots": {"count": intv("num_snapshots"), "oldest": intv("snapshot_oldest"),
                      "newest": intv("snapshot_newest")},
        "disk": {"total_bytes": int(total) if total else None,
                 "free_bytes": int(free) if free is not None else None},
        "wifi": {"ssid": raw.get("wifi_ssid") or None, "strength": raw.get("wifi_strength") or None,
                 "ip": raw.get("wifi_ip") or None, "freq": raw.get("wifi_freq") or None},
        "ethernet": {"ip": raw.get("ether_ip") or None, "speed": raw.get("ether_speed") or None},
        "throttled": raw.get("throttled") or None,
        "raw": raw,
    }


def _ingest_view() -> dict:
    from .scanner import scanner
    from .watcher import _transfer_in_progress, watcher

    root = Path(settings.teslacam_root)
    base = scanner.base_path()
    markers = {}
    if base is not None:
        for p in (base / "ARCHIVE_UPLOADED", base / "SentryClips" / "ARCHIVE_UPLOADED",
                  base / "SavedClips" / "ARCHIVE_UPLOADED"):
            with contextlib.suppress(OSError):
                markers[str(p.relative_to(base))] = p.stat().st_mtime
    return {
        "root": str(root),
        "base": str(base) if base else None,
        "layout": None if base is None else ("TeslaCam/" if base.name == "TeslaCam" else "root"),
        "archive_markers": markers,
        "transfer_in_progress": bool(base and _transfer_in_progress(base)),
        "scan": {k: v for k, v in scanner.stats.items() if k != "skipped_items"},
        "last_ingest": last_ingest,
        "watcher": watcher.status(),
    }


@router.get("/status")
async def status():
    cfg = await get_config()
    runs = await store.get_setting(RUNS_KEY, []) or []
    last_finish = next((r for r in runs if r.get("kind") == "finish"), None)
    in_progress = bool(runs) and runs[0].get("kind") == "start"
    out = {
        "configured": bool(cfg.get("pi_url")),
        "settings": {k: v for k, v in _public(cfg).items() if k != "notify_token"},
        "pi": None,
        "pi_error": None,
        "archiving": in_progress,
        "last_archive": last_finish,
        "runs": runs[:10],
        "last_sync_request": await store.get_setting("teslausb_last_sync"),
        "ingest": await asyncio.to_thread(_ingest_view),
    }
    if cfg.get("pi_url"):
        try:
            r = await _pi_get(cfg, "/cgi-bin/status.sh")
            if r.status_code == 200:
                out["pi"] = _parse_status(r.text)
            elif r.status_code == 401:
                out["pi_error"] = "the Pi asked for a password (set the teslausb web user name/password)"
            else:
                out["pi_error"] = f"Pi answered HTTP {r.status_code}"
        except ValueError:
            out["pi_error"] = "the Pi's status.sh did not return JSON"
        except Exception as e:  # noqa: BLE001
            out["pi_error"] = _pi_error(e)
    return out
