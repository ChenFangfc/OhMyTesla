import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import base64
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .alerts.engine import engine
from .alerts.router import router as alerts_router
from .alerts.sentry import tracker as sentry_tracker
from .alerts.store import store
from .alerts.watchdog import watchdog
from .config import settings
from .dashcam.jobs import jobs
from .dashcam.router import router as dashcam_router
from .dashcam.scanner import scanner
from .dashcam.watcher import watcher
from .fleet.bridge import manager as fleet_bridge
from .i18n.router import router as prefs_router
from .insights.router import router as insights_router
from .setup.router import router as setup_router
from .overview.router import router as overview_router
from .vehicle.router import router as vehicle_router
from .livestate import live
from .tariff.router import router as tariff_router
from .tesla.router import router as tesla_router
from . import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("ohmytesla")

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.open()
    sentry_tracker.bind(engine.fire_custom)
    sentry_tracker.start()
    watchdog.bind(engine.fire_custom)
    watchdog.start()
    live.add_transition_hook(engine.handle)
    # 先从数据库补出最后已知状态，再挂 MQTT。顺序不能反：反过来的话
    # 补进去的旧值会盖住刚收到的实时值。
    await live.seed_from_db()
    live.start()
    asyncio.create_task(_detect_region_once())
    fleet_bridge.start()
    jobs.start()
    watcher.start()
    log.info("OhmyTesla hub started")
    yield
    await fleet_bridge.stop()
    await watchdog.stop()
    await sentry_tracker.stop()
    await watcher.stop()
    await jobs.stop()
    await live.stop()
    await store.close()
    await db.close()


async def _detect_region_once() -> None:
    """启动后台检测一次市场；仅在用户没手动改过偏好时套用默认单位。"""
    await asyncio.sleep(20)   # 等 TeslaMate 把车同步进来
    try:
        from .i18n import prefs as _prefs
        from .i18n import region as _region

        info = await _region.detect()
        if info["market"]:
            p = await _prefs.apply_market(info["market"])
            log.info("region detected: %s (%s) -> locale=%s units=%s/%s",
                     info["market"], "; ".join(info["signals"]), p["locale"], p["length"], p["temp"])
    except Exception:  # noqa: BLE001
        log.debug("region detection skipped", exc_info=True)


app = FastAPI(title="OhmyTesla Hub", lifespan=lifespan)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """可选的全站 HTTP Basic 认证（OHMYTESLA_PASSWORD 非空时启用）。"""

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                # compare_digest 对含非 ASCII 的 str 会抛 TypeError——统一转 bytes
                if user == "ohmytesla" and secrets.compare_digest(
                    pw.encode(), settings.ohmytesla_password.encode()
                ):
                    return await call_next(request)
            except Exception:  # noqa: BLE001
                pass
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="OhmyTesla"'},
        )


if settings.ohmytesla_password:
    app.add_middleware(BasicAuthMiddleware)
app.include_router(alerts_router)
app.include_router(dashcam_router)
app.include_router(insights_router)
app.include_router(tariff_router)
app.include_router(tesla_router)
app.include_router(prefs_router)
app.include_router(setup_router)
app.include_router(overview_router)
app.include_router(vehicle_router)

templates = Jinja2Templates(directory=BASE / "web" / "templates")
app.mount("/static", StaticFiles(directory=BASE / "web" / "static"), name="static")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# 导出成品与转码预览（StaticFiles 自带 Range 支持，可拖动播放）
app.mount("/exports", StaticFiles(directory=settings.exports_dir), name="exports")
app.mount("/preview", StaticFiles(directory=settings.preview_dir), name="preview")


@app.get("/media/{path:path}")
async def media(path: str):
    """TeslaCam 素材。每次请求都重新解析根目录——用户可能在容器启动后
    才把 TeslaCam 文件夹拷进挂载目录，静态挂载会把空目录冻结成 404。"""
    base = scanner.base_path()
    if base is None:
        raise HTTPException(404, "TeslaCam directory not found")
    target = (base / path).resolve()
    if not target.is_relative_to(base.resolve()) or not target.is_file():
        raise HTTPException(404, "file not found")
    media_type = "video/mp4" if target.suffix == ".mp4" else None
    return FileResponse(target, media_type=media_type)


# ---------------- 实时状态 ----------------

@app.get("/api/fleet/bridge/status")
async def fleet_bridge_status():
    """中转桥接健康状态——桥接挂掉的表现就是"没有告警"，必须能看见。"""
    return fleet_bridge.status()


@app.post("/api/fleet/bridge/enable-sentry")
async def fleet_bridge_enable_sentry():
    """请求中转商开始推送 SentryMode 字段（用户显式触发）。"""
    return await fleet_bridge.enable_sentry_field()


