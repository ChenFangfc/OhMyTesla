"""Fleet Telemetry alerts over MQTT reach the Sentry pipeline exactly once.

The dispatcher publishes {base}/{VIN}/alerts/{Name}/current and …/history,
with the alert name only in the topic and the car's own StartedAt in the
payload. An alert replayed after a reconnect is old by that clock and must
not alert; the same alert republished when it ends must not alert twice.

Run:  python3 tests/test_fleet_alerts.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="omt-fleet-")   # never the real /data

from app.livestate import LiveState  # noqa: E402

VIN = "7G2CEHED0RA000000"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))


def stamp(ago_s: float) -> str:
    return datetime.fromtimestamp(time.time() - ago_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def msg(suffix: str, payload) -> SimpleNamespace:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(topic=f"telemetry/{VIN}/{suffix}", payload=body.encode())


async def main():
    live = LiveState()
    got: list[tuple[str, str]] = []

    async def record(vin, key, state, force=False):
        got.append((key, state))

    live._ingest_fleet_field = record
    alarm = "VCSEC_a133_alarmTriggered"
    pull = "VCSEC_a211_handlePullWithoutAuth"

    started = stamp(5)
    await live._handle_fleet(msg(f"alerts/{alarm}/current", {"StartedAt": started, "Audiences": ["Service"]}))
    check("a fresh alarm reaches Sentry", got == [("fleet_alert", alarm)], str(got))

    got.clear()
    await live._handle_fleet(msg(f"alerts/{alarm}/history", [{"StartedAt": started}]))
    check("the history topic doesn't repeat it", not got, str(got))

    await live._handle_fleet(msg(f"alerts/{alarm}/current", {"StartedAt": started, "EndedAt": stamp(1)}))
    check("the same alarm ending doesn't alert again", not got, str(got))

    await live._handle_fleet(msg(f"alerts/{pull}/current", {"StartedAt": stamp(3600)}))
    check("an hour-old replayed handle pull stays quiet", not got, str(got))

    await live._handle_fleet(msg(f"alerts/{pull}/current", {"StartedAt": stamp(2)}))
    check("a fresh handle pull reaches Sentry", got == [("fleet_alert", pull)], str(got))

    got.clear()
    await live._handle_fleet(msg("alerts/BMS_a066_w_some_other_alert/current", {"StartedAt": stamp(1)}))
    check("unrelated alerts are ignored", not got, str(got))

    # The car was out of signal (garage, jammer, tow) and reports late: an
    # alarm is never dropped for being old, it's delivered marked as late.
    got.clear()
    await live._handle_fleet(msg(f"alerts/{alarm}/current", {"StartedAt": stamp(240)}))
    check("an alarm reported 4 min late, still sounding → delivered, marked late",
          got == [("fleet_alert", f"{alarm}|late=240")] or (len(got) == 1 and got[0][1].startswith(f"{alarm}|late=")),
          str(got))
    got.clear()
    await live._handle_fleet(msg(f"alerts/{alarm}/current", {"StartedAt": stamp(1800), "EndedAt": stamp(1700)}))
    check("an alarm that went off half an hour ago (ended) → still delivered late",
          len(got) == 1 and "|late=" in got[0][1], str(got))
    got.clear()
    await live._handle_fleet(msg(f"alerts/{alarm}/current", {"StartedAt": stamp(9 * 3600), "EndedAt": stamp(9 * 3600 - 60)}))
    check("an alarm long over (9 h) is not pushed now", not got, str(got))

    # Right after a reconnect, Sentry Panic is not held back by the replay gate.
    sentry = []

    async def fake_sentry(vin, state):
        sentry.append(state)

    live.ingest_fleet_sentry = fake_sentry
    live._fleet_reconnect[VIN] = time.time()
    await live._handle_fleet(SimpleNamespace(topic=f"telemetry/{VIN}/v/SentryMode",
                                             payload=b'"SentryModeStatePanic"'))
    await live._handle_fleet(SimpleNamespace(topic=f"telemetry/{VIN}/v/SentryMode",
                                             payload=b'"SentryModeStateAware"'))
    check("Panic right after a reconnect gets through; a replayed Aware doesn't",
          sentry == ["Panic"], str(sentry))

    # TeslaMate's heartbeat (what the "data stopped" watchdog reads) moves only
    # on live TeslaMate messages — not on the fast poll, Fleet data or replays.
    hb = LiveState()
    hb.ingest("1", "locked", "true", source="fastpoll")
    hb.ingest("1", "odometer", "1000", replay=True)
    check("fast poll and retained replays don't count as TeslaMate alive", "1" not in hb.last_update,
          str(hb.last_update))
    hb.ingest("1", "healthy", "true")
    check("a live TeslaMate message does", "1" in hb.last_update)
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "alerts", "fastpoll.py")).read()
    check("the fast poller tags what it ingests", 'source="fastpoll"' in src)

    # Two hubs on one broker must not share a client id: the broker hands the
    # session to the newcomer and kicks the other off (live: production was
    # knocked off ~8 times a minute by a dev copy).
    import subprocess

    import app.livestate as L

    other = subprocess.run([sys.executable, "-c", "import app.livestate as L; print(L._MQTT_CLIENT_ID)"],
                           capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(L.__file__)),
                           env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(L.__file__))}).stdout.strip()
    check("each hub process has its own MQTT client id",
          L._MQTT_CLIENT_ID.startswith("ohmytesla-hub-") and other and other != L._MQTT_CLIENT_ID,
          f"{L._MQTT_CLIENT_ID} vs {other!r}")

    print()
    print("全部通过" if all(results) else f"{results.count(False)} 项失败")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
