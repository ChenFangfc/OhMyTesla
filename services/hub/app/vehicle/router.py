"""车辆渲染图。官方图取得到就用官方图，取不到就让前端用自绘 SVG。"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

from .. import db
from ..config import settings
from ..livestate import live
from . import image

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vehicle", tags=["vehicle"])


async def _config(car_id: int | None) -> tuple[str, str, int] | None:
    cars = live.snapshot()
    key = str(car_id) if car_id is not None and str(car_id) in cars else (
        next(iter(cars)) if cars else None)
    if key is None:
        return None
    c = cars[key]
    model, colour = c.get("model"), c.get("exterior_color")
    if not model:
        try:
            row = await db.fetchrow(
                "SELECT model, exterior_color FROM cars WHERE id = $1", int(key))
            if row:
                model = model or row["model"]
                colour = colour or row["exterior_color"]
        except Exception:  # noqa: BLE001
            pass
    return (model or ""), (colour or ""), int(key)


@router.get("/render")
async def render(car_id: int | None = None, view: str = "STUD_3QTR"):
    """有官方图就回 PNG，没有就回 404 —— 前端据此决定是否落回 SVG。"""
    cfg = await _config(car_id)
    if cfg is None:
        raise HTTPException(404, "no vehicle")
    model, colour, _ = cfg
    if not image.supported(model):
        # Cybertruck 走这里：compositor 不收录，自绘 SVG 才是唯一来源
        raise HTTPException(404, "no official render for this model")

    path = await image.fetch(Path(settings.data_dir) / "vehicle", model, colour, view)
    if path is None or not path.is_file():
        raise HTTPException(404, "render unavailable")
    return Response(path.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/render/status")
async def render_status(car_id: int | None = None):
    """界面据此知道该显示官方图还是自绘图，以及为什么。"""
    cfg = await _config(car_id)
    if cfg is None:
        return {"available": False, "reason": "no_vehicle", "model": None}
    model, colour, _ = cfg
    if not image.supported(model):
        return {"available": False,
                "reason": "model_not_served" if model else "model_unknown",
                "model": model or None}
    return {"available": True, "reason": "", "model": model,
            "colour": image._norm(colour) or None}
