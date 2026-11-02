"""Sentry v2: one decision per event, made on the settled state.

Each case feeds values through the real LiveState → AlertEngine → SentryService
path (with a throwaway SQLite store) and records what would have been pushed.
The cases come from the live false alarms an audit measured — 29 critical
"broken into" pushes from 18 owner returns — and from the real threats that
must still get through.

Run:  python3 tests/test_sentry_v2.py   (needs the hub's Python deps)
"""

import asyncio
import os
import random
import sys
import tempfile
import time

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="omt-sentry-")   # never the real /data
os.environ["NTFY_TOPIC"] = ""

from app.alerts import sentry as S  # noqa: E402
from app.alerts.engine import engine  # noqa: E402
from app.alerts.store import store  # noqa: E402
from app.livestate import LiveState  # noqa: E402
import app.livestate as livestate_mod  # noqa: E402

PUSHED: list[tuple[str, str, str]] = []     # (level, gate, first body line)


async def fake_fire(car_id, level, title, body, rule=None, gate=None, **kw):
    PUSHED.append((level, gate, (body.splitlines() or [""])[0]))
    return True


S.SETTLE_S = 0.05
S.SETTLE_MAX_S = 0.3


class Fresh:
    """A new LiveState + SentryService per case, wired like main.py does."""

    def __init__(self):
        self.live = LiveState()
        livestate_mod.live = self.live
        import app.alerts.engine as eng
        import app.alerts.sentry as sen
        eng.live = self.live
        self.tracker = sen.SentryService()
        eng.sentry_tracker = self.tracker
        sen.tracker = self.tracker
        self.tracker.bind(fake_fire)

        async def label(_cid):
            return "Cybertruck"
        sen.car_label = label
        eng.car_label = label
        self.live.add_transition_hook(engine.handle)
        self.live.add_snapshot_hook(self.tracker.flush)
        PUSHED.clear()

    async def baseline(self, car="1", **values):
        """State as the hub would know it before the scene starts (no transitions)."""
        for k, v in values.items():
            self.live.ingest(car, k, v, replay=True)
        self.live.live_seen[car] = 1.0

    async def feed(self, pairs, car="1", shuffle=False, gap=0.0):
        pairs = list(pairs)
        if shuffle:
            random.shuffle(pairs)
        for k, v in pairs:
            self.live.ingest(car, k, v)
            if gap:
                await asyncio.sleep(gap)

    async def settle(self, t=1.2):
        await asyncio.sleep(t)


ARMED = dict(model="Cybertruck", sentry_mode="true", locked="true", is_user_present="false", state="online",
             shift_state="P", doors_open="false", trunk_open="false", frunk_open="false",
             windows_open="false", center_display_state="0", latitude="40.100000",
             longitude="-98.200000", geofence="", battery_level="70")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))


