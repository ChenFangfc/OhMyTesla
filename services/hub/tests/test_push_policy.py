"""Push policy: only Sentry and major events reach the phone, and briefly.

The owner's rule: a push must say what happened at a glance, and routine
diagnostics (data resumed, updates, maintenance, logger health, low battery…)
stay in the app. This runs the real AlertEngine with the network send stubbed.

Run:  python3 tests/test_push_policy.py
"""

import asyncio
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


async def fake_send(title, body, payload=None, attach=None, *, level=None, url=None, only=None):
    SENT.append((title, body))
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
    await eng._fire("tpms_warning", "1", extra="front left")
    check("low tire pressure pushes", len(SENT) == 1, str(SENT))
    title, body = SENT[0] if SENT else ("", "")
    check("title names the event in one line", title == "⚠️ Cybertruck — low tire pressure · front left", title)
    check("body is one short line (place · time)", "\n" not in body and len(body) <= 40 and "http" not in body, repr(body))

    SENT.clear()
    await eng._fire("charging_interrupted", "1")
    check("charging losing power pushes", SENT and SENT[0][0] == "⚠️ Cybertruck — charging lost power", str(SENT))

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
        check("sentry title: '🚨 Cybertruck — door opened while locked'",
              SENT[0][0] == "🚨 Cybertruck — door opened while locked", SENT[0][0])
        check("sentry body is one short line", "\n" not in SENT[0][1] and len(SENT[0][1]) <= 40, repr(SENT[0][1]))

    # the owner can still turn one into a push, or off
    await eng.set_mode("low_battery", "push")
    SENT.clear()
    eng._last_fired.clear()
    await eng._fire("low_battery", "1", extra="18%")
    check("a rule switched to Push pushes", SENT and SENT[0][0] == "🪫 Cybertruck — battery at 18%", str(SENT))
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
