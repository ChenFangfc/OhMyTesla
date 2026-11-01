import functools
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .. import db
from ..alerts.store import store
from ..config import settings
from . import catalog, commands, fleet, tokens

router = APIRouter(prefix="/api/tesla", tags=["tesla"])
# Browser redirect target for the Fleet API sign-in, and the public key Tesla
# fetches — both outside /api.
public_router = APIRouter(tags=["tesla"])

_last_cmd: dict[tuple[int, str], float] = {}
_COOLDOWN_S = 3  # 防手抖连点

# Command error code -> HTTP status. Never 401: the browser would take it as
# the portal's Basic-auth challenge and pop a login dialog.
_STATUS = {
    "unknown_command": 400, "invalid_param": 400, "unknown_car": 404, "not_supported": 422,
    "needs_setup": 409, "not_paired": 409, "auth": 409, "mobile_access_disabled": 409,
    "no_location": 409, "asleep": 503, "network": 502, "rejected": 502, "timeout": 504,
    "rate_limited": 429, "billing": 402,
}


def _prune_cooldown(now: float) -> None:
    if len(_last_cmd) > 500:
        for k in [k for k, t in _last_cmd.items() if now - t > _COOLDOWN_S * 10]:
            _last_cmd.pop(k, None)


@router.get("/status")
async def status():
    src = await tokens.token_source()
    return {
        "token_source": src,
        "ready": src not in ("none", "db_down"),
        "api_host": settings.tesla_api_host,
        "fleet": await fleet.status(),
        "commands": list(commands.COMMANDS.values()),
    }


@router.get("/capabilities")
async def capabilities(car_id: int):
    """What this car can be told to do right now, and why not if it can't."""
    try:
        return await commands.capabilities(car_id)
    except commands.CommandError as e:
        return JSONResponse(e.as_dict(), status_code=_STATUS.get(e.code, 400))


@router.get("/catalog")
async def command_catalog(model: str | None = None):
    cmds = catalog.for_model(model) if model else catalog.COMMANDS.values()
    return [c.describe() for c in cmds]


class TokenBody(BaseModel):
    token: str = ""


@router.post("/token")
async def set_token(body: TokenBody):
    await tokens.set_manual_token(body.token)
    return {"ok": True, "source": await tokens.token_source()}


class CommandBody(BaseModel):
    car_id: int
    command: str
    params: dict | None = None
    confirm: bool = False


@router.post("/command")
async def send_command(body: CommandBody):
    try:
        spec, _ = catalog.resolve(body.command, body.params)
    except KeyError:
        raise HTTPException(400, "Unknown command") from None
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e), "code": "invalid_param"}, status_code=400)
    if spec.risk >= 1 and not body.confirm:
        return JSONResponse({"ok": False, "error": "This action needs confirmation", "code": "confirm",
                             "risk": spec.risk}, status_code=428)

    key = (body.car_id, spec.id)
    now = time.time()
    if now - _last_cmd.get(key, 0) < _COOLDOWN_S:
        return JSONResponse({"ok": False, "error": "Too fast — wait a moment", "code": "cooldown"},
                            status_code=429)
    _prune_cooldown(now)
    _last_cmd[key] = now

    # 每一条指令都留痕：这个接口能解锁车、开备箱、关哨兵，
    # 出了事必须能回溯"什么时候谁下了什么、成没成"。
    started = time.time()
    try:
        result = await commands.send(body.car_id, body.command, body.params)
    except commands.CommandError as e:
        await store.log_command(str(body.car_id), spec.id, body.params, "api", False,
                                f"{e.code}: {e}", int((time.time() - started) * 1000))
        return JSONResponse(e.as_dict(), status_code=_STATUS.get(e.code, 502))
    except db.DbUnavailable as e:
        await store.log_command(str(body.car_id), spec.id, body.params, "api", False, str(e),
                                int((time.time() - started) * 1000))
        return JSONResponse({"ok": False, "error": "database unavailable", "code": "db_down"},
                            status_code=503)
    await store.log_command(str(body.car_id), spec.id, body.params, "api", True, "",
                            int((time.time() - started) * 1000))
    return result


