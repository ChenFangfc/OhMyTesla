"""Send a vehicle command over whichever path this car accepts.

Two transports:

* **signed** — Tesla Fleet API through ``tesla_fleet_api``, which implements
  the vehicle-command protocol (session handshake, HMAC/AES-GCM signing) in
  process. Required by every 2021+ car, Cybertruck included. Needs the owner's
  own developer app and a paired virtual key (see ``fleet.py``).
* **unsigned** — the legacy REST endpoint with TeslaMate's token. Tesla still
  accepts it only from pre-2021 Model S/X; everything newer answers 403.

Callers (HTTP API, MCP, automations) all come through ``send`` and get either
a result or a ``CommandError`` whose ``code`` says what to do next
(``needs_setup``, ``not_paired``, ``asleep``, ``timeout`` …). A ``timeout``
carries ``maybe_executed=True``: the car may well have done it, and
``actuate_trunk`` *toggles*, so nothing here ever retries a command that
might have run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import httpx

from .. import db
from ..config import settings
from ..livestate import live
from ..vehicle import catalog as vehicle_catalog
from . import catalog, fleet, tokens

log = logging.getLogger(__name__)

# Model-year letter (VIN position 10) → year. Unsigned commands died with the
# 2021 refresh of S/X; earlier S/X still take them.
_YEAR = {c: 2010 + i for i, c in enumerate("ABCDEFGHJKLMNPRSTVWXY")}


class CommandError(Exception):
    def __init__(self, message: str, code: str = "error", *, maybe_executed: bool = False,
                 transport: str | None = None, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.maybe_executed = maybe_executed
        self.transport = transport
        self.detail = detail

    def as_dict(self) -> dict:
        return {"ok": False, "error": str(self), "code": self.code,
                "maybe_executed": self.maybe_executed, "transport": self.transport,
                "detail": self.detail}


# Kept for callers that still look commands up by id (MCP describe, UI lists).
COMMANDS = {c.id: c.describe() for c in catalog.COMMANDS.values()}


async def _car(car_id: int) -> dict:
    row = await db.fetchrow("SELECT id, eid, vin, model FROM cars WHERE id = $1", car_id)
    if row is None:
        raise CommandError(f"no car with id {car_id}", "unknown_car")
    car = dict(row)
    ident = vehicle_catalog.identify(vin=car.get("vin"), model=car.get("model"))
    car["model_key"] = ident.key
    vin = (car.get("vin") or "").upper()
    car["model_year"] = _YEAR.get(vin[9]) if len(vin) >= 10 else None
    return car


def unsigned_allowed(car: dict) -> bool:
    return car.get("model_key") in ("models", "modelx") and (car.get("model_year") or 9999) <= 2020


async def transport_for(car: dict) -> tuple[str | None, str]:
    """(transport, reason). transport is None when no path can command this car."""
    if await fleet.ready():
        return "signed", ""
    if unsigned_allowed(car):
        return "unsigned", ""
    return None, "needs_setup"


async def capabilities(car_id: int) -> dict:
    car = await _car(car_id)
    transport, reason = await transport_for(car)
    st = await fleet.status()
    vin = car.get("vin") or ""
    paired = vin in (st.get("paired") or {})
    cmds = [c.describe() for c in catalog.for_model(car["model_key"])
            if (transport == "signed" and c.method) or (transport == "unsigned" and c.rest)
            or transport is None]
    return {
        "car_id": car_id, "model": car["model_key"], "transport": transport,
        "ready": transport is not None and (transport != "signed" or paired),
        "reason": reason or ("not_paired" if transport == "signed" and not paired else ""),
        "paired": paired, "fleet": st, "commands": cmds,
    }


# ── signed transport ─────────────────────────────────────────────────────

_api = None
_session = None
_vehicles: dict[str, object] = {}
_api_lock = asyncio.Lock()


async def _signed_vehicle(vin: str):
    """One signed vehicle object per VIN, kept alive so its session survives
    between commands (a fresh session costs a handshake with both car domains)."""
    global _api, _session
    async with _api_lock:
        if _api is None:
            import aiohttp
            from tesla_fleet_api import TeslaFleetApi

            _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            _api = TeslaFleetApi(_session, access_token=fleet.access_token, region=await fleet.region())
            fleet.ensure_keypair()
            await _api.get_private_key(str(fleet.private_key_path()))
        v = _vehicles.get(vin)
        if v is None:
            v = _api.vehicles.createSigned(vin)
            _vehicles[vin] = v
        return v


async def reset_signed() -> None:
    """Drop cached clients — after the Fleet app or key changes."""
    global _api, _session
    async with _api_lock:
        _vehicles.clear()
        if _session is not None:
            await _session.close()
        _api, _session = None, None


async def shutdown() -> None:
    await reset_signed()


def _classify(e: BaseException) -> CommandError:
    from tesla_fleet_api import exceptions as x

    name = type(e).__name__
    if isinstance(e, (x.NotOnWhitelistFault, x.TeslaFleetMessageFaultUnknownKeyId,
                      x.TeslaFleetMessageFaultInactiveKey)):
        return CommandError("this car hasn't paired the app's key yet — approve it in the Tesla app",
                            "not_paired", transport="signed")
    if isinstance(e, x.VehicleOffline):
        return CommandError("the car is asleep or offline", "asleep", transport="signed")
    if isinstance(e, (x.GatewayTimeout, x.TeslaFleetMessageFaultTimeout, asyncio.TimeoutError)):
        return CommandError("no answer from the car in time — it may have done it anyway",
                            "timeout", maybe_executed=True, transport="signed")
    if isinstance(e, x.RateLimited):
        return CommandError("Tesla is rate-limiting this account — try again in a minute",
                            "rate_limited", transport="signed")
    if isinstance(e, (x.InsufficientCredits, x.PaymentRequired)):
        return CommandError("the Fleet API account is out of credit — check billing on developer.tesla.com",
                            "billing", transport="signed")
    if isinstance(e, (x.OAuthExpired, x.InvalidToken, x.OAuthInvalid, x.LoginRequired, x.MissingToken)):
        return CommandError("the Fleet API sign-in is no longer valid — sign in again", "auth",
                            transport="signed")
    if isinstance(e, x.MobileAccessDisabled):
        return CommandError("remote access is turned off in the car (Controls → Safety → Allow Mobile Access)",
                            "mobile_access_disabled", transport="signed")
    if isinstance(e, x.UnsupportedVehicle):
        return CommandError("Tesla says this vehicle doesn't support that", "not_supported", transport="signed")
    if isinstance(e, fleet.FleetError):
        return CommandError(str(e), "needs_setup", transport="signed")
    msg = getattr(e, "message", None) or str(e) or name
    return CommandError(f"the car refused: {msg}", "rejected", transport="signed", detail=name)


def _check_result(res) -> None:
    resp = res.get("response") if isinstance(res, dict) else None
    if isinstance(resp, dict) and resp.get("result") is False:
        reason = resp.get("reason") or "rejected"
        raise CommandError(f"the car refused: {reason}", "rejected", transport="signed", detail=reason)


def _lib_error() -> type[BaseException]:
    from tesla_fleet_api.exceptions import TeslaFleetError

    return TeslaFleetError


async def _wake_signed(vehicle, car_id: int, wait_s: float = 40) -> None:
    await vehicle.wake_up()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        state = (live.cars.get(str(car_id)) or {}).get("state")
        if state and state not in ("asleep", "offline", "suspended"):
            return
        try:
            info = await vehicle.vehicle()
            if ((info or {}).get("response") or {}).get("state") == "online":
                return
        # The library's errors derive from BaseException, so `except Exception`
        # alone would let them escape the polling loop.
        except (Exception, _lib_error()):
            log.debug("wake poll failed", exc_info=True)
    raise CommandError("the car didn't wake up within 40 s", "asleep", transport="signed")


async def _send_signed(car: dict, spec: catalog.Cmd, p: dict, allow_wake: bool = True) -> dict:
    from tesla_fleet_api import exceptions as x

    vin = car.get("vin")
    if not vin:
        raise CommandError("this car has no VIN on record", "unknown_car")
    vehicle = await _signed_vehicle(vin)
    method = getattr(vehicle, spec.method, None)
    if method is None:
        raise CommandError(f"{spec.id} isn't supported by this version of the signing library",
                           "not_supported", transport="signed")
    kwargs = spec.args(p)
    try:
        res = await asyncio.wait_for(method(**kwargs), timeout=25)
    except x.VehicleOffline:
        # Offline means it certainly didn't run, so one retry after waking is safe —
        # even for toggles.
        if not (spec.wake and allow_wake):
            raise CommandError("the car is asleep", "asleep", transport="signed") from None
        try:
            await _wake_signed(vehicle, car["id"])
            res = await asyncio.wait_for(method(**kwargs), timeout=25)
        except CommandError:
            raise
        except (x.TeslaFleetError, asyncio.TimeoutError, fleet.FleetError) as e:
            raise _classify(e) from e
    except (x.TeslaFleetError, asyncio.TimeoutError, fleet.FleetError) as e:
        err = _classify(e)
        if err.code == "not_paired":
            await fleet.mark_paired(vin, False)
        raise err from e
    _check_result(res)
    await fleet.mark_paired(vin, True)
    return res if isinstance(res, dict) else {"response": {"result": True}}


# ── unsigned transport (pre-2021 S/X) ────────────────────────────────────

def _rest_base() -> str:
    host = (settings.tesla_api_host or "https://owner-api.teslamotors.com").rstrip("/")
    return host if host.startswith("http") else "https://" + host


async def _send_unsigned(car: dict, spec: catalog.Cmd, p: dict, allow_wake: bool = True) -> dict:
    if not spec.rest:
        raise CommandError("this command needs signed commands (Fleet API)", "needs_setup",
                           transport="unsigned")
    token = await tokens.access_token()
    if not token:
        raise CommandError("no Tesla token — sign in first", "auth", transport="unsigned")
    if not car.get("eid"):
        raise CommandError("TeslaMate hasn't recorded this car's API id yet", "unknown_car")
    url = f"{_rest_base()}/api/1/vehicles/{car['eid']}/command/{spec.rest}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30) as cli:
            r = await cli.post(url, json=spec.body(p) or {}, headers=headers)
            if r.status_code == 408 and spec.wake and allow_wake:
                await cli.post(f"{_rest_base()}/api/1/vehicles/{car['eid']}/wake_up", headers=headers)
                for _ in range(15):
                    await asyncio.sleep(2)
                    st = (live.cars.get(str(car["id"])) or {}).get("state")
                    if st and st not in ("asleep", "offline"):
                        break
                r = await cli.post(url, json=spec.body(p) or {}, headers=headers)
    except httpx.TimeoutException as e:
        raise CommandError("no answer in time — it may have run anyway", "timeout",
                           maybe_executed=True, transport="unsigned") from e
    except httpx.HTTPError as e:
        raise CommandError(f"couldn't reach Tesla: {e.__class__.__name__}", "network",
                           transport="unsigned") from e
    if r.status_code == 401:
        tokens.invalidate_cache()
        raise CommandError("Tesla rejected the token", "auth", transport="unsigned")
    if r.status_code == 403:
        raise CommandError("this car only accepts signed commands — set up the Fleet API",
                           "needs_setup", transport="unsigned")
    if r.status_code == 408:
        raise CommandError("the car is asleep", "asleep", transport="unsigned")
    if r.status_code >= 400:
        raise CommandError(f"Tesla returned HTTP {r.status_code}", "rejected",
                           transport="unsigned", detail=r.text[:300])
    data = r.json() if r.content else {}
    resp = data.get("response") or {}
    if isinstance(resp, dict) and resp.get("result") is False:
        raise CommandError(f"the car refused: {resp.get('reason') or 'rejected'}", "rejected",
                           transport="unsigned")
    return data


# ── entry point ──────────────────────────────────────────────────────────

async def _last_location(car_id: int) -> tuple[float, float] | None:
    c = live.cars.get(str(car_id)) or {}
    with contextlib.suppress(TypeError, ValueError):
        if c.get("latitude") and c.get("longitude"):
            return float(c["latitude"]), float(c["longitude"])
    row = await db.fetchrow("SELECT latitude, longitude FROM positions WHERE car_id = $1"
                            " AND latitude IS NOT NULL ORDER BY date DESC LIMIT 1", car_id)
    return (float(row["latitude"]), float(row["longitude"])) if row else None


async def send(car_id: int, command: str, params: dict | None = None, *,
               allow_wake: bool = True) -> dict:
    """Send one command. `allow_wake=False` (the AI interface) never wakes a
    sleeping car; it gets code "asleep" instead."""
    try:
        spec, p = catalog.resolve(command, params)
    except KeyError:
        raise CommandError(f"unknown command: {command}", "unknown_command") from None
    except ValueError as e:
        raise CommandError(str(e), "invalid_param") from None
    car = await _car(int(car_id))
    if not spec.applies_to(car["model_key"]):
        raise CommandError(f"{spec.id} doesn't apply to this vehicle", "not_supported")
    if spec.needs_location:
        pos = await _last_location(car["id"])
        if pos is None:
            raise CommandError("the car's location isn't known yet", "no_location")
        p["_lat"], p["_lon"] = pos
    transport, reason = await transport_for(car)
    if transport is None:
        raise CommandError(
            "this car only accepts signed commands — finish the vehicle-command setup in Settings",
            reason)
    started = time.monotonic()
    if transport == "signed":
        await _send_signed(car, spec, p, allow_wake)
    else:
        await _send_unsigned(car, spec, p, allow_wake)
    log.info("command %s → car %s via %s in %.1fs", spec.id, car_id, transport, time.monotonic() - started)
    return {"ok": True, "command": spec.id, "transport": transport, "expect": spec.expect}
