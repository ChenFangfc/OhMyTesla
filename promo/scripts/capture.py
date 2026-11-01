"""Capture the screens the film uses, in both languages.

    python3 promo/scripts/capture.py <site_dir> [--out promo/src/shots]

<site_dir> is a static demo build of the UI (synthetic data, a mock API
layer) made with the BUNDLED model — never a private one: these frames are
published. It is served locally; nothing leaves this machine.

Each screen: 1440×900 CSS px at 3× pixels (sharp when the film moves in), the app's
default light theme, saved as JPEG.
Needs Python 3.10+, playwright (chromium) and Pillow.
"""
import argparse
import asyncio
import io
import socket
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

# The host page around the demo's index (it ships as body content).
SKELETON = ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>{content}</body></html>')
SHOTS = [
    # name, page, settle seconds, what to do before the shot
    ("overview", "", 8, None),
    ("security", "security.html", 4, None),
    ("driving", "data-driving.html", 6, None),
    ("charging", "data-charging.html", 4, None),
    ("battery", "data-battery.html", 4, None),
    ("footprint", "data-footprint.html", 7, None),
    ("dashcam", "dashcam.html", 4, None),
    ("controls", "controls.html", 4, "confirm"),
]


async def main(site: Path, out: Path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
                            "--directory", str(site)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}/"
    time.sleep(0.8)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader",
                                                    "--ignore-gpu-blocklist"])
            for lang in ("en", "zh"):
                (out / lang).mkdir(parents=True, exist_ok=True)
                ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=3,
                                                color_scheme="light", locale="en-US" if lang == "en" else "zh-CN",
                                                timezone_id="America/Los_Angeles")

                async def wrap(route, request):
                    path = request.url[len(base) - 1:].split("?")[0]
                    if request.resource_type == "document" and path in ("/", "/index.html"):
                        resp = await route.fetch()
                        body = SKELETON.replace("{content}", (await resp.body()).decode("utf-8"))
                        return await route.fulfill(status=200, body=body, headers={"content-type": "text/html; charset=utf-8"})
                    return await route.continue_()
                await ctx.route(f"{base}**", wrap)
                for name, page_path, settle, action in SHOTS:
                    page = await ctx.new_page()
                    await page.goto(base + page_path, wait_until="load")
                    await page.wait_for_timeout(settle * 1000)
                    # the demo's own badge is not part of the product
                    await page.add_style_tag(content="#omt-demo-badge { display: none !important; }")
                    if action == "confirm":
                        # a risky command: the sheet that asks first
                        await page.click('[data-cmd="frunk"]')
                        await page.wait_for_selector("dialog[open]")
                        await page.wait_for_timeout(900)
                    png = await page.screenshot()
                    im = Image.open(io.BytesIO(png)).convert("RGB")
                    im.save(out / lang / f"{name}.jpg", quality=90, optimize=True, progressive=True)
                    print(f"  {lang} {name:10s} {im.size[0]}×{im.size[1]}")
                    await page.close()
                await ctx.close()
            await browser.close()
    finally:
        srv.terminate()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("site", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "src" / "shots")
    a = ap.parse_args()
    asyncio.run(main(a.site.resolve(), a.out.resolve()))