async def main():
    await store.open()

    # 1. Owner returns with phone key; TeslaMate publishes the batch in any order.
    for trial in range(20):
        f = Fresh()
        await f.baseline(**ARMED)
        await f.feed([("locked", "false"), ("is_user_present", "true"), ("doors_open", "true"),
                      ("sentry_mode", "false")], shuffle=True)
        await f.settle()
        if PUSHED:
            break
    check("owner unlock + door, any message order (20 shuffles) → no push", not PUSHED, str(PUSHED))

    # 2. Owner unlocks, opens the door, but hasn't sat down and Sentry still reads on
    #    (the 7/18 live cases the old 2-second re-check could not explain).
    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("locked", "false")])
    await f.settle()
    await f.feed([("doors_open", "true")])
    await f.settle()
    check("unlocked by key, then door opened, no one seated → no alarm",
          not any(lv == "critical" for lv, _, _ in PUSHED), str(PUSHED))

    # 3. Door opens while the car still reports locked → forced entry → one critical push.
    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("doors_open", "true"), ("trunk_open", "true")])
    await f.settle()
    crit = [p for p in PUSHED if p[0] == "critical"]
    check("door + trunk open while locked → exactly one critical push", len(crit) == 1, str(PUSHED))

    # 4. TeslaMate 4.4 empty payload: sentry_mode blinks "" and then a door is forced.
    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("sentry_mode", "")])
    await f.feed([("doors_open", "true")])
    await f.settle()
    check("sentry_mode \"\" does not blind the alarm", any(p[0] == "critical" for p in PUSHED), str(PUSHED))

    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("trunk_open", "")])
    await f.feed([("trunk_open", "false")])
    await f.settle()
    check("a field blinking \"\" and back is not an event", not PUSHED, str(PUSHED))

    # 5. Sentry screen lights up (polled) away from home → aware push; at home → logged only.
    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("center_display_state", "7")])
    await f.settle()
    check("display 7 away from home → one aware push",
          [p[0] for p in PUSHED] == ["info"], str(PUSHED))

    f = Fresh()
    await f.tracker.set_home(40.1, -98.2, 150)
    await f.baseline(**ARMED)
    await f.feed([("center_display_state", "7")])
    await f.settle()
    check("display 7 at home → logged, not pushed (default policy)", not PUSHED, str(PUSHED))
    await store.set_setting("home_location", None)

    # 6. Owner sits in the car right after the screen lit (same burst) → nothing.
    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("center_display_state", "7"), ("is_user_present", "true")], shuffle=True)
    await f.settle()
    check("display 7 with the owner seated in the same burst → no push", not PUSHED, str(PUSHED))

    # 7. Towing: the parked, locked car moves 300 m with no one aboard.
    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("latitude", "40.102700")])           # ≈300 m north of where it was parked
    await f.settle()
    check("one 300 m GPS jump alone is not a tow", not PUSHED, str(PUSHED))
    await f.feed([("latitude", "40.104500")])           # still moving (≈500 m)
    await f.settle()
    check("parked car keeps moving, nobody aboard → critical (tow)",
          [p[0] for p in PUSHED] == ["critical"], str(PUSHED))

    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("shift_state", "D"), ("latitude", "40.104500")])
    await f.settle()
    await f.feed([("latitude", "40.110000")])
    await f.settle()
    check("owner drives away (gear D) → no tow alarm", not PUSHED, str(PUSHED))

    # 8. Retained replay after a reconnect is a snapshot, not an event.
    f = Fresh()
    await f.baseline(**ARMED)
    f.live.ingest("1", "doors_open", "true", replay=True)
    await f.settle()
    check("retained replay of doors_open=true → no push", not PUSHED, str(PUSHED))

    # 9. Fleet Telemetry: Aware then Panic → warning-level first, critical on escalation.
    f = Fresh()
    await f.baseline(**ARMED)
    await engine.handle("1", "fleet_sentry_state", "Armed", "Aware")
    await engine.handle("1", "fleet_sentry_state", "Aware", "Panic")
    levels = [p[0] for p in PUSHED]
    check("Fleet Aware → Panic → aware push then one critical", levels == ["info", "critical"], str(PUSHED))

    # 10. Tesla's alarm alert always pushes, even at home.
    f = Fresh()
    await f.tracker.set_home(40.1, -98.2, 150)
    await f.baseline(**ARMED)
    await engine.handle("1", "fleet_alert", None, "VCSEC_a133_alarmTriggered")
    check("alarmTriggered at home → critical push", [p[0] for p in PUSHED] == ["critical"], str(PUSHED))
    await store.set_setting("home_location", None)

    # 11. A hub-sent command explains the next openings.
    f = Fresh()
    await f.baseline(**ARMED)
    await store.log_command("1", "frunk", None, "api", True, "", 900)
    await f.feed([("frunk_open", "true")])
    await f.settle()
    check("frunk opened right after this hub sent 'frunk' → no alarm",
          not any(p[0] == "critical" for p in PUSHED), str(PUSHED))

    async def clear_commands():
        await store.db.execute("DELETE FROM command_log")
        await store.db.commit()

    # 12. Only a command that could cause the opening explains it: a honk doesn't.
    await clear_commands()
    f = Fresh()
    await f.baseline(**ARMED)
    await store.log_command("1", "honk_horn", None, "api", True, "", 600)
    await f.feed([("doors_open", "true")])
    await f.settle()
    check("a honk from this hub doesn't hide a forced door", [p[0] for p in PUSHED] == ["critical"], str(PUSHED))

    # 13. A frunk command that timed out may still have opened it: not a break-in.
    await clear_commands()
    f = Fresh()
    await f.baseline(**ARMED)
    await store.log_command("1", "frunk", None, "api", False, "timeout: no answer from the car", 30000)
    await f.feed([("frunk_open", "true")])
    await f.settle()
    check("frunk opens after this hub's frunk command timed out → no push", not PUSHED, str(PUSHED))
    await clear_commands()

    # 14. Sentry OFF is not "the owner is here": a locked car is still guarded.
    off = {**ARMED, "sentry_mode": "false"}
    f = Fresh()
    await f.baseline(**off)
    await f.feed([("doors_open", "true")])
    await f.settle()
    check("Sentry off, door opens on the locked car → critical", [p[0] for p in PUSHED] == ["critical"], str(PUSHED))

    f = Fresh()
    await f.baseline(**off)
    await f.feed([("latitude", "40.102700")])
    await f.settle()
    await f.feed([("latitude", "40.104500")])
    await f.settle()
    check("Sentry off, locked car towed → critical", [p[0] for p in PUSHED] == ["critical"], str(PUSHED))

    f = Fresh()
    await f.baseline(**off)
    await engine.handle("1", "fleet_display_state", "Off", "Lock")
    check("Sentry off, someone touches the locked car (Fleet Lock) → pushed", len(PUSHED) == 1, str(PUSHED))

    # 15. …but switching Sentry off *is* an owner sign for the next two minutes.
    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("sentry_mode", "false")])
    await f.settle()
    await f.feed([("doors_open", "true")])
    await f.settle()
    check("door opens right after the owner switched Sentry off → no alarm", not PUSHED, str(PUSHED))

    # 16. The Tesla app opens the trunk remotely and the car stays locked: worth a look, no alarm.
    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("trunk_open", "true")])
    await f.settle()
    check("trunk opens on a locked car → one 👀 push, not critical",
          [p[0] for p in PUSHED] == ["info"], str(PUSHED))

    # 17. Windows vented from the Tesla app on a locked car: recorded, not pushed.
    f = Fresh()
    await f.baseline(**ARMED)
    await f.feed([("windows_open", "true")])
    await f.settle()
    modes = await engine.rule_modes()
    pushed = [g for _, g, _ in PUSHED if modes.get(g or "", "push") == "push"]
    check("windows open on a locked car → recorded in-app only, never pushed",
          not pushed and [g for _, g, _ in PUSHED] == ["sentry_window_opened"], str(PUSHED))

    # 18. At home the default policy still pushes when activity persists.
    real_escalate = S.settings.sentry_escalate_seconds
    S.settings.sentry_escalate_seconds = 0.5
    try:
        f = Fresh()
        await f.tracker.set_home(40.1, -98.2, 150)
        await f.baseline(**ARMED)
        await f.feed([("center_display_state", "7")])
        await f.settle()
        first = list(PUSHED)
        await f.feed([("center_display_state", "0")])
        await f.feed([("center_display_state", "7")])
        await f.settle()
        check("at home: a glance isn't pushed, lingering is (⚠️)",
              not first and [p[0] for p in PUSHED] == ["warning"], f"{first} → {PUSHED}")
    finally:
        S.settings.sentry_escalate_seconds = real_escalate
        await store.set_setting("home_location", None)

    # 19. A fast-poll reading arrives whole: decided at once. MQTT still waits for quiet.
    real_settle, real_max = S.SETTLE_S, S.SETTLE_MAX_S
    S.SETTLE_S, S.SETTLE_MAX_S = 5.0, 8.0
    try:
        f = Fresh()
        await f.baseline(**ARMED)
        t0 = time.monotonic()
        await f.feed([("center_display_state", "7")])
        f.live.end_snapshot("1")
        for _ in range(50):
            if PUSHED:
                break
            await asyncio.sleep(0.02)
        took = time.monotonic() - t0
        check("fast-poll reading → pushed at once, not after the 5 s quiet period",
              [p[0] for p in PUSHED] == ["info"] and took < 1.0, f"{took:.2f}s {PUSHED}")

        f = Fresh()
        await f.baseline(**ARMED)
        await f.feed([("center_display_state", "7")])
        await asyncio.sleep(0.5)
        check("an MQTT burst still waits for quiet before deciding", not PUSHED, str(PUSHED))
        p = f.tracker._pending.pop("1", None)
        if p and p.timer:
            p.timer.cancel()
    finally:
        S.SETTLE_S, S.SETTLE_MAX_S = real_settle, real_max

    # 20. One reading carries the lit screen and the owner's key unlock, in either
    #     order: that's the owner walking up, not someone at the car.
    for order in ([("center_display_state", "7"), ("locked", "false")],
                  [("locked", "false"), ("center_display_state", "7")]):
        f = Fresh()
        await f.baseline(**ARMED)
        await f.feed(order)
        f.live.end_snapshot("1")
        await f.settle()
        aware = [p for p in PUSHED if p[1] == "sentry_aware"]
        check(f"lit screen + key unlock in one reading ({order[0][0]} first) → no 👀 push", not aware, str(PUSHED))

    import httpx
    from app.alerts import fastpoll as FP

    async def token():
        return "tok"

    def vehicle(lit=0, sentry=True):
        return httpx.Response(200, json={"response": {
            "vehicle_state": {"center_display_state": lit, "sentry_mode": sentry, "locked": True,
                              "is_user_present": False},
            "drive_state": {"latitude": 40.1, "longitude": -98.2}}})

    # 21. Fast poll: one kept-alive client across checks; a dropped connection is
    #     retried at once on a fresh one; the reading is decided as soon as it lands.
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 3:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        return vehicle(lit=7 if len(calls) >= 4 else 0)

    real_live, real_token = FP.live, FP.tokens.access_token
    S.SETTLE_S, S.SETTLE_MAX_S = 5.0, 8.0
    fp = FP.FastPoll()
    try:
        f = Fresh()
        await f.baseline(**ARMED)
        FP.live, FP.tokens.access_token = f.live, token
        fp.transport = httpx.MockTransport(handler)
        fp._eid["1"] = 42
        await fp._poll("1", 5)
        first = fp._cli
        await fp._poll("1", 5)
        kept = fp._cli is first
        delay = await fp._poll("1", 5)
        check("fast poll keeps one client across checks", kept and first is not None)
        check("a dropped kept-alive connection is retried at once on a fresh one",
              delay == 5 and fp._cli is not first and fp.stats["1"]["errors"] == 0 and len(calls) == 4,
              f"delay={delay} stats={fp.stats.get('1')} calls={len(calls)}")
        for _ in range(25):
            if PUSHED:
                break
            await asyncio.sleep(0.02)
        check("the lit screen in a fast-poll reading → 👀 push at once", [p[0] for p in PUSHED] == ["info"], str(PUSHED))
    finally:
        await fp.stop()
        FP.live, FP.tokens.access_token = real_live, real_token
        S.SETTLE_S, S.SETTLE_MAX_S = real_settle, real_max

    # 22. The loop: checks while Sentry is on, stops (and lets the connection go) the
    #     moment it goes off, and checks again at once when it comes back on.
    hits = []
    fp = FP.FastPoll()
    try:
        f = Fresh()
        await f.baseline(**ARMED)
        FP.live, FP.tokens.access_token = f.live, token

        def car_now(request):
            hits.append(time.monotonic())
            return vehicle(sentry=f.live.cars["1"].get("sentry_mode") == "true")

        fp.transport = httpx.MockTransport(car_now)
        fp.start_delay = 0
        fp._eid["1"] = 42
        fp._every, fp._every_at = 1, time.monotonic() + 1e6      # every second, never re-read
        f.live.add_transition_hook(fp.on_change)
        fp.start()
        await asyncio.sleep(2.6)
        n_on = len(hits)
        f.live.ingest("1", "sentry_mode", "false")                # the owner switches Sentry off
        await asyncio.sleep(0.3)
        n_off, cli_off = len(hits), fp._cli
        await asyncio.sleep(1.5)
        check("fast poll: checks while Sentry is on, none once it's off, connection closed",
              n_on >= 2 and len(hits) == n_off and cli_off is None, f"on={n_on} off={n_off}→{len(hits)} cli={cli_off}")
        t_on = time.monotonic()
        f.live.ingest("1", "sentry_mode", "true")
        for _ in range(40):
            if len(hits) > n_off:
                break
            await asyncio.sleep(0.05)
        late = hits[n_off] - t_on if len(hits) > n_off else None
        check("fast poll: Sentry back on → checked at once, not after the idle minute",
              late is not None and late < 1.0, f"after {late}")
    finally:
        await fp.stop()
        FP.live, FP.tokens.access_token = real_live, real_token

    # The default home name follows the owner's language; a name they chose doesn't.
    check("home label: 家 / Home follow the language, custom names stay",
          [S.place_label("家", True, "en"), S.place_label("Home", True, "zh"), S.place_label("", True, "en"),
           S.place_label("Garage", True, "zh"), S.place_label("家", False, "en")]
          == ["Home", "家", "Home", "Garage", "家"])

    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()
    await store.close()
    print()
    print("全部通过" if all(results) else f"{results.count(False)} 项失败")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
