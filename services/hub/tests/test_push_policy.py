"""Push policy: only Sentry and major events reach the phone, and briefly.

The owner's rule: a push must say what happened at a glance, and routine
diagnostics (data resumed, updates, maintenance, logger health, low battery…)
stay in the app. This runs the real AlertEngine with the network send stubbed.

Run:  python3 tests/test_push_policy.py
"""

import asyncio
import json
import os
import sys
import tempfile

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="omt-push-")   # never the real /data
os.environ["ALERT_QUIET_HOURS"] = ""

from app.alerts import engine as E  # noqa: E402
from app.alerts import notifier  # noqa: E402
from app.alerts import sentry as S  # noqa: E402
from app.alerts.store import store  # noqa: E402
from app.livestate import live  # noqa: E402

SENT: list[tuple[str, str]] = []
URGENT: dict[str, bool] = {}        # title -> sent time-sensitive?


async def fake_send(title, body, payload=None, attach=None, *, level=None, url=None, only=None):
    SENT.append((title, body))
    URGENT[title] = bool((payload or {}).get("urgent"))
    return True, ["ntfy"]


notifier.send = fake_send


async def label(_cid):
    return "Cybertruck"

E.car_label = label
S.car_label = label

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))


async def main():
    await store.open()
    from app.i18n import prefs as P

    await P.set_prefs({"locale": "en", "length": "mi", "temp": "F"})
    eng = E.engine
    eng._enabled_cache = None
    live.cars["1"] = {"model": "Cybertruck", "latitude": "40.1", "longitude": "-98.2", "battery_level": "70", "plugged_in": "true"}

    # routine diagnostics: recorded, never pushed
    SENT.clear()
    ok = await eng.fire_custom("1", "info", "✅ x", "", rule="data_resumed")
    check("data resumed is recorded but not pushed", ok and not SENT, str(SENT))
    SENT.clear()
    await eng._fire("update_available", "1", extra="2026.40.1")
    await eng._fire("low_battery", "1", extra="18%")
    await eng._fire("teslamate_unhealthy", "1")
    check("update / low battery / logger health don't push", not SENT, str(SENT))
    SENT.clear()
    await eng.fire_custom("1", "info", "maint", "", rule="maintenance_due")
    await eng.fire_custom("1", "warning", "clip", "", rule="new_sentry_clip")
    check("maintenance and synced clips don't push", not SENT, str(SENT))

    # major events push, with a short specific title
    SENT.clear()
    from app.i18n.router import title_case

    await eng._fire("tpms_warning", "1", extra=title_case("Front left", "en"))   # as the MQTT handler passes it
    check("low tire pressure pushes", len(SENT) == 1, str(SENT))
    title, body = SENT[0] if SENT else ("", "")
    check("title names the event in one line", title == "⚠️ Cybertruck — Low Tire Pressure · Front Left", title)
    check("body is one short line (place · time)", "\n" not in body and len(body) <= 40 and "http" not in body, repr(body))

    SENT.clear()
    await eng._fire("charging_interrupted", "1")
    check("charging losing power pushes", SENT and SENT[0][0] == "⚠️ Cybertruck — Charging Lost Power", str(SENT))

    SENT.clear()
    await eng.fire_custom("1", "critical", "🔇 Cybertruck — Sentry alerts paused", "No data for 45 min — check the server",
                          rule="data_stale")
    check("Sentry alerts paused pushes", len(SENT) == 1, str(SENT))

    # sentry: one short push stating the event
    SENT.clear()
    t = S.SentryService()
    t.bind(eng.fire_custom)
    await t._signal("1", "Cybertruck", "state", "panic", "sentry.reason.forced_door")
    check("forced door → one push", len(SENT) == 1, str(SENT))
    if SENT:
        check("sentry title: '🚨 Cybertruck — Door Opened While Locked'",
              SENT[0][0] == "🚨 Cybertruck — Door Opened While Locked", SENT[0][0])
        check("sentry body is one short line", "\n" not in SENT[0][1] and len(SENT[0][1]) <= 40, repr(SENT[0][1]))
    check("someone at the car is sent time-sensitive; a tire warning isn't",
          URGENT.get("🚨 Cybertruck — Door Opened While Locked") is True
          and URGENT.get("⚠️ Cybertruck — Low Tire Pressure · Front Left") is False, str(URGENT))

    # what time-sensitive means on each channel; the level still sets the wording
    import httpx

    seen = []

    def handler(request):
        seen.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"code": 200})

    real_client = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler), **kw)
    try:
        ntfy = {"url": "https://ntfy.sh", "topic": "t"}
        await notifier._ntfy(ntfy, "👀 x", "y", "info", None, None, urgent=True)
        await notifier._ntfy(ntfy, "x", "y", "info", None, None)
        await notifier._ntfy(ntfy, "🚨 x", "y", "critical", None, None, urgent=True)
        await notifier._bark({"key": "k"}, "👀 x", "y", "info", None, urgent=True)
        await notifier._bark({"key": "k"}, "x", "y", "info", None)
    finally:
        httpx.AsyncClient = real_client
    check("ntfy: someone at the car → priority 4 (time-sensitive on iPhone); alarm 5; routine 3",
          [s.get("priority") for s in seen[:3]] == [4, 3, 5], str(seen[:3]))
    check("Bark: someone at the car → timeSensitive; routine → default",
          [s.get("level") for s in seen[3:5]] == ["timeSensitive", None], str(seen[3:5]))
    check("ntfy: a title with its own emoji gets no emoji tag (the iPhone would show two); a bare one gets one",
          [s.get("tags") for s in seen[:3]] == [[], ["information_source"], []], str([s.get("tags") for s in seen[:3]]))
    check("our icon goes to ntfy (shown on Android) and Bark (iOS 15+)",
          all(s.get("icon") == notifier._ICON_URL for s in seen[:5]), str([s.get("icon") for s in seen[:5]]))
    en = json.loads(open(os.path.join(os.path.dirname(notifier.__file__), "..", "i18n", "locales", "en", "push.json"),
                         encoding="utf-8").read())
    from app.i18n.router import MINOR_WORDS

    events = [v.split(" — ", 1)[1] if " — " in v else v for k, v in en.items()
              if k.startswith("sentry.short.") or (k.startswith("push.") and " — " in v)]
    # Title Case up to the first " · "; what follows is a measurement ("5 min ago").
    def untitled(e):
        words = e.split(" · ")[0].replace(",", "").split()
        return [w for i, w in enumerate(words)
                if w[:1].isalpha() and w[:1].islower() and (i == 0 or w not in MINOR_WORDS)
                and not (i and words[i - 1][:1] in "{0123456789")]     # a unit after a number

    lower = [e for e in events if untitled(e)]
    check("English push titles are in Title Case ('Someone Came Close')", not lower, str(lower))
    from app.alerts.channels import bark_address

    check("Bark: the address the app shows is split into server and key",
          bark_address({"key": "https://api.day.app/AbC123xyz/Push%20content"})
          == {"key": "AbC123xyz", "server": "https://api.day.app"}
          and bark_address({"key": "AbC123xyz"}) == {"key": "AbC123xyz"}, "")
    from app.alerts import webpush

    check("Web Push: someone at the car → Urgency high; routine info stays normal",
          (webpush.urgency("info", True), webpush.urgency("info")) == ("high", "normal"))

    # the owner can still turn one into a push, or off
    await eng.set_mode("low_battery", "push")
    SENT.clear()
    eng._last_fired.clear()
    await eng._fire("low_battery", "1", extra="18%")
    check("a rule switched to Push pushes", SENT and SENT[0][0] == "🪫 Cybertruck — Battery at 18%", str(SENT))
    await eng.set_mode("tpms_warning", "off")
    SENT.clear()
    eng._last_fired.clear()
    ok = await eng._fire("tpms_warning", "1", extra="rear right")
    check("a rule switched Off neither pushes nor records", not ok and not SENT, str(SENT))

    # an old on/off choice: "off" is honoured, "on" no longer forces routine pushes
    await store.set_setting("alert_modes", {})
    await store.set_setting("alert_rules", {"tpms_warning": False, "update_available": True})
    eng._enabled_cache = None
    modes = await eng.rule_modes()
    check("legacy 'off' stays off", modes["tpms_warning"] == "off", modes["tpms_warning"])
    check("legacy 'on' keeps a routine rule in-app", modes["update_available"] == "app", modes["update_available"])

    # a charge that stops short is judged after a grace period, against the
    # car's learned schedule, and never pushed
    from app.insights import charging_stops as CS

    fired = []
    real_fire = eng._fire

    async def spy(rule_id, car_id, *a, **k):
        fired.append(rule_id)
        return True

    eng._fire = spy
    eng.STOP_GRACE_S = 0.05
    verdict = {"interrupted": False, "reason": "scheduled_stop"}

    async def fake_classify(*a, **k):
        return dict(verdict)

    CS.classify_stop = fake_classify
    car = {"model": "Cybertruck", "plugged_in": "true", "battery_level": "60", "charge_limit_soc": "80",
           "charging_state": "Stopped"}
    live.cars["1"] = dict(car)
    await eng.handle("1", "charging_state", "Charging", "Stopped")
    check("stop is not judged on the spot", not fired, str(fired))
    await asyncio.sleep(0.2)
    check("a scheduled stop (end of off-peak) is not recorded", not fired, str(fired))
    verdict = {"interrupted": True, "reason": "unexplained"}
    await eng.handle("1", "charging_state", "Charging", "Stopped")
    await asyncio.sleep(0.2)
    check("an unexplained stop is recorded (in-app)", fired == ["charging_stopped_early"], str(fired))
    fired.clear()
    await eng.handle("1", "charging_state", "Charging", "Stopped")
    live.cars["1"]["plugged_in"] = "false"            # owner unplugged within the grace period
    await asyncio.sleep(0.2)
    check("unplugging right after stopping is not an interruption", not fired, str(fired))
    check("stopped early is in-app only by default", (await eng.rule_modes())["charging_stopped_early"] != "push")
    eng._fire = real_fire

    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()
    await store.close()
    print()
    print("全部通过" if all(results) else f"{results.count(False)} 项失败")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
