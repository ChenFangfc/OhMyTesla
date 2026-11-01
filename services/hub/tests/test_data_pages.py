"""数据 hub routes: six chapters, one period, and the old pages folded in.

What must hold:
- every chapter renders its own template with active="data" (the top bar
  lights 数据) and marks itself in the chapter index;
- the period travels: ?days=30 selects 30天 and every chapter link carries
  it; a bogus value is ignored, days=0 means all;
- /energy, /trips, /timeline (and the older /insights, /drives) answer 301
  and keep their query strings, so ?drive= / ?charge= deep links still land;
- the hub sits behind the portal password like every other page.

Nothing here touches TeslaMate: the pages render a shell and fetch their data
in the browser, and the app's lifespan (MQTT, pollers) is never started.

Run:  python3 tests/test_data_pages.py
"""

import asyncio
import base64
import os
import sys
import tempfile

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="omt-datapages-")   # never the real /data
os.environ["OHMYTESLA_PASSWORD"] = "test-pw"  # noqa: S105 - a throwaway test secret

try:
    import httpx
    from app import main
except ImportError as e:
    print(f"⏭  skipped: needs the hub runtime ({e})")
    sys.exit(0)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))


AUTH = {"Authorization": "Basic " + base64.b64encode(b"ohmytesla:test-pw").decode()}
CHAPTERS = [
    ("/data", "data.html", "overview"),
    ("/data/driving", "data_driving.html", "driving"),
    ("/data/charging", "data_charging.html", "charging"),
    ("/data/battery", "data_battery.html", "battery"),
    ("/data/footprint", "data_footprint.html", "footprint"),
    ("/data/timeline", "data_timeline.html", "timeline"),
]

# Record what each page hands to its template.
rendered: list[tuple[str, dict]] = []
_orig = main.templates.TemplateResponse


def _spy(request, name, context=None, *a, **kw):
    rendered.append((name, dict(context or {})))
    return _orig(request, name, context, *a, **kw)


main.templates.TemplateResponse = _spy


async def run():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://hub", headers=AUTH) as c:
        print("chapters")
        for path, tpl, chapter in CHAPTERS:
            rendered.clear()
            r = await c.get(path)
            check(f"{path} answers 200", r.status_code == 200, r.status_code)
            name, ctx = rendered[-1] if rendered else ("", {})
            check(f"{path} renders {tpl}", name == tpl, name)
            check(f"{path} lights 数据 in the top bar (active=data)", ctx.get("active") == "data", ctx.get("active"))
            check(f"{path} knows its chapter", ctx.get("chapter") == chapter, ctx.get("chapter"))
            check(f"{path} marks itself in the chapter index",
                  f'href="{path}"' in r.text and r.text.count('aria-current="page"') >= 1
                  and f'href="{path}" aria-current="page"' in r.text, path)
            check(f"{path} loads the hub's own stylesheet and script",
                  "/static/css/data.css" in r.text and "/static/js/pages/data.js" in r.text)

        print("one period for the whole hub")
        rendered.clear()
        r = await c.get("/data/charging?days=30")
        ctx = rendered[-1][1] if rendered else {}
        check("?days=30 is passed to the page", ctx.get("days") == "30" and ctx.get("period_qs") == "?days=30", ctx.get("days"))
        check("every chapter link carries ?days=30",
              all(f'href="{p}?days=30"' in r.text for p, _, _ in CHAPTERS))
        check("30天 is the pressed period", 'data-days="30" aria-pressed="true"' in r.text)
        rendered.clear()
        await c.get("/data?days=0")
        check("days=0 means all time", (rendered[-1][1] if rendered else {}).get("days") == "all")
        rendered.clear()
        r = await c.get("/data/footprint?days=13")
        ctx = rendered[-1][1] if rendered else {}
        check("an unknown period is ignored, not echoed", ctx.get("days") is None and ctx.get("period_qs") == "", ctx.get("days"))
        check("…and the links stay clean", 'href="/data/driving"' in r.text)

        print("old pages fold into the hub")
        for old, new in [("/energy", "/data/charging"), ("/trips", "/data/driving"), ("/timeline", "/data/timeline"),
                         ("/insights", "/data"), ("/drives", "/data/driving")]:
            r = await c.get(old, follow_redirects=False)
            check(f"{old} → 301 {new}", r.status_code == 301 and r.headers.get("location") == new,
                  f"{r.status_code} {r.headers.get('location')}")
        r = await c.get("/trips?drive=12", follow_redirects=False)
        check("/trips?drive=12 keeps the deep link", r.headers.get("location") == "/data/driving?drive=12", r.headers.get("location"))
        r = await c.get("/energy?charge=5&days=all", follow_redirects=False)
        check("/energy?charge=5 keeps the deep link and the period",
              r.headers.get("location") == "/data/charging?charge=5&days=all", r.headers.get("location"))
        r = await c.get("/trips?drive=12", follow_redirects=True)
        check("…and the redirect lands on a page", r.status_code == 200 and str(r.url).endswith("/data/driving?drive=12"), str(r.url))

    print("behind the portal password")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://hub") as anon:
        r = await anon.get("/data")
        check("no password → 401", r.status_code == 401, r.status_code)
        r = await anon.get("/trips", follow_redirects=False)
        check("old URLs too (no redirect before auth)", r.status_code == 401, r.status_code)


asyncio.run(run())
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