@router.get("/commands/log")
async def command_log(limit: int = 100):
    """指令流水。下发失败的原因也在里面 —— 排查"我点了没反应"时唯一有用的东西。"""
    return {"commands": await store.recent_commands(max(1, min(limit, 500)))}


# ── Fleet API setup ──────────────────────────────────────────────────────

def _fleet_errors(fn):
    # functools.wraps keeps the endpoint's signature visible to FastAPI;
    # without it every wrapped route demands bogus `a`/`kw` query params.
    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except fleet.FleetError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
    return wrapper


@router.get("/fleet")
async def fleet_status():
    return await fleet.status()


class FleetConfig(BaseModel):
    client_id: str
    client_secret: str | None = None
    domain: str
    region: str = "na"
    redirect_uri: str


@router.post("/fleet/config")
@_fleet_errors
async def fleet_config(body: FleetConfig):
    await fleet.configure(body.client_id, body.client_secret, body.domain, body.region, body.redirect_uri)
    await commands.reset_signed()
    return await fleet.status()


@router.post("/fleet/key")
async def fleet_key():
    pem = fleet.ensure_keypair()
    return {"public_key": pem, "fingerprint": fleet.public_key_fingerprint(),
            "path": fleet.PUBLIC_KEY_PATH}


@router.get("/fleet/public-key.pem")
async def fleet_public_key_download():
    pem = fleet.public_key_pem()
    if not pem:
        raise HTTPException(404, "no key yet")
    return PlainTextResponse(pem, media_type="application/x-pem-file",
                             headers={"Content-Disposition": 'attachment; filename="com.tesla.3p.public-key.pem"'})


@router.get("/fleet/check-key")
@_fleet_errors
async def fleet_check_key():
    return await fleet.check_public_key()


@router.post("/fleet/register")
@_fleet_errors
async def fleet_register():
    return await fleet.register_partner()


@router.post("/fleet/login")
@_fleet_errors
async def fleet_login():
    return {"url": await fleet.login_url()}


class FleetCallback(BaseModel):
    code_or_url: str
    state: str | None = None


@router.post("/fleet/callback")
@_fleet_errors
async def fleet_callback(body: FleetCallback):
    st = await fleet.finish_login(body.code_or_url, body.state)
    await commands.reset_signed()
    return st


@router.get("/fleet/pairing")
@_fleet_errors
async def fleet_pairing(car_id: int):
    row = await db.fetchrow("SELECT vin FROM cars WHERE id = $1", car_id)
    if row is None:
        raise HTTPException(404, "no such car")
    return {"url": await fleet.pairing_link(row["vin"]), "vin": row["vin"]}


@router.post("/fleet/check-pairing")
@_fleet_errors
async def fleet_check_pairing():
    rows = await db.fetch("SELECT vin FROM cars WHERE vin IS NOT NULL")
    return await fleet.fleet_status([r["vin"] for r in rows])


@router.post("/fleet/reset")
async def fleet_reset():
    await fleet.reset()
    await commands.reset_signed()
    return {"ok": True}


@public_router.get(fleet.PUBLIC_KEY_PATH)
async def well_known_public_key():
    """Tesla fetches this when the hub itself is the app's domain. Public on
    purpose (it is a public key); 404 until a key exists."""
    pem = fleet.public_key_pem()
    if not pem:
        raise HTTPException(404)
    return PlainTextResponse(pem, media_type="application/x-pem-file")


@public_router.get("/fleet/callback", response_class=HTMLResponse)
async def fleet_callback_page():
    """Where Tesla sends the browser after sign-in. Hands the code to the API
    and returns to Settings; if this page is unreachable from where you signed
    in, paste its address into Settings instead."""
    return HTMLResponse("""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Connecting…</title><body style="background:#0b0c0e;color:#e8e9eb;font:15px -apple-system,system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0">
<div id="m">Connecting your Tesla…</div>
<script>
fetch("/api/tesla/fleet/callback",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({code_or_url: location.href})})
 .then(r=>r.json().then(j=>({ok:r.ok,j})))
 .then(({ok,j})=>{ if(ok){ location.replace("/settings#commands"); }
   else { document.getElementById("m").textContent = j.error || "Sign-in failed"; } })
 .catch(()=>{ document.getElementById("m").textContent = "Sign-in failed"; });
</script></body>""")
