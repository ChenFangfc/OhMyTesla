"""自动化规则的读写接口。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .engine import RISKY_COMMANDS, SAFE_COMMANDS, TRIGGERS, engine

router = APIRouter(prefix="/api/automations", tags=["automations"])


class Rule(BaseModel):
    id: str
    name: str = ""
    enabled: bool = False          # 新规则默认停用：这个引擎能控车
    trigger: dict
    conditions: dict | None = None
    actions: list[dict]
    cooldown_s: float | None = None


@router.get("")
async def list_rules():
    return {
        "rules": await engine.rules(),
        # 界面据此渲染下拉，不要在前端再抄一份常量 —— 抄的那份迟早对不上
        "triggers": TRIGGERS,
        "commands": sorted(SAFE_COMMANDS),
        "risky_commands": sorted(RISKY_COMMANDS),
    }


@router.put("")
async def replace_rules(rules: list[Rule]):
    try:
        return {"rules": await engine.save_rules([r.model_dump() for r in rules])}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("")
async def upsert_rule(rule: Rule):
    try:
        return {"rules": await engine.upsert(rule.model_dump())}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{rule_id}")
async def delete_rule(rule_id: str):
    return {"rules": await engine.delete(rule_id)}


@router.get("/history")
async def history():
    """最近的触发记录。规则不生效时第一件事就是看这里有没有记录。"""
    return {"history": await engine.history()}
