import asyncio
import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from . import db
from .alerts.engine import engine
from .alerts.janitor import janitor
from .alerts.maintenance import watcher as maintenance_watcher
from .alerts.router import router as alerts_router
from .alerts.sentry import tracker as sentry_tracker
from .alerts.store import store
from .alerts.watchdog import watchdog
from .automation.engine import engine as automation
from .automation.router import router as automation_router
from .compat.teslamateapi import router as tma_router
from .config import settings
from .dashcam.jobs import jobs
from .dashcam.router import router as dashcam_router
from .dashcam.scanner import scanner
from .dashcam.watcher import watcher
from .fleet.bridge import manager as fleet_bridge
from .health.export import router as export_router
from .health.router import router as health_router
from .i18n.router import router as prefs_router
from .insights.router import router as insights_router
from .integrations.abrp import relay as abrp_relay
from .integrations.hass import discovery as hass_discovery
from .livestate import live
from .mcp.router import router as mcp_router
from .overview.router import router as overview_router
from .setup.router import router as setup_router
from .tariff.router import router as tariff_router
from .tesla.router import router as tesla_router
from .trips.router import router as trips_router
from .vehicle.router import router as vehicle_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("ohmytesla")

BASE = Path(__file__).parent

# 后台一次性任务的强引用池，防止被 GC。见 lifespan 里的用法。
_bg_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.open()
    sentry_tracker.bind(engine.fire_custom)
    sentry_tracker.start()
    watchdog.bind(engine.fire_custom)
    watchdog.start()
    live.add_transition_hook(engine.handle)
    # 自动化和告警共用同一条 transition 管道。顺序无所谓：两者互不依赖，
    # 而且都在自己内部处理"回放期不执行"。
    live.add_transition_hook(automation.handle)
    # 先从数据库补出最后已知状态，再挂 MQTT。顺序不能反：反过来的话
    # 补进去的旧值会盖住刚收到的实时值。
    await live.seed_from_db()
    live.start()
    # 必须留引用：asyncio 对运行中的任务只持弱引用，不留的话这个任务
    # 可能在它自己的 await sleep(20) 中途被 GC 掉，市场检测就静默不执行了。
    # db.py 的 _CLOSING 集合是同一个坑的另一处体现。
    _bg_tasks.add(t := asyncio.create_task(_detect_region_once()))
    t.add_done_callback(_bg_tasks.discard)
    fleet_bridge.start()
    jobs.start()
    watcher.start()
    janitor.start()
    abrp_relay.start()
    maintenance_watcher.bind(engine.fire_custom)
    maintenance_watcher.start()
    hass_discovery.start()
    automation.start()
    log.info("OhmyTesla hub started")
    yield
    await automation.stop()
    await hass_discovery.stop()
    await maintenance_watcher.stop()
    await abrp_relay.stop()
    await janitor.stop()
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
    except Exception:
        log.debug("region detection skipped", exc_info=True)


app = FastAPI(title="OhmyTesla Hub", lifespan=lifespan)


# 认证豁免的路径。只有存活探针 —— 它必须在没配密码时也能答，
# 否则容器会陷入健康检查失败→重启的循环，而用户看到的是"装不上"。
_PUBLIC_PATHS = frozenset({"/healthz"})

