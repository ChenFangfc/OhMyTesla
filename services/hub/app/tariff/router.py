import logging

from fastapi import APIRouter, HTTPException

from .. import db
from ..config import settings
from ..insights.guard import http_error
from . import calc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tariff", tags=["tariff"])


def _raise(e: Exception):
    err = http_error(e)
    if err is None:
        raise e
    raise err from e


@router.get("/plan")
async def get_plan():
    plan = await calc.get_plan()
    if not plan.get("currency"):
        # 没显式设过币种就跟地区走
        from ..i18n.prefs import get as _get_prefs
        plan["currency"] = (await _get_prefs()).get("currency") or "USD"
    geofences = []
    try:
        rows = await db.fetch("SELECT name FROM geofences ORDER BY name")
        geofences = [r["name"] for r in rows]
    except Exception:
        log.debug("suppressed", exc_info=True)
    stored = await calc.stored_costs()
    phash = calc.plan_hash(plan)
    return {
        "plan": plan,
        "geofences": geofences,
        "write_back": settings.cost_write_back,
        "warnings": calc.validate(plan),
        "dirty": await calc.is_dirty(),
        "unconfigured": calc.is_unconfigured(plan),
        "saved": await calc.plan_saved(),
        # 自动定价的进度：多少场已算、其中多少是按旧方案算的（需要"重算"才会更新）
        "priced_sessions": len(stored),
        "stale_sessions": sum(1 for v in stored.values() if v.get("p") != phash),
        "method": calc.COST_METHOD,
        "energy_basis": "max(charge_energy_added, charge_energy_used)",
    }


@router.get("/templates")
async def list_templates():
    """常见分时电价的**时段结构**。费率一律留空让用户自己填 ——
    各地各档年年变，内置一个数字出来用户很可能直接用了，然后账就是错的。"""
    from ..i18n.prefs import get as _get_prefs

    loc = (await _get_prefs())["locale"]
    return {
        "templates": [
            {"id": t["id"],
             "name": t["name_zh"] if loc == "zh" else t["name_en"],
             "rule": calc.template(t["id"], loc)}
            for t in calc.TEMPLATES
        ]
    }


@router.post("/plan")
async def save_plan(plan: dict):
    if not isinstance(plan, dict):
        raise HTTPException(400, "plan must be an object")
    # 币种跟地区走，不再硬编码 CNY —— 北美用户打开设置看到 ¥ 很怪
    from ..i18n.prefs import get as _get_prefs
    cur = str(plan.get("currency") or (await _get_prefs()).get("currency") or "USD").upper()
    if len(cur) > 8 or not cur.isalnum():
        raise HTTPException(400, "Currency must be a short alphanumeric code (USD, EUR, CNY…)")
    plan["currency"] = cur
    # 写的时候严格：一条非数字费率存进去之后，读的路径（重算、预览、洞察页
    # 的费用列）会一路 500，而用户没有任何界面能把它改回来。在入口挡住，
    # 并明确指出是哪一条不合格。
    errs = calc.validate_plan(plan)
    if errs:
        raise HTTPException(400, "; ".join(errs[:5]))
    await calc.set_plan(plan)
    saved = calc.normalize_plan(plan)
    return {"ok": True, "plan": saved, "warnings": calc.validate(saved)}


@router.post("/recompute")
async def recompute(car_id: int | None = None):
    try:
        return await calc.recompute_all(car_id, write_back=settings.cost_write_back)
    except Exception as e:  # noqa: BLE001
        _raise(e)


@router.get("/preview")
async def preview(cp_id: int):
    """单次充电的费用明细（分时段拆分）。计费电量 = max(电池端, 墙端)。"""
    plan = await calc.get_plan()
    try:
        res = await calc.compute_session(cp_id, plan, calc.plan_tz(plan))
    except Exception as e:  # noqa: BLE001
        _raise(e)
    if res is None:
        raise HTTPException(404, "charging session not found")
    return res
