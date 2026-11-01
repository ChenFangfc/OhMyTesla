"""The car's full current state, read on demand — what the official app shows.

TeslaMate's MQTT carries what a logger needs (battery, location, doors,
climate on/off). The controls screen needs more: per-seat heater levels,
charge limit and current, cabin-overheat setting, tonneau position, outlets
and Powershare on a Cybertruck, media, software update progress. Those exist
only in ``vehicle_data``.

Rules:

* Read only when the car is already awake (TeslaMate says so). Never wake it
  implicitly — waking costs battery and, on the Fleet API, money.
* Use TeslaMate's token read-only (never refresh it).
* Cache for 20 s: several screens open at once must not multiply requests.
* Return metric units; the UI converts.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .. import db
from ..config import settings
from ..livestate import live
from ..tesla import tokens

log = logging.getLogger(__name__)

_CACHE_S = 20
_cache: dict[int, tuple[float, dict]] = {}
_locks: dict[int, asyncio.Lock] = {}
_last_wake: dict[int, float] = {}
_ENDPOINTS = "charge_state;climate_state;closures_state;drive_state;gui_settings;location_data;vehicle_config;vehicle_state"
MI_TO_KM = 1.609344
AWAKE = ("online", "charging", "driving", "updating")


def _b(v):
    return None if v is None else bool(v)


def _open(v) -> bool | None:
    return None if v is None else v not in (0, False)


def normalise(r: dict) -> dict:
    """vehicle_data response → the shape the UI uses (metric)."""
    cs, cl = r.get("charge_state") or {}, r.get("climate_state") or {}
    vs, ds = r.get("vehicle_state") or {}, r.get("drive_state") or {}
    vc, gui = r.get("vehicle_config") or {}, r.get("gui_settings") or {}

    def km(mi):
        return None if mi is None else round(float(mi) * MI_TO_KM, 1)

    seats = {k: cl.get(f"seat_heater_{n}") for k, n in (
        ("fl", "left"), ("fr", "right"), ("rl", "rear_left"), ("rc", "rear_center"), ("rr", "rear_right"))}
    ct = {k: v for k, v in {**vs, **vc, **cs}.items()
          if k.startswith(("tonneau", "powershare", "outlet", "power_feed", "lightbar", "bed_light",
                           "tent", "offroad", "suspension", "ride_height"))}
    return {
        "charge": {
            "level": cs.get("battery_level"), "usable_level": cs.get("usable_battery_level"),
            "limit": cs.get("charge_limit_soc"), "limit_min": cs.get("charge_limit_soc_min"),
            "limit_max": cs.get("charge_limit_soc_max"), "range_km": km(cs.get("battery_range")),
            "est_range_km": km(cs.get("est_battery_range")), "state": cs.get("charging_state"),
            "power_kw": cs.get("charger_power"), "amps": cs.get("charge_current_request"),
            "amps_max": cs.get("charge_current_request_max"), "voltage": cs.get("charger_voltage"),
            "port_open": _b(cs.get("charge_port_door_open")), "latch": cs.get("charge_port_latch"),
            "cable": cs.get("conn_charge_cable"), "fast_charger": _b(cs.get("fast_charger_present")),
            "minutes_to_full": round(cs["time_to_full_charge"] * 60) if cs.get("time_to_full_charge") else None,
            "energy_added_kwh": cs.get("charge_energy_added"),
            "scheduled_start_min": cs.get("scheduled_charging_start_time_minutes"),
            "scheduled_mode": cs.get("scheduled_charging_mode"),
            "preconditioning": _b(cl.get("is_preconditioning")),
        },
        "climate": {
            "on": _b(cl.get("is_climate_on")), "inside_c": cl.get("inside_temp"),
            "outside_c": cl.get("outside_temp"), "driver_c": cl.get("driver_temp_setting"),
            "passenger_c": cl.get("passenger_temp_setting"), "min_c": cl.get("min_avail_temp"),
            "max_c": cl.get("max_avail_temp"), "seats": seats,
            "seat_cool": {"fl": cl.get("seat_fan_front_left"), "fr": cl.get("seat_fan_front_right")},
            "steering_heat": cl.get("steering_wheel_heat_level", cl.get("steering_wheel_heater")),
            "defrost_front": _b(cl.get("is_front_defroster_on")), "defrost_rear": _b(cl.get("is_rear_defroster_on")),
            "keeper": cl.get("climate_keeper_mode"), "cop": cl.get("cabin_overheat_protection"),
            "cop_actively_cooling": _b(cl.get("cabin_overheat_protection_actively_cooling")),
            "bioweapon": _b(cl.get("bioweapon_mode")), "fan": cl.get("fan_status"),
            "remote_heater_allowed": _b(cl.get("remote_heater_control_enabled")),
        },
        "closures": {
            "locked": _b(vs.get("locked")),
            "doors": {"fl": _open(vs.get("df")), "fr": _open(vs.get("pf")),
                      "rl": _open(vs.get("dr")), "rr": _open(vs.get("pr"))},
            "frunk": _open(vs.get("ft")), "trunk": _open(vs.get("rt")),
            "windows": {"fl": _open(vs.get("fd_window")), "fr": _open(vs.get("fp_window")),
                        "rl": _open(vs.get("rd_window")), "rr": _open(vs.get("rp_window"))},
            "sentry": _b(vs.get("sentry_mode")), "sentry_available": _b(vs.get("sentry_mode_available")),
            "valet": _b(vs.get("valet_mode")), "user_present": _b(vs.get("is_user_present")),
            "center_display": vs.get("center_display_state"),
            "homelink_nearby": _b(vs.get("homelink_nearby")),
        },
        "tires": {k: vs.get(f"tpms_pressure_{k}") for k in ("fl", "fr", "rl", "rr")}
        | {"warnings": {k: _b(vs.get(f"tpms_soft_warning_{k}")) for k in ("fl", "fr", "rl", "rr")}},
        "media": {"playing": (vs.get("media_info") or {}).get("media_playback_status"),
                  "title": (vs.get("media_info") or {}).get("now_playing_title"),
                  "artist": (vs.get("media_info") or {}).get("now_playing_artist"),
                  "source": (vs.get("media_info") or {}).get("now_playing_source"),
                  "volume": (vs.get("media_info") or {}).get("audio_volume"),
                  "volume_max": (vs.get("media_info") or {}).get("audio_volume_max")},
        "software": {"version": (vs.get("car_version") or "").split(" ")[0] or None,
                     "update_status": (vs.get("software_update") or {}).get("status"),
                     "update_version": (vs.get("software_update") or {}).get("version"),
                     "download_pct": (vs.get("software_update") or {}).get("download_perc"),
                     "install_pct": (vs.get("software_update") or {}).get("install_perc")},
        "location": {"lat": ds.get("latitude"), "lon": ds.get("longitude"), "heading": ds.get("heading"),
                     "speed_kmh": None if ds.get("speed") is None else round(ds["speed"] * MI_TO_KM),
                     "shift": ds.get("shift_state"), "power_kw": ds.get("power")},
        "odometer_km": km(vs.get("odometer")),
        "name": vs.get("vehicle_name"),
        "config": {"car_type": vc.get("car_type"), "trim": vc.get("trim_badging"),
                   "color": vc.get("exterior_color"), "wheels": vc.get("wheel_type"),
                   "paint_override": vc.get("paint_color_override"),
                   "has_seat_cooling": _b(vc.get("has_seat_cooling")),
                   "rear_seat_heaters": vc.get("rear_seat_heaters"),
                   "sun_roof": vc.get("sun_roof_installed"),
                   "units": {"distance": gui.get("gui_distance_units"), "temp": gui.get("gui_temperature_units")}},
        "cybertruck": ct,
    }


async def _eid(car_id: int) -> int | None:
    row = await db.fetchrow("SELECT eid FROM cars WHERE id = $1", car_id)
    return int(row["eid"]) if row and row["eid"] else None


def _base() -> str:
    return (settings.tesla_api_host or "https://owner-api.teslamotors.com").rstrip("/")


async def read(car_id: int, refresh: bool = False) -> dict:
    """Full state if the car is awake; otherwise what TeslaMate last told us."""
    state = (live.cars.get(str(car_id)) or {}).get("state")
    now = time.time()
    hit = _cache.get(car_id)
    if hit and not refresh and now - hit[0] < _CACHE_S:
        return {**hit[1], "cached_s": round(now - hit[0])}
    if state not in AWAKE:
        out = {"asleep": True, "state": state, "fetched_at": hit[0] if hit else None}
        if hit:
            out["last"] = hit[1]
        return out
    lock = _locks.setdefault(car_id, asyncio.Lock())
    async with lock:
        hit = _cache.get(car_id)
        if hit and time.time() - hit[0] < 3:
            return hit[1]
        token, eid = await tokens.access_token(), await _eid(car_id)
        if not token or not eid:
            return {"asleep": False, "error": "no_token" if not token else "no_vehicle_id"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as cli:
                r = await cli.get(f"{_base()}/api/1/vehicles/{eid}/vehicle_data",
                                  params={"endpoints": _ENDPOINTS},
                                  headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except httpx.HTTPError as e:
            return {"asleep": False, "error": f"network: {e.__class__.__name__}"}
        if r.status_code == 408:
            return {"asleep": True, "state": "asleep"}
        if r.status_code == 401:
            tokens.invalidate_cache()
            return {"asleep": False, "error": "token_rotating"}
        if r.status_code >= 400:
            return {"asleep": False, "error": f"HTTP {r.status_code}"}
        data = normalise((r.json() or {}).get("response") or {})
        data.update({"asleep": False, "state": state, "fetched_at": time.time()})
        _cache[car_id] = (time.time(), data)
        return data


async def wake(car_id: int) -> dict:
    """Wake the car (a plain REST call — no signed command needed, so it works
    for every car with only TeslaMate's login). Throttled to once a minute."""
    now = time.time()
    if now - _last_wake.get(car_id, 0) < 60:
        return {"ok": True, "throttled": True}
    token, eid = await tokens.access_token(), await _eid(car_id)
    if not token or not eid:
        return {"ok": False, "error": "no_token" if not token else "no_vehicle_id"}
    _last_wake[car_id] = now
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as cli:
        r = await cli.post(f"{_base()}/api/1/vehicles/{eid}/wake_up",
                           headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code >= 400:
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    return {"ok": True, "state": ((r.json() or {}).get("response") or {}).get("state")}
