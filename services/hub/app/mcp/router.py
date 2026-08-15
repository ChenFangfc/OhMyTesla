"""MCP 服务端（Streamable HTTP 传输）。

把这辆车的完整历史开放给 AI 助手：在 Claude Code / Claude Desktop / Cursor 里
接上这个地址，就能直接问「上个月充电花了多少」「这两周能耗为什么变差」
「哪次哨兵事件持续最久」，模型自己去查、自己交叉验证。

商业产品里没有一个提供这个 —— 数据在别人服务器上，他们不会开这种口子。
自托管才做得到：数据本来就在你自己机器上。

实现说明：MCP 是 JSON-RPC 2.0。这里只实现服务端必需的那几个方法
（initialize / tools/list / tools/call / ping），够 Claude 和 Cursor 用，
不引入任何新依赖。通知类消息（notifications/*）按规范不回响应。
"""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import tools

log = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

# 与客户端协商的协议版本。收到别的版本也照常工作 —— 按规范回我们支持的那个。
PROTOCOL_VERSION = "2025-06-18"

SERVER_INFO = {"name": "ohmytesla", "version": "1.0.0"}

INSTRUCTIONS = (
    "这是一台特斯拉的自托管数据中枢。可以查行程、充电、电池健康、停放耗电、"
    "胎压、告警与哨兵活动。写 SQL 之前先调 describe_schema —— 里面写了几个"
    "会算错的口径（时间戳是 naive UTC、能耗要用额定续航差乘 efficiency）。"
)


def _ok(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


async def _handle(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 消息。返回 None 表示这是通知，不该有响应。"""
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    # 通知：没有 id，按规范不回任何东西
    if rid is None and isinstance(method, str) and method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })

    if method == "ping":
        return _ok(rid, {})

    if method == "tools/list":
        return _ok(rid, {"tools": tools.list_tools()})

    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        text = await tools.call_tool(name, args)
        is_error = '"error"' in text[:60]
        return _ok(rid, {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        })

    if rid is None:
        return None
    return _err(rid, -32601, f"method not found: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(_err(None, -32700, "parse error"), status_code=400)

    # 规范允许批量：一次发一个数组
    if isinstance(payload, list):
        out = [r for r in [await _handle(m) for m in payload] if r is not None]
        if not out:
            return Response(status_code=202)     # 全是通知
        return JSONResponse(out)

    result = await _handle(payload)
    if result is None:
        return Response(status_code=202)
    return JSONResponse(result)


@router.get("/mcp")
async def mcp_info():
    """浏览器直接打开时给一句人话，而不是 405。"""
    return {
        "server": SERVER_INFO,
        "protocol": PROTOCOL_VERSION,
        "transport": "streamable-http (POST JSON-RPC to this same URL)",
        "tools": [t["name"] for t in tools.list_tools()],
        "hint": "把这个地址加进 Claude Code：claude mcp add --transport http ohmytesla <本地址>",
    }
