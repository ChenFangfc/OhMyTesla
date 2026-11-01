"""MCP send_command: the right car, named, and a confirmation that can't move.

An AI client sends commands through /mcp. It must default to the car that is
actually in use (not the first key of the live map, which on a multi-car
account can be a car that left the account), say which car it is about to
act on, refuse archived cars, and bind a confirmation to that car.
The Tesla call itself is stubbed — nothing reaches a car.

Run:  python3 tests/test_mcp_write.py
"""

import asyncio
import os
import sys
import tempfile

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="omt-mcpw-")   # never the real /data
os.environ["MCP_WRITE"] = "true"

from app.alerts.store import store  # noqa: E402
from app.insights import cars as registry  # noqa: E402
from app.mcp import write_tools as W  # noqa: E402
from app.tesla import commands as cmd  # noqa: E402

CARS = {1: {"id": 1, "label": "Cybertruck", "archived": False},
        3: {"id": 3, "label": "Model Y", "archived": True}}
SENT: list[tuple[int, str]] = []
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))


async def default_car_id():
    return 1


async def get_car(car_id):
    return CARS.get(car_id)


WAKE: list[bool] = []


async def fake_send(car_id, command, params, *, allow_wake=True):
    SENT.append((car_id, command))
    WAKE.append(allow_wake)
    return {"ok": True}


async def main():
    await store.open()
    registry.default_car_id = default_car_id
    registry.get_car = get_car
    cmd.send = fake_send

    r = await W.t_send_command("flash_lights")
    check("defaults to the active car and names it", r.get("ok") and r.get("car") == "Cybertruck"
          and SENT == [(1, "flash_lights")], r)
    check("says what it did, with the car", str(r.get("did", "")).startswith("Cybertruck"), r.get("did"))
    check("the AI never wakes a sleeping car to run a command", WAKE == [False], str(WAKE))

    SENT.clear()
    r = await W.t_send_command("flash_lights", car_id=3)
    check("refuses an archived car", "error" in r and not SENT, r)
    r = await W.t_send_command("flash_lights", car_id=99)
    check("refuses an unknown car", "error" in r and not SENT, r)

    r = await W.t_send_command("door_unlock")
    check("a risky command asks first, naming the car", r.get("needs_confirmation")
          and r.get("what_will_happen", "").startswith("Cybertruck") and not SENT, r)
    tok = r.get("confirm_token", "")
    CARS[2] = {"id": 2, "label": "Cyberbeast", "archived": False}
    r2 = await W.t_send_command("door_unlock", car_id=2, confirm_token=tok)
    check("the confirmation can't be used on another car", "error" in r2 and not SENT, r2)
    r = await W.t_send_command("door_unlock")
    r3 = await W.t_send_command("door_unlock", confirm_token=r["confirm_token"])
    check("…and works on the car it was given for", r3.get("ok") and SENT == [(1, "door_unlock")], r3)

    r = await W.t_send_command("remote_start")
    check("the highest-risk commands stay off-limits", "error" in r and "remote_start" in r["error"], r)

    r = await W.t_snooze_alerts(1440, "all")
    check("the AI can't mute alarms (scope=all)", "error" in r, r)
    r = await W.t_snooze_alerts(60, "noncritical")
    check("…but can quiet the routine ones for a while", r.get("ok") is True, r)
    await W.t_snooze_alerts(0, "noncritical")

    await store.close()
    print()
    print("全部通过" if all(results) else f"{results.count(False)} 项失败")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
