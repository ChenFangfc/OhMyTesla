import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..config import settings
from . import commands, tokens

router = APIRouter(prefix="/api/tesla", tags=["tesla"])

_last_cmd: dict[tuple[int, str], float] = {}
_COOLDOWN_S = 3  # 防手抖连点


def _prune_cooldown(now: float) -> None:
    if len(_last_cmd) > 500:
        for k in [k for k, t in _last_cmd.items() if now - t > _COOLDOWN_S * 10]:
            _last_cmd.pop(k, None)


@router.get("/status")
async def status():
    src = await tokens.token_source()
    return {
        "token_source": src,
        "ready": src not in ("none", "db_down"),
        "api_host": settings.tesla_command_proxy or settings.tesla_api_host,
        "proxy": bool(settings.tesla_command_proxy),
        "commands": [
            {"id": k, "key": v["key"], "icon": v["icon"],
             "risky": v["risky"], "param": v.get("param")}
            for k, v in commands.COMMANDS.items()
        ],
    }


class TokenBody(BaseModel):
    token: str = ""


@router.post("/token")
async def set_token(body: TokenBody):
    await tokens.set_manual_token(body.token)
    return {"ok": True, "source": await tokens.token_source()}


class CommandBody(BaseModel):
    car_id: int
    command: str
    params: dict | None = None
    confirm: bool = False


@router.post("/command")
async def send_command(body: CommandBody):
    spec = commands.COMMANDS.get(body.command)
    if spec is None:
        raise HTTPException(400, "Unknown command")
    if spec["risky"] and not body.confirm:
        raise HTTPException(428, "This action needs confirmation")

    key = (body.car_id, body.command)
    now = time.time()
    if now - _last_cmd.get(key, 0) < _COOLDOWN_S:
        raise HTTPException(429, "Too fast — wait a moment")
    _prune_cooldown(now)
    _last_cmd[key] = now

    try:
        return await commands.send(body.car_id, body.command, body.params)
    except commands.CommandError as e:
        raise HTTPException(502, str(e)) from e
    except db.DbUnavailable as e:
        raise HTTPException(503, f"TeslaMate 数据库不可用: {e}") from e
