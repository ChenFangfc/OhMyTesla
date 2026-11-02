"""Render dmg/background.html into the DMG window's background.

    python3 packaging/macos/scripts/render_dmg_background.py

Writes dmg/background.png (660 × 480) and dmg/background@2x.png (1320 × 960);
make_dmg.sh hands them to dmgbuild, which joins the two into one HiDPI image.
Needs Python 3 with playwright (chromium). Re-run after editing the HTML and commit
both PNGs: the build itself never renders anything.
"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

DMG = Path(__file__).resolve().parent.parent / "dmg"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for scale, name in ((1, "background.png"), (2, "background@2x.png")):
            page = await browser.new_page(viewport={"width": 660, "height": 480}, device_scale_factor=scale)
            await page.goto((DMG / "background.html").as_uri())
            await page.evaluate("document.fonts.ready.then(() => true)")
            await page.screenshot(path=str(DMG / name))
            await page.close()
            print(DMG / name)
        await browser.close()


asyncio.run(main())
