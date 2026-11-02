"""Render the film: every frame is FILM.seek(t), captured and piped into ffmpeg.

    python3 promo/scripts/music.py                       # → promo/out/soundtrack.wav
    python3 promo/scripts/render.py --lang en            # → promo/out/ohmytesla-film.en.mp4
    python3 promo/scripts/render.py --lang zh --webp     # also a looping WebP for the README

Frames are drawn at three times the pixels (5760×3240, the truck included) and
scaled down with lanczos to 4K (3840×2160), so edges and small UI text stay crisp;
the soundtrack is muxed in at −16 LUFS.
The truck is WebGL (src/car3d.js): on a Mac, Chromium draws it on the GPU through
Metal; elsewhere it falls back to software rendering, which is slower but the same.
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


async def render(lang: str, fps: int, out: Path, webp: Path | None, webp_width: int, webp_fps: int, crf: str,
                 dsf: int, size: str, audio: Path | None):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
                            "--directory", str(ROOT / "src")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h = size.split("x")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-"]
    if audio and audio.exists():
        # loudnorm alone let the AAC encoder overshoot to +2 dBFS on the drums;
        # a limiter at −4 dB after it keeps the decoded peak near −3.6 dBFS.
        cmd += ["-i", str(audio), "-map", "0:v", "-map", "1:a",
                "-af", "loudnorm=I=-16:TP=-3:LRA=11,alimiter=limit=0.63:attack=1:release=50:level=false",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest"]
    cmd += ["-vf", f"scale={w}:{h}:flags=lanczos", "-c:v", "libx264", "-preset", "slow", "-crf", crf,
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", str(out)]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    frames_for_webp = []
    try:
        async with async_playwright() as p:
            gpu = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"] if sys.platform == "darwin" else []
            browser = await p.chromium.launch(args=gpu)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=dsf)
            await page.goto(f"http://127.0.0.1:{port}/film.html?render&lang={lang}&pr={dsf}", wait_until="load")
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
                                duration=round(1000 / webp_fps), loop=0, quality=80, method=6)
        print(f"  {lang}: {webp} ({webp.stat().st_size / 1e6:.1f} MB, {len(frames_for_webp)} frames)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=("en", "zh"), default="en")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--crf", default="18")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--webp", action="store_true", help="also write a looping WebP for the README")
    ap.add_argument("--webp-width", type=int, default=1280)
    ap.add_argument("--webp-fps", type=int, default=12)
    ap.add_argument("--dsf", type=int, default=3, help="pixels drawn per CSS pixel before scaling down")
    ap.add_argument("--size", default="3840x2160", help="output video size")
    ap.add_argument("--audio", type=Path, default=ROOT / "out" / "soundtrack.wav", help="soundtrack (none if missing)")
    a = ap.parse_args()
    out = a.out or ROOT / "out" / f"ohmytesla-film.{a.lang}.mp4"
    webp = ROOT / "out" / f"ohmytesla-film.{a.lang}.webp" if a.webp else None
    asyncio.run(render(a.lang, a.fps, out, webp, a.webp_width, a.webp_fps, a.crf, a.dsf, a.size, a.audio))
