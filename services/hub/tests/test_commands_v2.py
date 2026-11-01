"""Vehicle command layer: the catalogue's guarantees, without touching a car.

The rule that matters most: a caller can only influence a command through its
declared parameters. The previous table let `params` overwrite fixed body
fields, so "sentry_on" with {"on": false} silently disabled Sentry without the
confirmation that "sentry_off" requires.

Run:  python3 tests/test_commands_v2.py
"""

import asyncio
import os
import sys
import tempfile

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="omt-cmd-")   # never the real /data
os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-commands")

from app.tesla import catalog, commands  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc as e:
        return str(e)
    return None


async def main():
    # parameters are the only lever
    check("sentry_on rejects an 'on' override",
          raises(lambda: catalog.resolve("sentry_on", {"on": False})) is not None)
    check("close_windows rejects a 'command' override",
          raises(lambda: catalog.resolve("close_windows", {"command": "vent"})) is not None)
    spec, p = catalog.resolve("sentry_on", {})
    check("sentry_on always sends on=True", spec.args(p) == {"on": True})

    # schemas
    check("charge limit below 50% refused", raises(lambda: catalog.resolve("set_charge_limit", {"percent": 30})))
    check("charge limit '80' (string) coerced to 80",
          catalog.resolve("set_charge_limit", {"percent": "80"})[1] == {"percent": 80})
    check("seat heater level must be 0–3",
          raises(lambda: catalog.resolve("seat_heater", {"seat": 0, "level": 5})) is not None)
    spec, p = catalog.resolve("set_temps", {"driver_temp": 21})
    check("set_temps fills the passenger side (protocol treats missing as 0 °C)",
          spec.args(p) == {"driver_temp": 21.0, "passenger_temp": 21.0}, str(spec.args(p)))

    # legacy names still work and map onto the new schema
    spec, p = catalog.resolve("seat_heater_left", {"level": 2})
    check("legacy seat_heater_left → seat_heater seat 0",
          spec.id == "seat_heater" and spec.args(p) == {"seat_position": 0, "seat_heater_level": 2})
    check("legacy REST body keeps the old 'heater' naming",
          spec.body(p) == {"heater": 0, "level": 2})

    # risk levels
    risky = {c.id for c in catalog.COMMANDS.values() if c.risk >= 1}
    for cid in ("door_unlock", "frunk", "trunk", "sentry_off", "vent_windows", "tonneau_open",
                "remote_start", "suspension_level", "powershare_on"):
        check(f"{cid} needs confirmation", cid in risky)
    check("remote_start and homelink are the strongest tier",
          catalog.COMMANDS["remote_start"].risk == 2 and catalog.COMMANDS["homelink"].risk == 2)
    # Reversible, harmless commands run on a tap — confirming them would only teach people to click through.
    for cid in ("door_lock", "sentry_on", "flash_lights", "honk_horn", "climate_on", "climate_off",
                "charge_port_open", "charge_port_close", "close_windows", "tonneau_close"):
        check(f"{cid} runs without confirmation", cid not in risky)

    # model applicability
    ct = {c.id for c in catalog.for_model("cybertruck")}
    my = {c.id for c in catalog.for_model("modely")}
    check("Cybertruck has tonneau, outlets, light bar, suspension",
          {"tonneau_open", "outlets_cabin_bed", "lightbar_brightness", "suspension_level"} <= ct)
    check("Model Y has none of them",
          not ({"tonneau_open", "outlets_cabin_bed", "lightbar_brightness", "suspension_level"} & my))
    check("sunroof only on S/X", "sunroof_vent" not in ct and "sunroof_vent" in {c.id for c in catalog.for_model("models")})

    # every signed method exists in the signing library
    from tesla_fleet_api.tesla.vehicle.signed import VehicleSigned

    missing = [c.id for c in catalog.COMMANDS.values() if c.method and not hasattr(VehicleSigned, c.method)]
    check("every signed command maps to a real library method", not missing, str(missing))

    # every command's arguments bind to the library method's real signature
    import inspect
    unbound = []
    for c in catalog.COMMANDS.values():
        if not c.method:
            continue
        sample = {p.name: (p.choices[-1] if p.choices else p.hi if p.hi is not None
                           else "x" if p.kind == "str" else True if p.kind == "bool" else 1) for p in c.params}
        try:
            _, vp = catalog.resolve(c.id, sample)
            vp.setdefault("_lat", 40.0)
            vp.setdefault("_lon", -98.0)
            inspect.signature(getattr(VehicleSigned, c.method)).bind(None, **c.args(vp))
        except Exception as e:  # noqa: BLE001
            unbound.append(f"{c.id}: {e}")
    check("every command's arguments fit the library method's signature", not unbound, "; ".join(unbound))

    # unsigned transport only for pre-2021 S/X
    check("2018 Model S may use unsigned commands",
          commands.unsigned_allowed({"model_key": "models", "model_year": 2018}))
    check("2024 Cybertruck may not",
          not commands.unsigned_allowed({"model_key": "cybertruck", "model_year": 2024}))
    check("2021 Model X (refresh) may not",
          not commands.unsigned_allowed({"model_key": "modelx", "model_year": 2021}))

    # library errors map to actionable codes
    from tesla_fleet_api import exceptions as x

    cases = {
        x.VehicleOffline(): "asleep",
        x.RateLimited(): "rate_limited",
        x.InsufficientCredits(): "billing",
        x.MobileAccessDisabled(): "mobile_access_disabled",
    }
    for exc, code in cases.items():
        got = commands._classify(exc).code
        check(f"{type(exc).__name__} → {code}", got == code, got)
    to = commands._classify(asyncio.TimeoutError())
    check("a timeout is reported as possibly executed", to.code == "timeout" and to.maybe_executed)

    print()
    print("全部通过" if all(results) else f"{results.count(False)} 项失败")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
