"""官方车辆渲染图 —— 取自 Tesla 的 configurator compositor，本地缓存。

三条硬约束：

1. **绝不让用户浏览器直连 Tesla。** 热链接会把每个用户的 IP 暴露给
   Tesla，也等于告诉他们谁在用第三方工具。这里由服务端取一次、落盘、
   之后一律走本地 /api/vehicle/image。

2. **绝不把图片提交进仓库。** 这是 Tesla 的版权素材，AGPL 项目里
   随源码分发会有问题。缓存目录进 .gitignore。

3. **取不到就退回自绘 SVG，不留白。** compositor 只服务 S/3/X/Y；
   Cybertruck 返回 412（未收录）。而且请求密了会被限流封 IP，所以
   失败要长时间退避，不能重试风暴。

已实测：model=ms/m3/mx/my 返回 1440×810 透明背景 PNG；model=ct/cb/ctk
一律 412。约 20 次请求后源站开始 403 限流。
"""

import asyncio
import hashlib
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

COMPOSITOR = "https://static-assets.tesla.com/configurator/compositor"

# TeslaMate 的 cars.model 存单字母。Cybertruck 不在其中 —— compositor 不收录。
MODELS = {"S": "ms", "3": "m3", "X": "mx", "Y": "my"}

# TeslaMate 存 API 原值（如 "PearlWhite"）→ compositor 选装码。
# 只列我能对上的；对不上的宁可不传 paint 让 Tesla 用默认色，
# 也不能猜一个错的 —— 那会渲染出一辆颜色不对的车而没人发现。
PAINT = {
    "PearlWhite": "$PPSW", "PearlWhiteMulticoat": "$PPSW",
    "SolidBlack": "$PBSB", "Black": "$PBSB", "ObsidianBlack": "$PBSB",
    "MidnightSilver": "$PMNG", "MidnightSilverMetallic": "$PMNG",
    "DeepBlue": "$PPSB", "DeepBlueMetallic": "$PPSB",
    "RedMulticoat": "$PPMR", "Red": "$PPMR",
    "SilverMetallic": "$PMSS", "Silver": "$PMSS",
    "SteelGrey": "$PMNG",
    "StealthGrey": "$PN01",
    "UltraRed": "$PR01",
    "Quicksilver": "$PN00",
    "DiamondBlack": "$PMBL",
}

CACHE_TTL_S = 60 * 60 * 24 * 30      # 车漆不会变，缓存一个月
FAIL_BACKOFF_S = 60 * 60 * 6         # 失败后退避 6 小时，避免重试风暴招来封禁
_TIMEOUT = httpx.Timeout(20.0)

_fail_until: dict[str, float] = {}
_lock = asyncio.Lock()


def _norm(value: str | None) -> str:
    """去掉 API 的 ExteriorColor / WheelType 前缀，只留关键词。"""
    if not value:
        return ""
    s = str(value)
    for prefix in ("ExteriorColor", "WheelType", "PaintColor"):
        s = s.removeprefix(prefix)
    return s


# 公开别名：别的模块要用就用这个，不要伸手去拿 _norm
normalise_colour = _norm


def supported(model: str | None) -> bool:
    return (model or "").upper() in MODELS


def build_url(model: str, exterior_color: str | None, view: str = "STUD_3QTR",
              size: int = 1400) -> str | None:
    code = MODELS.get((model or "").upper())
    if code is None:
        return None
    opts = []
    paint = PAINT.get(_norm(exterior_color))
    if paint:
        opts.append(paint)
    # 轮毂码逐年逐车型都不同，我没有能核实的对照表；不传就用该车型默认轮毂，
    # 比渲染出一双错的轮毂要好。
    q = [
        "context=design_studio_2",
        f"model={code}",
        f"view={view}",
        f"size={size}",
        "bkba_opt=1",          # 透明背景
        "crop=0,0,0,0",
    ]
    if opts:
        q.append("options=" + ",".join(opts))
    return f"{COMPOSITOR}?" + "&".join(q)


def _cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".png")


async def fetch(cache_dir: Path, model: str, exterior_color: str | None,
                view: str = "STUD_3QTR") -> Path | None:
    """返回本地缓存文件；取不到返回 None（调用方退回 SVG）。"""
    url = build_url(model, exterior_color, view)
    if url is None:
        return None

    path = _cache_path(cache_dir, url)
    if path.is_file() and path.stat().st_size > 4096:  # noqa: SIM102 - 分开写是为了让两个条件各自带注释说明
        if time.time() - path.stat().st_mtime < CACHE_TTL_S:
            return path

    if time.time() < _fail_until.get(url, 0):
        return path if path.is_file() else None

    async with _lock:                       # 同一时刻只放一个请求出去
        if path.is_file() and time.time() - path.stat().st_mtime < CACHE_TTL_S:
            return path
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as c:
                r = await c.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
                    "Accept": "image/png,image/*,*/*",
                })
        except httpx.HTTPError as e:
            log.info("vehicle image fetch failed (%s); using the built-in render", e)
            _fail_until[url] = time.time() + FAIL_BACKOFF_S
            return path if path.is_file() else None

        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or not ct.startswith("image/"):
            # 412 = 该车型未收录（Cybertruck）；403 = 源站限流
            log.info("compositor returned %s (%s) for model=%s — using the built-in render",
                     r.status_code, ct or "no content-type", model)
            _fail_until[url] = time.time() + FAIL_BACKOFF_S
            return path if path.is_file() else None

        cache_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - 本地 stat/exists，微秒级；为它开线程的开销大于调用本身
        tmp = path.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(path)                   # 原子替换，避免读到写了一半的文件
        log.info("cached official render for model=%s (%d KB)", model, len(r.content) // 1024)
        return path
