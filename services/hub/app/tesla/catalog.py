"""Every vehicle command the app can send, as data.

One table drives the controls UI, the HTTP API, MCP and automations, so a
command means the same thing everywhere. Each entry says:

* how to send it **signed** (a ``tesla_fleet_api`` method — required by every
  2021+ car, Cybertruck included) and, where Tesla still accepts it, how to send
  it **unsigned** over the old REST endpoint (pre-2021 Model S/X only);
* which parameters it takes, with types and ranges — the *only* values a
  caller can influence. Fixed arguments live in the builders, out of reach of
  ``params``: "sentry_on" can never be turned into "sentry off" by passing
  ``{"on": false}`` (an audit found exactly that bypass in the old table);
* how risky it is: 0 runs on tap, 1 asks for confirmation, 2 asks the user to
  type the action (things that move the car or disable protection);
* which model lines it applies to (tonneau, outlets, light bar … are
  Cybertruck-only), and what live state it should produce, so the UI can
  reflect the result before the next poll arrives.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

CT = frozenset({"cybertruck"})
SX = frozenset({"models", "modelx"})


@dataclass(frozen=True)
class Param:
    name: str
    kind: str                       # int | float | bool | enum | str
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()
    required: bool = True
    default: Any = None
    unit: str | None = None         # hint for the UI (°C, %, A, min, s)

    def coerce(self, value: Any) -> Any:
        if value is None:
            if self.required and self.default is None:
                raise ValueError(f"missing parameter '{self.name}'")
            return self.default
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "on", "off"):
                return value.lower() in ("true", "1", "on")
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            raise ValueError(f"'{self.name}' must be true or false")
        if self.kind in ("int", "float", "enum"):
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"'{self.name}' must be a number") from None
            if not math.isfinite(num):           # "nan" passes every range check; "inf" breaks int()
                raise ValueError(f"'{self.name}' must be a number")
            if self.kind == "enum":
                if int(num) not in self.choices:
                    raise ValueError(f"'{self.name}' must be one of {list(self.choices)}")
                return int(num)
            if (self.lo is not None and num < self.lo) or (self.hi is not None and num > self.hi):
                raise ValueError(f"'{self.name}' must be between {self.lo:g} and {self.hi:g}")
            return round(num) if self.kind == "int" else num
        s = str(value).strip()
        if not s and self.required:
            raise ValueError(f"missing parameter '{self.name}'")
        if len(s) > 500:
            raise ValueError(f"'{self.name}' is too long")
        return s


@dataclass(frozen=True)
class Cmd:
    id: str
    group: str
    icon: str
    method: str | None                                  # tesla_fleet_api signed method
    args: Callable[[dict], dict] = lambda p: {}         # params -> method kwargs
    rest: str | None = None                             # unsigned REST command name
    body: Callable[[dict], dict] = lambda p: {}         # params -> REST JSON body
    params: tuple[Param, ...] = ()
    risk: int = 0
    models: frozenset | None = None                     # None = every model line
    expect: dict = field(default_factory=dict)          # live-state keys it should produce
    needs_location: bool = False                        # needs the car's lat/lon (homelink)
    wake: bool = True

    def applies_to(self, model_key: str | None) -> bool:
        return self.models is None or (model_key in self.models)

    def validate(self, raw: dict | None) -> dict:
        raw = raw or {}
        unknown = set(raw) - {p.name for p in self.params}
        if unknown:
            raise ValueError(f"unexpected parameter(s): {', '.join(sorted(unknown))}")
        return {p.name: p.coerce(raw.get(p.name)) for p in self.params}

    def describe(self) -> dict:
        return {
            "id": self.id, "group": self.group, "icon": self.icon, "risk": self.risk,
            "models": sorted(self.models) if self.models else None,
            "signed": self.method is not None, "unsigned": self.rest is not None,
            "params": [
                {"name": p.name, "kind": p.kind, "lo": p.lo, "hi": p.hi,
                 "choices": list(p.choices), "required": p.required,
                 "default": p.default, "unit": p.unit}
                for p in self.params
            ],
            "expect": self.expect,
        }


def _on(v: bool) -> Callable[[dict], dict]:
    return lambda p: {"on": v}


# Seat positions as the signed protocol numbers them (front-left 0, front-right 1,
# rear-left 2, rear-centre 4, rear-right 5). Coolers exist on the front seats only,
# and their protocol uses 1/2 for front-left/front-right.
SEATS = (0, 1, 2, 4, 5)

_ALL: list[Cmd] = [
    # ── security ─────────────────────────────────────────────────────────
    Cmd("door_lock", "security", "lock", "door_lock", rest="door_lock", expect={"locked": True}),
    Cmd("door_unlock", "security", "unlock", "door_unlock", rest="door_unlock", risk=1,
        expect={"locked": False}),
    Cmd("sentry_on", "security", "shield", "set_sentry_mode", _on(True),
        rest="set_sentry_mode", body=lambda p: {"on": True}, expect={"sentry_mode": True}),
    Cmd("sentry_off", "security", "shield", "set_sentry_mode", _on(False),
        rest="set_sentry_mode", body=lambda p: {"on": False}, risk=1, expect={"sentry_mode": False}),
    Cmd("flash_lights", "security", "lightbulb", "flash_lights", rest="flash_lights"),
    Cmd("honk_horn", "security", "horn", "honk_horn", rest="honk_horn"),
    Cmd("remote_start", "security", "key", "remote_start_drive", rest="remote_start_drive", risk=2),
    Cmd("dashcam_save_clip", "security", "film", "dashcam_save_clip"),
    Cmd("valet_on", "security", "key", "set_valet_mode",
        lambda p: {"on": True, "password": p["pin"]}, rest="set_valet_mode",
        body=lambda p: {"on": True, "password": p["pin"]},
        params=(Param("pin", "str"),), risk=1, expect={"valet_mode": True}),
    Cmd("valet_off", "security", "key", "set_valet_mode",
        lambda p: {"on": False, "password": p["pin"]}, rest="set_valet_mode",
        body=lambda p: {"on": False, "password": p["pin"]},
        params=(Param("pin", "str"),), expect={"valet_mode": False}),
    Cmd("speed_limit_set", "security", "gauge", "speed_limit_set_limit",
        lambda p: {"limit_mph": p["limit_mph"]}, rest="speed_limit_set_limit",
        body=lambda p: {"limit_mph": p["limit_mph"]},
        params=(Param("limit_mph", "int", 50, 120, unit="mph"),)),
    Cmd("speed_limit_on", "security", "gauge", "speed_limit_activate",
        lambda p: {"pin": p["pin"]}, rest="speed_limit_activate", body=lambda p: {"pin": p["pin"]},
        params=(Param("pin", "str"),)),
    Cmd("speed_limit_off", "security", "gauge", "speed_limit_deactivate",
        lambda p: {"pin": p["pin"]}, rest="speed_limit_deactivate", body=lambda p: {"pin": p["pin"]},
        params=(Param("pin", "str"),), risk=1),

    # ── closures ─────────────────────────────────────────────────────────
    # actuate_trunk *toggles*: never retry it blindly — a retry closes what
    # the first attempt opened.
    Cmd("frunk", "closures", "box", "actuate_trunk", lambda p: {"which_trunk": "front"},
        rest="actuate_trunk", body=lambda p: {"which_trunk": "front"}, risk=1,
        expect={"frunk_open": True}),
    Cmd("trunk", "closures", "box", "actuate_trunk", lambda p: {"which_trunk": "rear"},
        rest="actuate_trunk", body=lambda p: {"which_trunk": "rear"}, risk=1),
    Cmd("tonneau_open", "closures", "bed", "open_tonneau", models=CT, risk=1,
        expect={"tonneau": "open"}),
    Cmd("tonneau_close", "closures", "bed", "close_tonneau", models=CT, expect={"tonneau": "closed"}),
    Cmd("tonneau_stop", "closures", "bed", "stop_tonneau", models=CT),
    Cmd("vent_windows", "closures", "window", "window_control", lambda p: {"command": "vent"},
        rest="window_control", body=lambda p: {"command": "vent", "lat": 0, "lon": 0}, risk=1,
        needs_location=False, expect={"windows_open": True}),
    Cmd("close_windows", "closures", "window", "window_control", lambda p: {"command": "close"},
        rest="window_control", body=lambda p: {"command": "close", "lat": 0, "lon": 0},
        expect={"windows_open": False}),
    Cmd("charge_port_open", "closures", "plug", "charge_port_door_open", rest="charge_port_door_open",
        expect={"charge_port_door_open": True}),
    Cmd("charge_port_close", "closures", "plug", "charge_port_door_close", rest="charge_port_door_close",
        expect={"charge_port_door_open": False}),
    Cmd("homelink", "closures", "garage", "trigger_homelink", lambda p: {"lat": p["_lat"], "lon": p["_lon"]},
        rest="trigger_homelink", body=lambda p: {"lat": p["_lat"], "lon": p["_lon"]},
        needs_location=True, risk=1),
    Cmd("sunroof_vent", "closures", "window", "sun_roof_control", lambda p: {"state": "vent"},
        rest="sun_roof_control", body=lambda p: {"state": "vent"}, models=SX, risk=1),
    Cmd("sunroof_close", "closures", "window", "sun_roof_control", lambda p: {"state": "close"},
        rest="sun_roof_control", body=lambda p: {"state": "close"}, models=SX),

    # ── climate ──────────────────────────────────────────────────────────
    Cmd("climate_on", "climate", "climate", "auto_conditioning_start", rest="auto_conditioning_start",
        expect={"is_climate_on": True}),
    Cmd("climate_off", "climate", "climate", "auto_conditioning_stop", rest="auto_conditioning_stop",
        expect={"is_climate_on": False}),
    # Both zones always: the protocol treats a missing passenger temp as 0 °C.
    Cmd("set_temps", "climate", "thermo", "set_temps",
        lambda p: {"driver_temp": p["driver_temp"], "passenger_temp": p.get("passenger_temp") or p["driver_temp"]},
        rest="set_temps",
        body=lambda p: {"driver_temp": p["driver_temp"], "passenger_temp": p.get("passenger_temp") or p["driver_temp"]},
        params=(Param("driver_temp", "float", 15, 28, unit="°C"),
                Param("passenger_temp", "float", 15, 28, required=False, unit="°C"))),
    Cmd("seat_heater", "climate", "seat", "remote_seat_heater_request",
        lambda p: {"seat_position": p["seat"], "seat_heater_level": p["level"]},
        rest="remote_seat_heater_request",
        body=lambda p: {"heater": p["seat"], "level": p["level"]},     # legacy REST naming
        params=(Param("seat", "enum", choices=SEATS), Param("level", "enum", choices=(0, 1, 2, 3)))),
    Cmd("seat_cooler", "climate", "fan", "remote_seat_cooler_request",
        lambda p: {"seat_position": p["seat"], "seat_cooler_level": p["level"]},
        # seat 1 = front left, 2 = front right (proto numbering); level 0 off … 3 high
        # (tesla_fleet_api indexes its level table, it does not take the proto value).
        params=(Param("seat", "enum", choices=(1, 2)), Param("level", "enum", choices=(0, 1, 2, 3)))),
    Cmd("steering_heater_on", "climate", "wheel", "remote_steering_wheel_heater_request", _on(True),
        rest="remote_steering_wheel_heater_request", body=lambda p: {"on": True}),
    Cmd("steering_heater_off", "climate", "wheel", "remote_steering_wheel_heater_request", _on(False),
        rest="remote_steering_wheel_heater_request", body=lambda p: {"on": False}),
    Cmd("defrost_on", "climate", "defrost", "set_preconditioning_max",
        lambda p: {"on": True, "manual_override": True}, rest="set_preconditioning_max",
        body=lambda p: {"on": True}, expect={"is_front_defroster_on": True}),
    Cmd("defrost_off", "climate", "defrost", "set_preconditioning_max",
        lambda p: {"on": False, "manual_override": True}, rest="set_preconditioning_max",
        body=lambda p: {"on": False}, expect={"is_front_defroster_on": False}),
    Cmd("keeper_on", "climate", "climate", "set_climate_keeper_mode", lambda p: {"climate_keeper_mode": 1},
        rest="set_climate_keeper_mode", body=lambda p: {"climate_keeper_mode": 1},
        expect={"climate_keeper_mode": "on"}),
    Cmd("dog_mode_on", "climate", "dog", "set_climate_keeper_mode", lambda p: {"climate_keeper_mode": 2},
        rest="set_climate_keeper_mode", body=lambda p: {"climate_keeper_mode": 2},
        expect={"climate_keeper_mode": "dog"}),
    Cmd("camp_mode_on", "climate", "tent", "set_climate_keeper_mode", lambda p: {"climate_keeper_mode": 3},
        rest="set_climate_keeper_mode", body=lambda p: {"climate_keeper_mode": 3},
        expect={"climate_keeper_mode": "camp"}),
    Cmd("keeper_off", "climate", "climate", "set_climate_keeper_mode", lambda p: {"climate_keeper_mode": 0},
        rest="set_climate_keeper_mode", body=lambda p: {"climate_keeper_mode": 0},
        expect={"climate_keeper_mode": "off"}),
    Cmd("cabin_overheat_on", "climate", "sun", "set_cabin_overheat_protection",
        lambda p: {"on": True, "fan_only": False}, rest="set_cabin_overheat_protection",
        body=lambda p: {"on": True, "fan_only": False}),
    Cmd("cabin_overheat_fan", "climate", "sun", "set_cabin_overheat_protection",
        lambda p: {"on": True, "fan_only": True}, rest="set_cabin_overheat_protection",
        body=lambda p: {"on": True, "fan_only": True}),
    Cmd("cabin_overheat_off", "climate", "sun", "set_cabin_overheat_protection",
        lambda p: {"on": False, "fan_only": False}, rest="set_cabin_overheat_protection",
        body=lambda p: {"on": False, "fan_only": False}),
    Cmd("bioweapon_on", "climate", "shield", "set_bioweapon_mode",
        lambda p: {"on": True, "manual_override": True}, rest="set_bioweapon_mode",
        body=lambda p: {"on": True, "manual_override": True}),
    Cmd("bioweapon_off", "climate", "shield", "set_bioweapon_mode",
        lambda p: {"on": False, "manual_override": True}, rest="set_bioweapon_mode",
        body=lambda p: {"on": False, "manual_override": True}),
    Cmd("recirculation_on", "climate", "climate", "set_recirculation", _on(True)),
    Cmd("recirculation_off", "climate", "climate", "set_recirculation", _on(False)),

    # ── charging ─────────────────────────────────────────────────────────
    Cmd("charge_start", "charging", "bolt", "charge_start", rest="charge_start",
        expect={"charging_state": "Charging"}),
    Cmd("charge_stop", "charging", "bolt", "charge_stop", rest="charge_stop",
        expect={"charging_state": "Stopped"}),
    Cmd("set_charge_limit", "charging", "battery", "set_charge_limit", lambda p: {"percent": p["percent"]},
        rest="set_charge_limit", body=lambda p: {"percent": p["percent"]},
        params=(Param("percent", "int", 50, 100, unit="%"),)),
    Cmd("charge_standard", "charging", "battery", "charge_standard", rest="charge_standard"),
    Cmd("charge_max_range", "charging", "battery", "charge_max_range", rest="charge_max_range"),
    Cmd("set_charge_amps", "charging", "bolt", "set_charging_amps",
        lambda p: {"charging_amps": p["charging_amps"]}, rest="set_charging_amps",
        body=lambda p: {"charging_amps": p["charging_amps"]},
        params=(Param("charging_amps", "int", 1, 80, unit="A"),)),
    Cmd("schedule_charge", "charging", "clock", "set_scheduled_charging",
        lambda p: {"enable": True, "time": p["time"]}, rest="set_scheduled_charging",
        body=lambda p: {"enable": True, "time": p["time"]},
        params=(Param("time", "int", 0, 1439, unit="min"),)),
    Cmd("schedule_charge_off", "charging", "clock", "set_scheduled_charging",
        lambda p: {"enable": False, "time": 0}, rest="set_scheduled_charging",
        body=lambda p: {"enable": False, "time": 0}),
    Cmd("schedule_departure", "charging", "clock", "set_scheduled_departure",
        lambda p: {"enable": True, "departure_time": p["time"], "preconditioning_enabled": p["precondition"],
                   "off_peak_charging_enabled": p["off_peak"]},
        params=(Param("time", "int", 0, 1439, unit="min"),
                Param("precondition", "bool", required=False, default=True),
                Param("off_peak", "bool", required=False, default=False))),
    Cmd("schedule_departure_off", "charging", "clock", "set_scheduled_departure",
        lambda p: {"enable": False}),

    # ── Cybertruck power (outlets, Powershare) ──────────────────────────
    Cmd("outlets_cabin_bed", "power", "outlet", "set_outlets", lambda p: {"request": 1}, models=CT,
        expect={"outlet_state": "CabinAndBed"}),
    Cmd("outlets_cabin", "power", "outlet", "set_outlets", lambda p: {"request": 2}, models=CT,
        expect={"outlet_state": "Cabin"}),
    Cmd("outlets_off", "power", "outlet", "set_outlets", lambda p: {"request": 0}, models=CT,
        expect={"outlet_state": "Off"}),
    Cmd("outlet_timer", "power", "clock", "set_outlet_timer", lambda p: {"num_minutes": p["minutes"]},
        params=(Param("minutes", "int", 0, 1440, unit="min"),), models=CT),
    Cmd("outlet_soc_limit", "power", "battery", "set_outlet_soc_limit", lambda p: {"percent": p["percent"]},
        params=(Param("percent", "int", 0, 100, unit="%"),), models=CT),
    Cmd("powershare_on", "power", "home", "set_powershare_request", _on(True), models=CT, risk=1),
    Cmd("powershare_off", "power", "home", "set_powershare_request", _on(False), models=CT),
    Cmd("powershare_limit", "power", "battery", "set_powershare_discharge_limit",
        lambda p: {"percent": p["percent"]}, params=(Param("percent", "int", 0, 100, unit="%"),), models=CT),
    Cmd("accessory_power_on", "power", "outlet", "set_keep_accessory_power_mode", _on(True)),
    Cmd("accessory_power_off", "power", "outlet", "set_keep_accessory_power_mode", _on(False)),
    Cmd("low_power_on", "power", "battery", "set_low_power_mode", _on(True)),
    Cmd("low_power_off", "power", "battery", "set_low_power_mode", _on(False)),

    # ── Cybertruck lights & bed ──────────────────────────────────────────
    Cmd("lightbar_brightness", "lights", "sun", "set_lightbar_brightness",
        lambda p: {"brightness": p["brightness"]}, params=(Param("brightness", "int", 0, 100, unit="%"),),
        models=CT),
    Cmd("lightbar_middle_on", "lights", "lightbulb", "set_lightbar_middle", _on(True), models=CT),
    Cmd("lightbar_middle_off", "lights", "lightbulb", "set_lightbar_middle", _on(False), models=CT),
    Cmd("ditch_lights_on", "lights", "lightbulb", "set_lightbar_ditch", _on(True), models=CT),
    Cmd("ditch_lights_off", "lights", "lightbulb", "set_lightbar_ditch", _on(False), models=CT),
    Cmd("front_zone_lights", "lights", "lightbulb", "set_front_zone_lights", lambda p: {"level": p["level"]},
        params=(Param("level", "enum", choices=(0, 1, 2, 3)),), models=CT),
    Cmd("rear_zone_lights", "lights", "lightbulb", "set_rear_zone_lights", lambda p: {"level": p["level"]},
        params=(Param("level", "enum", choices=(0, 1, 2, 3)),), models=CT),
    Cmd("bed_light_auto_on", "lights", "bed", "set_truck_bed_light_auto", _on(True), models=CT),
    Cmd("bed_light_auto_off", "lights", "bed", "set_truck_bed_light_auto", _on(False), models=CT),
    Cmd("bed_light_brightness", "lights", "bed", "set_truck_bed_light_brightness",
        lambda p: {"brightness": p["brightness"]}, params=(Param("brightness", "int", 0, 100, unit="%"),),
        models=CT),
    Cmd("tent_mode_on", "lights", "tent", "set_tent_mode", _on(True), models=CT),
    Cmd("tent_mode_off", "lights", "tent", "set_tent_mode", _on(False), models=CT),
    # Ride height moves the body with people possibly around it: confirm.
    Cmd("suspension_level", "drive", "height", "set_suspension_level", lambda p: {"level": p["level"]},
        params=(Param("level", "enum", choices=(1, 2, 3, 4, 5, 6)),), models=CT, risk=1),

    # ── media ────────────────────────────────────────────────────────────
    Cmd("media_toggle", "media", "play", "media_toggle_playback", rest="media_toggle_playback"),
    Cmd("media_next", "media", "next", "media_next_track", rest="media_next_track"),
    Cmd("media_prev", "media", "prev", "media_prev_track", rest="media_prev_track"),
    Cmd("volume_up", "media", "volume", "media_volume_up", rest="media_volume_up"),
    Cmd("volume_down", "media", "volume", "media_volume_down", rest="media_volume_down"),
    Cmd("set_volume", "media", "volume", "adjust_volume", lambda p: {"volume": p["volume"]},
        rest="adjust_volume", body=lambda p: {"volume": p["volume"]},
        params=(Param("volume", "float", 0, 11),)),

    # ── navigation & software ────────────────────────────────────────────
    Cmd("navigate_to", "navigation", "pin", "navigation_request", lambda p: {"value": p["address"]},
        params=(Param("address", "str"),)),
    Cmd("navigate_gps", "navigation", "pin", "navigation_gps_request",
        lambda p: {"lat": p["lat"], "lon": p["lon"]},
        params=(Param("lat", "float", -90, 90), Param("lon", "float", -180, 180))),
    Cmd("schedule_update", "software", "download", "schedule_software_update",
        lambda p: {"offset_sec": p["offset_sec"]}, rest="schedule_software_update",
        body=lambda p: {"offset_sec": p["offset_sec"]},
        params=(Param("offset_sec", "int", 0, 86400 * 2, unit="s"),), risk=1),
    Cmd("cancel_update", "software", "download", "cancel_software_update", rest="cancel_software_update"),
]

COMMANDS: dict[str, Cmd] = {c.id: c for c in _ALL}

# Names the previous API used — MCP prompts and saved automations still send them.
ALIASES = {
    "seat_heater_left": ("seat_heater", {"seat": 0}),
    "seat_heater_right": ("seat_heater", {"seat": 1}),
}


def resolve(name: str, params: dict | None) -> tuple[Cmd, dict]:
    """Look a command up by id or legacy alias; returns (spec, validated params)."""
    params = dict(params or {})
    if name in ALIASES:
        target, fixed = ALIASES[name]
        params = {**params, **fixed}
        name = target
    spec = COMMANDS.get(name)
    if spec is None:
        raise KeyError(name)
    return spec, spec.validate(params)


def for_model(model_key: str | None) -> list[Cmd]:
    return [c for c in _ALL if c.applies_to(model_key)]
