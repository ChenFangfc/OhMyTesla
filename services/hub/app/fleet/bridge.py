"""托管遥测中转桥接 —— 不需要公网 IP 也能拿到实时哨兵状态。

SentryPro / SentryAlert 这类 App 之所以对用户零门槛，是因为**厂商自己托管了
fleet-telemetry 服务器**：车主只需授权 + 配对虚拟钥匙，车辆就直接把遥测推给厂商。

同样地，Teslemetry / Tessie / MyTeslaMate 这类中转服务替你托管那台服务器，
再把数据流转发出来。本模块从家里**主动向外**建立一条长连接去消费它——
家庭网络完全不需要开放任何入站端口、域名或证书。

数据流：
    车辆 → 中转服务（他们的 fleet-telemetry） → 本桥接（出站 SSE/WS）
         → live.ingest_fleet_sentry() → 告警引擎 → 会话模型 → 推送

只有 Teslemetry 的协议经过了机器可读文档（OpenAPI）核实；其余按"实验性"预设提供，
并且解析器对三种载荷写法都兼容，避免猜错格式。
"""

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from datetime import datetime, timezone

import httpx

from ..config import settings
from ..livestate import live

log = logging.getLogger(__name__)

# 各家中转的连接预设。transport: sse / ws
PRESETS: dict[str, dict] = {
    "teslemetry": {
        "transport": "sse",
        "url": "https://api.teslemetry.com/sse/{vin}",
        "auth_mode": "header",
        "auth_name": "Authorization",
        "auth_prefix": "Bearer",
        "verified": True,
    },
    # 以下为实验性：协议来自公开代码/文档推断，未用真实 token 验证过
    "tessie": {
        "transport": "ws",
        "url": "wss://streaming.tessie.com/{vin}",
        "auth_mode": "header",
        "auth_name": "Authorization",
        "auth_prefix": "Bearer",
        "verified": False,
    },
    "myteslamate": {
        "transport": "ws",
        "url": "wss://streaming.myteslamate.com/streaming/",
        "auth_mode": "none",
        "subscribe": '{"msg_type":"data:subscribe_all","tag":"{vin}","token":"{token}"}',
        "ready_prefix": "control:hello:",
        "verified": False,
    },
}

_STATES = ("Off", "Idle", "Armed", "Aware", "Panic", "Quiet")


def _extract_sentry(frame: dict) -> str | None:
    """从一帧遥测里取出哨兵状态，兼容三种已知写法。

    A) {"data": {"SentryMode": "SentryModeStateAware"}}           扁平对象
    B) {"data": [{"key": "SentryMode", "value": {...}}]}          Tesla 原生数组
       value 可能是 {"sentryModeStateValue": X} 或 {"stringValue": X}
    C) 值本身就是裸字符串
    """
    data = frame.get("data")
    raw = None
    if isinstance(data, dict):
        raw = data.get("SentryMode")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("key") == "SentryMode":
                raw = item.get("value")
                break
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("sentryModeStateValue") or raw.get("stringValue") or raw.get("value")
    if raw is None:
        return None
    text = str(raw)
    for s in _STATES:
        if text.endswith(s) or text == s:
            return s
    return None


_FRAC_RE = re.compile(r"\.(\d+)")


def _created_at(frame: dict) -> float:
    """帧的产生时间。Tesla 用 RFC3339 纳秒精度，Python 只认 3/6 位小数，需截断。"""
    for key in ("createdAt", "created_at", "timestamp"):
        v = frame.get(key)
        if not v:
            continue
        if isinstance(v, (int, float)):
            return float(v) / (1000 if v > 1e12 else 1)
        text = str(v).strip().replace("Z", "+00:00")
        # 只截断小数部分，绝不能把时区偏移里的数字也当小数位
        text = _FRAC_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return 0.0