# 改状态的方法。跨站来源校验只卡这些；GET 不该有副作用，
# 真有副作用的 GET 是另一个要修的问题，不是靠这里兜。
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """全站 HTTP Basic 认证 + 跨站来源校验。

    这个中间件现在是**无条件挂载**的。之前是 `if password: add_middleware(...)`，
    而 OHMYTESLA_PASSWORD 出厂为空、8800 又绑在 0.0.0.0 —— 于是按 README
    走一遍 `cp .env.example .env && ./omt up` 得到的是一个局域网内任何设备
    都能 POST /api/tesla/command 解锁车门、能用 /mcp 拉全部位置历史的服务。
    条件挂载最坏的地方在于它不是"放行"而是"闸门不存在"，日志里连一条痕迹都没有。
    现在没配密码 = 除探针外一律 503，把「没配置」变成「不可用」而不是「不设防」。

    第二道是跨站来源校验。HTTP Basic 的凭据由浏览器按 origin 自动附带，
    所以攻击者页面上一个 `<form method=post action="http://<hub>:8800/...">`
    就能带着车主的凭据打进来。多个端点不读 body（clear_history、recompute、
    index/rebuild 都是纯 query 或无参），表单提交的 Content-Type 又在 CORS
    安全列表里、不触发预检，因此这条路是通的。前端全部同源 fetch，不受影响。
    """

    async def dispatch(self, request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        if not settings.ohmytesla_password:
            return Response(
                status_code=503,
                content=(
                    b"OHMYTESLA_PASSWORD is not set. This portal can unlock your car and "
                    b"read your full location history, so it refuses to serve without one.\n"
                    b"Set it in .env and restart:  ./omt portal-password\n"
                ),
                media_type="text/plain; charset=utf-8",
            )

        if request.method in _UNSAFE_METHODS and not _same_site(request):
            log.warning("blocked cross-site %s %s (origin=%r)",
                        request.method, request.url.path, request.headers.get("origin"))
            return Response(status_code=403, content=b"cross-site request blocked",
                            media_type="text/plain; charset=utf-8")

        # try 只罩解码，**绝不能罩住 call_next**。
        # 原来 `return await call_next(request)` 就写在 try 里，于是任何一个
        # 路由抛出的异常都会被 `except Exception: pass` 吞掉，然后掉到下面
        # 返回 401 —— 一个后端 bug 被伪装成"密码不对"。实测：新加的
        # /api/v1/cars/1/drives 因为字段名写错而抛异常，客户端看到的却是
        # 401，日志里也只有一行"auth failed"，排查方向被彻底带偏。
        ok = False
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                # 用户名也要常量时间比较：== 会在第一个不同字符处返回，
                # 泄露"猜对了几个字母"。它是固定值，但代价只有几微秒。
                ok = secrets.compare_digest(user.encode(), b"ohmytesla") and secrets.compare_digest(
                    pw.encode(), settings.ohmytesla_password.encode()
                )
            except Exception:  # noqa: BLE001 - 头部格式不对就是认证失败
                ok = False
        if ok:
            return await call_next(request)
        log.warning("auth failed for %s %s from %s", request.method, request.url.path,
                    request.client.host if request.client else "?")
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="OhmyTesla"'},
        )


def _same_site(request) -> bool:
    """这个改状态的请求是不是来自本站。

    优先看 Sec-Fetch-Site（Chrome/Firefox/Safari 都发，且**不可被脚本伪造**）；
    老浏览器没有它时回退比对 Origin 的 host:port。两者都没有的时候放行 ——
    那是 curl / 移动 App / 脚本，它们本来就不受浏览器的凭据自动附带影响，
    卡它们只会把 CLI 用法一起打死。
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site:
        return fetch_site in ("same-origin", "same-site", "none")

    origin = request.headers.get("origin")
    if not origin:
        return True                      # 非浏览器发起
    try:
        from urllib.parse import urlsplit

        o = urlsplit(origin)
        return (o.hostname, o.port or (443 if o.scheme == "https" else 80)) == (
            request.url.hostname, request.url.port or (443 if request.url.scheme == "https" else 80)
        )
    except Exception:  # noqa: BLE001
        return False


app.add_middleware(BasicAuthMiddleware)
app.include_router(alerts_router)
app.include_router(dashcam_router)
app.include_router(insights_router)
app.include_router(tariff_router)
app.include_router(trips_router)
app.include_router(mcp_router)
app.include_router(health_router)
app.include_router(export_router)
app.include_router(tesla_router)
app.include_router(prefs_router)
app.include_router(setup_router)
app.include_router(overview_router)
app.include_router(vehicle_router)
# TeslaMateApi 兼容层：让 MateDroid / T-Buddy 这类现成 App 直接连过来
app.include_router(tma_router)
app.include_router(automation_router)

templates = Jinja2Templates(directory=BASE / "web" / "templates")
app.mount("/static", StaticFiles(directory=BASE / "web" / "static"), name="static")


@app.get("/healthz")
async def healthz():
    """存活探针。这是唯一不需要认证的端点，所以只暴露运维必需的事实，
    不带任何车辆数据。

    schema 版本放在这里是因为排障第一步永远是"跑的是哪一版"：
    容器升级但迁移没跑成、或者两台机器版本不一致，都从这里看出来。
    drifted 非空表示有人改了已发布的迁移，那是需要人工介入的状态。
    """
    from .alerts.store import store

    s = store.schema
    body = {"ok": True, "schema": s.get("version", 0)}
    if s.get("drifted"):
        # 健康检查照样返回 200：schema 漂移不影响服务可用性，
        # 但要让轮询它的人看得见。
        body["schema_drift"] = s["drifted"]
    return body


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


@app.get("/api/integrations/abrp/status")
async def abrp_status():
    """中继挂掉的表现是"ABRP 里的数据不动了"，本地必须看得见。"""
    return abrp_relay.status()


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
    except Exception:
        log.debug("suppressed", exc_info=True)
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


@app.get("/timeline", response_class=HTMLResponse)
async def timeline_page(request: Request):
    return await page(request, "timeline.html", active="timeline")


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
