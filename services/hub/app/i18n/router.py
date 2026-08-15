import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import prefs, region

router = APIRouter(prefix="/api/prefs", tags=["prefs"])


@router.get("")
async def get_prefs():
    p = await prefs.get()
    return {
        **p,
        "labels": {
            k: prefs.label(k, p)
            for k in ("length", "speed", "temp", "pressure", "consumption")
        },
    }


class PrefsBody(BaseModel):
    locale: str | None = None
    length: str | None = None
    temp: str | None = None
    pressure: str | None = None
    auto: bool | None = None
    currency: str | None = None


@router.post("")
async def set_prefs(body: PrefsBody):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    # 只有手动改**单位**才退出"跟随地区"模式；
    # 改语言不该影响单位跟随（在北美的中文用户 = 中文界面 + 英里）
    if any(k in patch for k in ("length", "temp", "pressure")):
        patch.setdefault("auto", False)
    return await prefs.set_prefs(patch)


@router.post("/detect")
async def detect_region():
    """重新检测市场并（在自动模式下）套用该市场的默认单位。"""
    info = await region.detect()
    p = await prefs.apply_market(info["market"]) if info["market"] else await prefs.get()
    return {"detected": info, "prefs": p}


_LOCALE_DIR = Path(__file__).parent / "locales"
_cache: dict[str, tuple[float, dict]] = {}


def catalog(locale: str) -> dict:
    """读文案字典，按文件修改时间热重载（改文案不用重启容器）。"""
    loc = locale if locale in ("zh", "en") else "en"
    f = _LOCALE_DIR / f"{loc}.json"
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return {}
    hit = _cache.get(loc)
    if hit and hit[0] == mtime:
        return hit[1]
    data = json.loads(f.read_text(encoding="utf-8"))
    _cache[loc] = (mtime, data)
    return data


def t(key: str, locale: str, **vars) -> str:
    """服务端取文案。推送文案要用用户选的语言，不能看浏览器。"""
    s = catalog(locale).get(key) or catalog("en").get(key) or key
    for k, v in vars.items():
        s = s.replace("{" + k + "}", str(v))
    return s


@router.get("/catalog/{locale}")
async def get_catalog(locale: str):
    if locale not in ("zh", "en"):
        raise HTTPException(404, "Unknown locale")
    return catalog(locale)