@app.get("/api/health/detail")
async def health_detail():
    """真实就绪状态。/healthz 是给 Docker 用的存活探针（永远 ok，否则容器会重启循环），
    这个端点才反映"数据是不是还在流进来"——界面据此显示中断横幅。"""
    return {"data": watchdog.state(), "cars": len(live.cars)}


@app.get("/api/live/state")
async def live_state():
    return live.snapshot()


@app.get("/api/live/stream")
async def live_stream():
    q = live.subscribe()

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'snapshot', 'cars': live.snapshot()}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {event}\n\n"
                except asyncio.TimeoutError:
                    if not live.is_subscribed(q):
                        # 队列积压被踢出后主动断流，让 EventSource 自动重连拿新快照
                        break
                    yield ": keepalive\n\n"
        finally:
            live.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/config")
async def public_config(request: Request):
    host = request.url.hostname or "localhost"
    return {
        "teslamate_url": f"http://{host}:{settings.teslamate_public_port}",
        "grafana_url": f"http://{host}:{settings.grafana_public_port}",
        "lang": settings.alert_lang,
        **(await _map_config()),
    }


async def _map_config() -> dict:
    """地图源与坐标基准。中国大陆访问不了 OSM，且国内源用 GCJ-02 基准。"""
    from .i18n import prefs as _prefs

    p = await _prefs.get()
    in_china = p.get("market") == "CN"
    if settings.tile_url:
        url = settings.tile_url
        datum = settings.tile_datum or ("gcj02" if in_china else "wgs84")
    elif in_china:
        # 高德的公开栅格瓦片；国内可直连，基准是 GCJ-02
        url = "https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"
        datum = "gcj02"
    else:
        # 亮色底图在暗色界面里像个灯箱。CARTO 的 dark_all 是专做的深色栅格底图，
        # 免费、无需 key，署名与 OSM 相同。
        url = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png"
        datum = "wgs84"
    return {"tile_url": url, "tile_datum": datum, "market": p.get("market", "")}


# ---------------- 页面 ----------------

async def page(request: Request, template: str, **ctx):
    """渲染页面。<title> 在服务端就按用户语言出——JS 事后改标题会先闪一下中文。"""
    from .i18n.prefs import get as _get_prefs
    from .i18n.router import catalog as _catalog

    try:
        loc = (await _get_prefs())["locale"]
    except Exception:  # noqa: BLE001
        loc = "en"
    cat = _catalog(loc)
    ctx["locale"] = loc
    ctx["html_lang"] = "zh-CN" if loc == "zh" else "en"
    ctx["t"] = lambda k, d="": cat.get(k, d or k)
    return templates.TemplateResponse(request, template, ctx)


@app.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request):
    return await page(request, "welcome.html", active="")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # 还没接上特斯拉账号时直接进引导页——用户不需要知道底层有几个服务
    try:
        from .tesla import vault

        if not await vault.has_tokens():
            return RedirectResponse("/welcome", status_code=307)
    except Exception:  # noqa: BLE001 - 数据库没就绪就照常渲染首页
        pass
    return await page(request, "overview.html", active="home")


@app.get("/dashcam", response_class=HTMLResponse)
async def dashcam_page(request: Request):
    return await page(request, "dashcam.html", active="dashcam")


@app.get("/dashcam/play", response_class=HTMLResponse)
async def dashcam_play(request: Request):
    return await page(request, "player.html", active="dashcam")


@app.get("/dashcam/usb", response_class=HTMLResponse)
async def dashcam_usb(request: Request):
    return await page(request, "usb.html", active="dashcam")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return await page(request, "settings.html", active="settings")


@app.get("/sw.js")
async def service_worker():
    """service worker 必须由根路径提供，否则作用域受限收不到推送。"""
    return FileResponse(
        BASE / "web" / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.json")
async def manifest():
    return FileResponse(BASE / "web" / "static" / "manifest.json", media_type="application/manifest+json")


# 新导航的规范路径。旧路径保留跳转，书签不会断。
@app.get("/energy", response_class=HTMLResponse)
async def energy_page(request: Request):
    return await page(request, "energy.html", active="energy")


@app.get("/trips", response_class=HTMLResponse)
async def trips_page(request: Request):
    return await page(request, "trips.html", active="trips")


@app.get("/security", response_class=HTMLResponse)
async def security_page(request: Request):
    return await page(request, "security.html", active="security")


@app.get("/insights")
async def _legacy_insights():
    return RedirectResponse("/energy", status_code=301)


@app.get("/drives")
async def _legacy_drives():
    return RedirectResponse("/trips", status_code=301)


@app.get("/alerts")
async def _legacy_alerts():
    return RedirectResponse("/security", status_code=301)


@app.get("/control")
async def _legacy_control():
    # 常用控制已在首页 hero，其余在 ⌘K —— 不再需要独立页面
    return RedirectResponse("/", status_code=301)
