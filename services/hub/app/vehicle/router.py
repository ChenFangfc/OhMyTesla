"""车辆渲染图。官方图取得到就用官方图，取不到就让前端用自绘 SVG。"""

import logging
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

from .. import db
from ..config import settings
from ..livestate import live
from . import catalog, image, models3d

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vehicle", tags=["vehicle"])
# Owner-supplied GLBs live in the data dir, outside /static.
files_router = APIRouter(tags=["vehicle"])


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
        except Exception:
            log.debug("suppressed", exc_info=True)
    if not model and vin:
        model = _VIN_MODEL.get(vin[:3].upper(), "")
    return (model or ""), (colour or ""), cid


def _bundled_path(model: str) -> Path | None:
    name = BUNDLED.get((model or "").upper())
    if not name:
        return None
    p = Path(__file__).resolve().parent.parent / "web" / "static" / "vehicle" / name
    return p if p.is_file() else None


# Tesla compositor 认得的视角。必须白名单：view 会原样进 compositor 的请求
# 参数、也会进本地缓存文件名，不卡的话页面上一个
# `<img src="/api/vehicle/render?view=xxx1">` 循环就能既撑爆缓存目录、
# 又替用户把 IP 打到被 Tesla 限流。
_VIEWS = frozenset({"STUD_3QTR", "STUD_SIDE", "STUD_REAR", "STUD_SEAT", "STUD_WHEEL"})


@router.get("/render")
async def render(car_id: int | None = None, view: str = "STUD_3QTR"):
    """按优先级给车图：官方 compositor → 自带素材 → 404（前端落回自绘 SVG）。"""
    if view not in _VIEWS:
        raise HTTPException(400, f"unknown view; expected one of {sorted(_VIEWS)}")
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


async def _car(car_id: int | None) -> dict | None:
    """The car's identity row: DB first, live MQTT values on top (fresher)."""
    cars = live.snapshot()
    keys = sorted(int(k) for k in cars if k.isdigit())
    if car_id is None:
        car_id = keys[0] if keys else None
    row = None
    try:
        if car_id is None:
            row = await db.fetchrow("SELECT * FROM cars ORDER BY display_priority, id LIMIT 1")
        else:
            row = await db.fetchrow("SELECT * FROM cars WHERE id = $1", car_id)
    except Exception:
        log.debug("cars lookup failed", exc_info=True)
    if row is None and car_id is None:
        return None
    out = dict(row) if row else {"id": car_id}
    live_car = cars.get(str(out["id"]), {})
    for k in ("model", "trim_badging", "exterior_color", "wheel_type", "display_name"):
        if live_car.get(k):
            out[k] = live_car[k]
    return out


def _identity(car: dict) -> catalog.Identity:
    return catalog.identify(
        vin=car.get("vin"), model=car.get("model"), trim_badging=car.get("trim_badging"),
        marketing_name=car.get("marketing_name"), exterior_color=car.get("exterior_color"),
        wheel_type=car.get("wheel_type"))


@router.get("/model")
async def vehicle_model(car_id: int | None = None):
    """Everything a view needs to draw this car: identity, the 3D model to load
    (or null), and whether a 2D render exists as the fallback."""
    car = await _car(car_id)
    if car is None:
        raise HTTPException(404, "no vehicle")
    ident = _identity(car)
    m = models3d.resolve(ident.key)
    if m is not None and ident.length_m:
        m.setdefault("length_m", ident.length_m)
    tm_model = {"cybertruck": "CT", "model3": "3", "modely": "Y", "models": "S", "modelx": "X"}.get(ident.key or "", "")
    has_2d = image.supported(tm_model) or _bundled_path(tm_model) is not None
    return {
        "car_id": car["id"],
        "name": car.get("name") or car.get("display_name") or ident.name,
        "identity": ident.as_dict(),
        "model": m,
        "render": f"/api/vehicle/render?car_id={car['id']}" if has_2d else None,
    }


@router.get("/models")
async def list_models():
    """Which model lines have a 3D model, and from where — for Settings."""
    avail = models3d.available()
    return [
        {"key": k, "name": meta["name"], "bundled": avail.get(k, {}).get("bundled"),
         "user": avail.get(k, {}).get("user")}
        for k, meta in catalog.MODELS.items()
    ]


@router.post("/models/{key}")
async def upload_model(key: str, file: UploadFile = File(...)):  # noqa: B008 - FastAPI's File() belongs in the default, by framework convention
    """Install the owner's own .glb for a model line (takes priority over the
    bundled one). Validated structurally before it is stored."""
    if key not in catalog.MODELS:
        raise HTTPException(400, f"unknown model line; expected one of {sorted(catalog.MODELS)}")
    data = await file.read(models3d.MAX_UPLOAD + 1)
    try:
        info = models3d.save_user_model(key, data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True, "key": key, **info}


@router.delete("/models/{key}")
async def delete_model(key: str):
    if not models3d.delete_user_model(key):
        raise HTTPException(404, "no user model for that key")
    return {"ok": True}


@files_router.get("/models/{name}")
async def user_model_file(name: str):
    key, _, ext = name.rpartition(".")
    if ext != "glb" or not models3d.valid_key(key):
        raise HTTPException(404)
    p = models3d.user_dir() / f"{key}.glb"
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type="model/gltf-binary",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.get("/state")
async def vehicle_state(car_id: int, refresh: bool = False):
    """Full current state for the controls screen (only read when the car is
    already awake; never wakes it)."""
    from . import state

    return await state.read(car_id, refresh)


@router.post("/wake")
async def vehicle_wake(car_id: int):
    from . import state

    return await state.wake(car_id)