class VinBridge:
    """一辆车一条连接。"""

    def __init__(self, vin: str, cfg: dict) -> None:
        self.vin = vin
        self.cfg = cfg
        self.connected = False
        self.connected_since = 0.0
        self.last_event_at = 0.0
        self.last_state = ""
        self.events = 0
        self.dropped_cache = 0
        self.dropped_stale = 0
        self.reconnects = 0
        self.last_error = ""
        self._floor = 0.0   # 早于此时间的帧视为重放，丢弃
        self._task: asyncio.Task | None = None

    def status(self) -> dict:
        return {
            "vin": f"…{self.vin[-6:]}",
            "connected": self.connected,
            "connected_since": self.connected_since or None,
            "last_event_at": self.last_event_at or None,
            "last_state": self.last_state,
            "events": self.events,
            "dropped_cache": self.dropped_cache,
            "dropped_stale": self.dropped_stale,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"fleet-bridge-{self.vin[-6:]}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ---------- 帧处理 ----------

    async def _on_frame(self, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return

        # 连接时的快照重放：厂商会补发最后已知值，不能当成新事件
        if frame.get("isCache") is True or frame.get("is_cache") is True:
            self.dropped_cache += 1
            return

        state = _extract_sentry(frame)
        if state is None:
            return

        ts = _created_at(frame)
        if ts and ts <= self._floor:
            # 车辆断线重连会重放最多数千条缓冲消息，靠时间戳精确过滤，
            # 不必像 MQTT 那条路那样整段静默 60 秒
            self.dropped_stale += 1
            return
        if ts:
            self._floor = ts

        self.events += 1
        self.last_event_at = time.time()
        self.last_state = state
        log.info("fleet bridge: VIN…%s SentryMode=%s", self.vin[-6:], state)
        await live.ingest_fleet_sentry(self.vin, state)

    # ---------- 连接循环 ----------

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                healthy_at = time.time()
                # 重连后只接受"比上次已知事件更新"或"最近 N 秒内"的帧
                self._floor = max(self._floor, time.time() - settings.fleet_bridge_stale_seconds)
                if self.cfg["transport"] == "sse":
                    await self._run_sse()
                else:
                    await self._run_ws()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:200]
                self.last_error = msg
                fatal_auth = "401" in msg or "403" in msg
                # 长连接断开的常态就是抛异常（读超时、连接被重置）。退避重置原本
                # 写在 try 里、调用之后，这条路径根本走不到 —— 于是 attempt 只增
                # 不减，一台每天断一次的桥接会一路退到 10 分钟才肯重连。
                if not fatal_auth and time.time() - healthy_at > 60:
                    attempt = 0
                if fatal_auth:
                    log.error("fleet bridge auth failed for VIN…%s: %s", self.vin[-6:], msg)
                    attempt = 99  # 直接退到最大间隔，不要反复拿坏 token 去撞
                else:
                    log.warning("fleet bridge VIN…%s disconnected: %s", self.vin[-6:], msg)
            finally:
                if self.connected:
                    self.connected = False
                    self.reconnects += 1

            delay = min(2 ** min(attempt, 10), settings.fleet_bridge_backoff_max)
            delay *= 0.8 + 0.4 * random.random()  # noqa: S311 - 抖动，非密码学用途
            attempt += 1
            await asyncio.sleep(max(delay, 1.0))

    def _url(self) -> str:
        return self.cfg["url"].replace("{vin}", self.vin)

    def _headers(self) -> dict:
        h = {"Accept": "text/event-stream"}
        if self.cfg.get("auth_mode") == "header" and settings.fleet_bridge_token:
            prefix = self.cfg.get("auth_prefix") or ""
            h[self.cfg.get("auth_name") or "Authorization"] = (
                f"{prefix} {settings.fleet_bridge_token}".strip()
            )
        return h

    async def _run_sse(self) -> None:
        url = self._url()
        params = {}
        if self.cfg.get("auth_mode") == "query" and settings.fleet_bridge_token:
            params[self.cfg.get("auth_name") or "token"] = settings.fleet_bridge_token
        timeout = httpx.Timeout(
            connect=10.0, read=settings.fleet_bridge_idle_timeout, write=10.0, pool=None
        )
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=self._headers(), params=params) as resp:
                resp.raise_for_status()
                self.connected = True
                self.connected_since = time.time()
                self.last_error = ""
                log.info("fleet bridge connected (SSE): VIN…%s", self.vin[-6:])
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        await self._on_frame(line[5:].strip())

    async def _run_ws(self) -> None:
        try:
            import websockets
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "WebSocket 中转需要 websockets 库；请在 requirements.txt 中加入 websockets>=12"
            ) from e

        url = self._url()
        headers = self._headers()
        headers.pop("Accept", None)
        # 传自定义头的参数名在 websockets 14 改了：14+ 叫 additional_headers，
        # 之前叫 extra_headers。写死一个会在另一个大版本上抛 TypeError，
        # 而那个 TypeError 又会被外层 except Exception 吃掉 —— 表现成
        # 永远重连不上、last_error 还看不懂。
        kw = dict(ping_interval=20, close_timeout=5)
        if headers:
            major = int((getattr(websockets, "__version__", "0") or "0").split(".")[0] or 0)
            kw["additional_headers" if major >= 14 else "extra_headers"] = headers
        async with websockets.connect(url, **kw) as ws:
            sub = self.cfg.get("subscribe") or settings.fleet_bridge_subscribe
            if sub:
                await ws.send(
                    sub.replace("{vin}", self.vin).replace("{token}", settings.fleet_bridge_token)
                )
            ready_prefix = self.cfg.get("ready_prefix") or settings.fleet_bridge_ready_prefix
            ready = not ready_prefix
            self.connected = True
            self.connected_since = time.time()
            self.last_error = ""
            log.info("fleet bridge connected (WS): VIN…%s", self.vin[-6:])
            async for raw in ws:
                text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                if not ready:
                    if ready_prefix in text:
                        ready = True
                        log.info("fleet bridge subscription confirmed: VIN…%s", self.vin[-6:])
                    continue
                await self._on_frame(text)


