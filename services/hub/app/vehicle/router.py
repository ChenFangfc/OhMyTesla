"""车辆渲染图。官方图取得到就用官方图，取不到就让前端用自绘 SVG。"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response  # noqa: F401

from .. import db
from ..config import settings
from ..livestate import live
from . import image

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vehicle", tags=["vehicle"])


# 本仓库自带的车型渲染图（Tesla 的 compositor 不收录这些）。
# 文件放在 static/vehicle/，属版权素材，随 .gitignore 不入库。
BUNDLED = {"CT": "cybertruck.png"}

# VIN 前三位（WMI+车型位）→ TeslaMate 的 model 码。TeslaMate 对
# Cybertruck 常常留空 model，光看 model 认不出车。**只用来认车型，
# 不用来判市场** —— 产地和销售市场两回事，见 i18n/region.py。
_VIN_MODEL = {"7G2": "CT"}


async def _config(car_id: int | None) -> tuple[str, str, int] | None:
    cars = live.snapshot()
    # livestate 在 VIN→car_id 映射还没建好时会造 "vin:XXX" 这种占位键，
    # 直接 int() 会 ValueError → 500。认不出编号就当没有车。
    keys = [k for k in cars if k.isdigit()]
    key = str(car_id) if car_id is not None and str(car_id) in cars and str(car_id).isdigit() else (
        keys[0] if keys else None)
    if key is None:
        return None
    cid = int(key)
    c = cars[key]
    model, colour = c.get("model"), c.get("exterior_color")
    vin = c.get("vin") or ""
    if not model or not colour or not vin:
        try:
            row = await db.fetchrow(
                "SELECT model, exterior_color, vin FROM cars WHERE id = $1", cid)
            if row:
                model = model or row["model"]
                colour = colour or row["exterior_color"]
                vin = vin or (row["vin"] or "")
        except Exception:  # noqa: BLE001
            pass
    if not model and vin:
        model = _VIN_MODEL.get(vin[:3].upper(), "")
    return (model or ""), (colour or ""), cid


def _bundled_path(model: str) -> Path | None:
    name = BUNDLED.get((model or "").upper())
    if not name:
        return None
    p = Path(__file__).resolve().parent.parent / "web" / "static" / "vehicle" / name
    return p if p.is_file() else None


@router.get("/render")
async def render(car_id: int | None = None, view: str = "STUD_3QTR"):
    """按优先级给车图：官方 compositor → 自带素材 → 404（前端落回自绘 SVG）。"""
    cfg = await _config(car_id)
    if cfg is None:
        raise HTTPException(404, "no vehicle")
    model, colour, _ = cfg

    if image.supported(model):
        path = await image.fetch(Path(settings.data_dir) / "vehicle", model, colour, view)
        if path is not None and path.is_file():
            return Response(path.read_bytes(), media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    # compositor 不收录的车型（Cybertruck 返回 412），用仓库自带的那张
    bundled = _bundled_path(model)
    if bundled is not None:
        return Response(bundled.read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    raise HTTPException(404, "no render for this model")


@router.get("/render/status")
async def render_status(car_id: int | None = None):
    """界面据此知道该显示官方图还是自绘图，以及为什么。"""
    cfg = await _config(car_id)
    if cfg is None:
        return {"available": False, "reason": "no_vehicle", "model": None}
    model, colour, _ = cfg
    if image.supported(model):
        return {"available": True, "source": "official", "reason": "", "model": model,
                "colour": image.normalise_colour(colour) or None}
    if _bundled_path(model) is not None:
        return {"available": True, "source": "bundled", "reason": "", "model": model,
                "colour": image.normalise_colour(colour) or None}
    return {"available": False, "source": "drawn",
            "reason": "model_not_served" if model else "model_unknown",
            "model": model or None}
