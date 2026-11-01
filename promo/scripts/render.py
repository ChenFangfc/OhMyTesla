"""Render the film: every frame is FILM.seek(t), captured and piped into ffmpeg.

    python3 promo/scripts/render.py --lang en            # → promo/out/ohmytesla-film.en.mp4
    python3 promo/scripts/render.py --lang zh --webp     # also a small looping WebP for the README

Nothing is captured in real time, so a slow machine renders the same film.
Needs Python 3.10+, playwright (chromium), Pillow, and ffmpeg with libx264.
"""
import argparse
import asyncio
import base64
import io
import socket
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent


async def render(lang: str, fps: int, out: Path, webp: Path | None, webp_width: int, webp_fps: int, crf: str):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
                            "--directory", str(ROOT / "src")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps),
                           "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", crf,
                           "-pix_fmt", "yuv420p", "-profile:v", "high", "-colorspace", "bt709",
                           "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", str(out)],
                          stdin=subprocess.PIPE)
    frames_for_webp = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.goto(f"http://127.0.0.1:{port}/film.html?render&lang={lang}", wait_until="load")
            await page.evaluate("FILM.ready.then(() => true)")
            duration = await page.evaluate("FILM.duration")
            cdp = await page.context.new_cdp_session(page)
            n = round(duration * fps)
            step = max(1, round(fps / webp_fps))
            t0 = time.time()
            for i in range(n):
                await page.evaluate(f"FILM.seek({i / fps})")
                await page.evaluate("new Promise((r) => requestAnimationFrame(() => r()))")
                shot = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 95, "optimizeForSpeed": True})
                data = base64.b64decode(shot["data"])
                ff.stdin.write(data)
                if webp and i % step == 0:
                    im = Image.open(io.BytesIO(data)).convert("RGB")
                    frames_for_webp.append(im.resize((webp_width, round(webp_width * 9 / 16)), Image.LANCZOS))
                if i % (fps * 2) == 0:
                    print(f"\r  {lang}: {i}/{n} frames  {i / max(time.time() - t0, 0.01):.1f} fps   ", end="", flush=True)
            await browser.close()
    finally:
        ff.stdin.close()
        ff.wait()
        srv.terminate()
    print(f"\r  {lang}: {out} ({out.stat().st_size / 1e6:.1f} MB)              ")
    if webp:
        webp.parent.mkdir(parents=True, exist_ok=True)
        frames_for_webp[0].save(webp, save_all=True, append_images=frames_for_webp[1:],
                                duration=round(1000 / webp_fps), loop=0, quality=70, method=6)
        print(f"  {lang}: {webp} ({webp.stat().st_size / 1e6:.1f} MB, {len(frames_for_webp)} frames)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=("en", "zh"), default="en")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--crf", default="18")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--webp", action="store_true", help="also write a looping WebP for the README")
    ap.add_argument("--webp-width", type=int, default=960)
    ap.add_argument("--webp-fps", type=int, default=10)
    a = ap.parse_args()
    out = a.out or ROOT / "out" / f"ohmytesla-film.{a.lang}.mp4"
    webp = ROOT / "out" / f"ohmytesla-film.{a.lang}.webp" if a.webp else None
    asyncio.run(render(a.lang, a.fps, out, webp, a.webp_width, a.webp_fps, a.crf))