class BridgeManager:
    def __init__(self) -> None:
        self.bridges: dict[str, VinBridge] = {}
        self._starter: asyncio.Task | None = None

    def config(self) -> dict:
        preset = dict(PRESETS.get(settings.fleet_bridge_provider) or {})
        if settings.fleet_bridge_url:
            preset["url"] = settings.fleet_bridge_url
        if settings.fleet_bridge_transport:
            preset["transport"] = settings.fleet_bridge_transport
        if settings.fleet_bridge_auth_mode:
            preset["auth_mode"] = settings.fleet_bridge_auth_mode
        if settings.fleet_bridge_auth_name:
            preset["auth_name"] = settings.fleet_bridge_auth_name
        if settings.fleet_bridge_auth_prefix:
            preset["auth_prefix"] = settings.fleet_bridge_auth_prefix
        preset.setdefault("transport", "sse")
        return preset

    def start(self) -> None:
        if not settings.fleet_bridge_enabled:
            return
        self._starter = asyncio.create_task(self._boot(), name="fleet-bridge-boot")

    async def _boot(self) -> None:
        cfg = self.config()
        if not cfg.get("url"):
            log.error("fleet bridge enabled but no URL/provider configured")
            return
        # 等 TeslaMate 建好表、同步出 VIN
        vins: list[str] = []
        for _ in range(30):
            vins = [v.strip() for v in settings.fleet_bridge_vins.split(",") if v.strip()]
            if not vins:
                vins = await live.vins()
            if vins:
                break
            await asyncio.sleep(10)
        if not vins:
            log.warning("fleet bridge: no VIN found (TeslaMate 还没同步到车辆？)")
            return
        for vin in vins:
            b = VinBridge(vin, cfg)
            self.bridges[vin] = b
            b.start()
        log.info("fleet bridge started for %d vehicle(s) via %s",
                 len(vins), settings.fleet_bridge_provider)

    async def stop(self) -> None:
        if self._starter:
            self._starter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._starter
        for b in self.bridges.values():
            await b.stop()

    def status(self) -> dict:
        cfg = self.config()
        return {
            "enabled": settings.fleet_bridge_enabled,
            "provider": settings.fleet_bridge_provider,
            "transport": cfg.get("transport"),
            "verified_protocol": bool(cfg.get("verified")),
            "token_set": bool(settings.fleet_bridge_token),
            "vehicles": [b.status() for b in self.bridges.values()],
        }

    async def enable_sentry_field(self) -> dict:
        """让中转商开始推送 SentryMode 字段。

        光有 token 还不够：中转商默认只推它自己那套字段（Tessie 的默认 10 个字段里
        就没有 SentryMode）。这一步显式请求把它打开，1 秒间隔——
        遥测按"状态变化"计费而不是按间隔，所以 1 秒几乎不产生额外费用。

        故意做成用户点按钮触发，而不是容器启动时自动改——
        改动别人平台上的付费配置应当是一次明确的操作。
        """
        if not settings.fleet_bridge_token:
            return {"ok": False, "error": "还没有填 FLEET_BRIDGE_TOKEN"}
        provider = settings.fleet_bridge_provider
        vins = list(self.bridges) or await live.vins()
        if not vins:
            return {"ok": False, "error": "还没读到车辆 VIN（TeslaMate 登录了吗？）"}

        headers = {"Authorization": f"Bearer {settings.fleet_bridge_token}"}
        done, errors = 0, []
        async with httpx.AsyncClient(timeout=30) as client:
            for vin in vins:
                try:
                    if provider == "teslemetry":
                        # PATCH 是合并语义，不会动其他字段
                        r = await client.patch(
                            f"https://api.teslemetry.com/api/config/{vin}",
                            headers=headers,
                            json={
                                "fields": {
                                    "SentryMode": {
                                        "interval_seconds": 1,
                                        "resend_interval_seconds": 86400,
                                    }
                                },
                                "prefer_typed": True,
                            },
                        )
                    elif provider == "tessie":
                        # POST 是整体替换，必须先取回现有字段再合并，否则会
                        # 悄悄破坏中转商自己 App 依赖的那些字段
                        cur = await client.get(
                            f"https://api.tessie.com/{vin}/fleet_telemetry_config",
                            headers=headers,
                        )
                        fields = {}
                        if cur.is_success:
                            fields = ((cur.json() or {}).get("config") or {}).get("fields") or {}
                        fields["SentryMode"] = {"interval_seconds": 1}
                        r = await client.post(
                            f"https://api.tessie.com/{vin}/fleet_telemetry_config",
                            headers=headers, json={"fields": fields},
                        )
                    else:
                        return {
                            "ok": False,
                            "error": f"{provider} 需要在其自己的后台开启 SentryMode 字段，本项目无法代劳",
                        }
                    if r.is_success:
                        done += 1
                    else:
                        errors.append(f"{vin[-6:]}: HTTP {r.status_code} {r.text[:120]}")
                except httpx.HTTPError as e:
                    errors.append(f"{vin[-6:]}: {e}")

        log.info("enable_sentry_field: %d ok, %d failed", done, len(errors))
        return {
            "ok": done > 0,
            "configured": done,
            "error": "；".join(errors) if errors else "",
        }


manager = BridgeManager()
