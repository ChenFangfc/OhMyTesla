"""Tesla Fleet API: the owner's own developer app, keys and OAuth.

Every car built since 2021 — Cybertruck included — rejects commands that are
not signed by a key the car has paired. The token TeslaMate logs in with is an
Owner API token; it cannot sign anything (vehicle-command routes only
``fleet-api.*`` audiences). Remote control therefore needs, once:

1. a developer app at developer.tesla.com (client id + secret);
2. a key pair — generated here, private half never leaves this machine;
3. the public half reachable at
   ``https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem``
   (this hub serves it too, for owners whose hub has a public domain;
   otherwise a static host such as GitHub Pages works);
4. the domain registered with Tesla (a partner-token call, done here);
5. the owner's consent via OAuth (PKCE), and
6. the virtual key paired in the Tesla app (``tesla.com/_ak/<domain>``).

Commands are then signed in-process by ``tesla_fleet_api`` — no separate
signing proxy, no TLS certificates to manage.

Secrets (client secret, tokens) are stored encrypted with the same key
TeslaMate uses for its own tokens.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..alerts.store import store
from ..config import settings
from . import vault

log = logging.getLogger(__name__)

SERVERS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "cn": "https://fleet-api.prd.cn.vn.cloud.tesla.cn",
}
AUTHORIZE = {"na": "https://auth.tesla.com/oauth2/v3/authorize",
             "eu": "https://auth.tesla.com/oauth2/v3/authorize",
             "cn": "https://auth.tesla.cn/oauth2/v3/authorize"}
TOKEN = {"na": "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token",
         "eu": "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token",
         "cn": "https://auth.tesla.cn/oauth2/v3/token"}
SCOPES = "openid offline_access user_data vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds"
PUBLIC_KEY_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"
_SETTING = "fleet"
_PKCE_TTL = 15 * 60
_TIMEOUT = httpx.Timeout(20.0)


class FleetError(Exception):
    """A setup or token problem the UI can show verbatim."""


def key_dir() -> Path:
    p = Path(settings.data_dir) / "fleet"
    p.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):    # e.g. a filesystem without POSIX modes
        p.chmod(0o700)
    return p


def private_key_path() -> Path:
    return key_dir() / "private-key.pem"


def _enc(s: str | None) -> str | None:
    return base64.b64encode(vault.encrypt(s)).decode() if s else None


def _dec(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return vault.decrypt(base64.b64decode(s))
    except Exception:  # noqa: BLE001 — wrong ENCRYPTION_KEY or corrupt blob
        return None


async def _cfg() -> dict:
    return dict(await store.get_setting(_SETTING, {}) or {})


async def _save(patch: dict) -> dict:
    def apply(cur):
        cur = dict(cur or {})
        cur.update(patch)
        return cur
    return await store.mutate_setting(_SETTING, apply, {})


# ── keys ─────────────────────────────────────────────────────────────────

def ensure_keypair() -> str:
    """Create the EC P-256 key pair once; return the public key PEM."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    path = private_key_path()
    if path.is_file():
        priv = serialization.load_pem_private_key(path.read_bytes(), password=None)
    else:
        priv = ec.generate_private_key(ec.SECP256R1())
        pem = priv.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.NoEncryption())
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pem)
        tmp.chmod(0o600)
        tmp.replace(path)
        log.info("generated Fleet API command key at %s", path)
    return priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def public_key_pem() -> str | None:
    return ensure_keypair() if private_key_path().is_file() else None


def public_key_fingerprint() -> str | None:
    pem = public_key_pem()
    if not pem:
        return None
    return hashlib.sha256(pem.encode()).hexdigest()[:16]


# ── configuration ────────────────────────────────────────────────────────

def _normalise_domain(d: str) -> str:
    d = (d or "").strip().lower()
    for prefix in ("https://", "http://"):
        d = d.removeprefix(prefix)
    return d.split("/")[0]


async def configure(client_id: str, client_secret: str | None, domain: str,
                    region: str, redirect_uri: str) -> dict:
    if region not in SERVERS:
        raise FleetError("region must be na, eu or cn")
    domain = _normalise_domain(domain)
    if not domain or "." not in domain:
        raise FleetError("enter the domain that hosts your public key, e.g. yourname.github.io")
    if not client_id.strip():
        raise FleetError("client ID is required")
    if not redirect_uri.startswith(("http://", "https://")):
        raise FleetError("redirect URI must start with https:// (or http:// for a LAN address)")
    ensure_keypair()
    patch = {"client_id": client_id.strip(), "domain": domain, "region": region,
             "redirect_uri": redirect_uri.strip()}
    if client_secret:
        patch["client_secret"] = _enc(client_secret.strip())
    return await _save(patch)


async def status() -> dict:
    c = await _cfg()
    tok = c.get("token") or {}
    return {
        "configured": bool(c.get("client_id") and c.get("domain")),
        "has_secret": bool(c.get("client_secret")),
        "client_id": c.get("client_id"),
        # Until the owner saves the form, .env can suggest the domain and
        # redirect (e.g. a GitHub Pages site set up for them in advance).
        "domain": c.get("domain") or settings.tesla_fleet_domain or None,
        "region": c.get("region") or settings.tesla_fleet_region or "na",
        "redirect_uri": c.get("redirect_uri") or settings.tesla_fleet_redirect_uri or None,
        "key": bool(private_key_path().is_file()),
        "key_fingerprint": public_key_fingerprint(),
        "public_key_url": (f"https://{c.get('domain') or settings.tesla_fleet_domain}{PUBLIC_KEY_PATH}"
                           if (c.get("domain") or settings.tesla_fleet_domain) else None),
        "registered": bool(c.get("registered")),
        "registered_at": c.get("registered_at"),
        "signed_in": bool(tok.get("refresh")),
        "token_expires_at": tok.get("expires_at"),
        "scopes": tok.get("scopes"),
        "paired": c.get("paired") or {},
    }


async def reset() -> None:
    """Forget the app and tokens. The key pair is kept: re-pairing a new key in
    the car is the expensive step, so it survives a reconfiguration."""
    await store.set_setting(_SETTING, {})


# ── public key reachability & partner registration ───────────────────────

async def check_public_key() -> dict:
    c = await _cfg()
    if not c.get("domain"):
        raise FleetError("configure the domain first")
    url = f"https://{c['domain']}{PUBLIC_KEY_PATH}"
    want = (public_key_pem() or "").strip()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
            r = await cli.get(url)
    except httpx.HTTPError as e:
        return {"ok": False, "url": url, "reason": f"unreachable: {e.__class__.__name__}"}
    got = r.text.strip()
    if r.status_code != 200:
        return {"ok": False, "url": url, "reason": f"HTTP {r.status_code}"}
    if got != want:
        return {"ok": False, "url": url, "reason": "a different key is published there"}
    return {"ok": True, "url": url}


async def _partner_token(c: dict) -> str:
    secret = _dec(c.get("client_secret"))
    if not secret:
        raise FleetError("the client secret is needed to register the domain")
    region = c.get("region", "na")
    data = {"grant_type": "client_credentials", "client_id": c["client_id"], "client_secret": secret,
            "scope": "openid vehicle_device_data vehicle_cmds vehicle_charging_cmds",
            "audience": SERVERS[region]}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
        r = await cli.post(TOKEN[region], data=data)
    if r.status_code != 200:
        raise FleetError(f"Tesla refused the client credentials (HTTP {r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


async def register_partner() -> dict:
    """Tell Tesla which domain hosts this app's public key (once per region)."""
    c = await _cfg()
    if not c.get("client_id"):
        raise FleetError("configure the app first")
    pk = await check_public_key()
    if not pk["ok"]:
        raise FleetError(f"Tesla will fetch {pk['url']} and it is not ready yet ({pk['reason']})")
    token = await _partner_token(c)
    region = c.get("region", "na")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
        r = await cli.post(f"{SERVERS[region]}/api/1/partner_accounts",
                           json={"domain": c["domain"]},
                           headers={"Authorization": f"Bearer {token}"})
    if r.status_code not in (200, 201):
        raise FleetError(f"registration failed (HTTP {r.status_code}): {r.text[:300]}")
    await _save({"registered": True, "registered_at": int(time.time())})
    return {"ok": True}


# ── OAuth (authorization code + PKCE) ────────────────────────────────────

async def login_url() -> str:
    c = await _cfg()
    if not c.get("client_id") or not c.get("redirect_uri"):
        raise FleetError("configure the app first")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(18)
    await _save({"pkce": {"verifier": verifier, "state": state, "created": int(time.time())}})
    q = {
        "response_type": "code", "client_id": c["client_id"], "redirect_uri": c["redirect_uri"],
        "scope": SCOPES, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "prompt_missing_scopes": "true",
    }
    return f"{AUTHORIZE[c.get('region', 'na')]}?{urlencode(q)}"


def _extract_code(code_or_url: str) -> tuple[str, str | None]:
    s = (code_or_url or "").strip()
    if s.startswith(("http://", "https://")) or "?" in s:
        q = parse_qs(urlsplit(s).query)
        if "error" in q:
            raise FleetError(f"Tesla returned an error: {q.get('error_description', q['error'])[0]}")
        code = (q.get("code") or [""])[0]
        return code, (q.get("state") or [None])[0]
    return s, None


async def finish_login(code_or_url: str, state: str | None = None) -> dict:
    """Exchange the authorization code (or the whole redirect URL, pasted) for tokens."""
    c = await _cfg()
    pk = c.get("pkce") or {}
    code, url_state = _extract_code(code_or_url)
    state = state or url_state
    if not code:
        raise FleetError("no authorization code found — paste the full address you landed on")
    if not pk or time.time() - pk.get("created", 0) > _PKCE_TTL:
        raise FleetError("the login link expired — start the sign-in again")
    if state is not None and state != pk.get("state"):
        raise FleetError("this sign-in did not start here (state mismatch) — start it again")
    region = c.get("region", "na")
    data = {"grant_type": "authorization_code", "client_id": c["client_id"], "code": code,
            "redirect_uri": c["redirect_uri"], "code_verifier": pk["verifier"],
            "audience": SERVERS[region]}
    secret = _dec(c.get("client_secret"))
    if secret:
        data["client_secret"] = secret
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
        r = await cli.post(TOKEN[region], data=data)
    if r.status_code != 200:
        raise FleetError(f"Tesla rejected the code (HTTP {r.status_code}): {r.text[:200]}")
    await _store_tokens(r.json())
    await _save({"pkce": None})
    return await status()


async def _store_tokens(tok: dict) -> None:
    c = await _cfg()
    old = c.get("token") or {}
    refresh = tok.get("refresh_token") or _dec(old.get("refresh"))   # Tesla may omit it on refresh
    await _save({"token": {
        "access": _enc(tok["access_token"]),
        "refresh": _enc(refresh),
        "expires_at": int(time.time()) + int(tok.get("expires_in", 28800)),
        "scopes": tok.get("scope") or old.get("scopes"),
    }})


async def access_token() -> str:
    """A valid Fleet access token, refreshed when within 10 minutes of expiry."""
    c = await _cfg()
    tok = c.get("token") or {}
    access = _dec(tok.get("access"))
    if access and time.time() < tok.get("expires_at", 0) - 600:
        return access
    refresh = _dec(tok.get("refresh"))
    if not refresh:
        raise FleetError("not signed in to the Fleet API")
    region = c.get("region", "na")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
        r = await cli.post(TOKEN[region], data={"grant_type": "refresh_token",
                                                "client_id": c["client_id"], "refresh_token": refresh})
    if r.status_code != 200:
        body = r.text[:200]
        if "login_required" in body or "invalid_grant" in body:
            await _save({"token": None})
            raise FleetError("the Fleet API sign-in was revoked — sign in again")
        raise FleetError(f"token refresh failed (HTTP {r.status_code})")
    await _store_tokens(r.json())
    return r.json()["access_token"]


async def region() -> str:
    return (await _cfg()).get("region", "na")


# ── virtual key pairing ─────────────────────────────────────────────────

async def pairing_link(vin: str | None = None) -> str:
    c = await _cfg()
    if not c.get("domain"):
        raise FleetError("configure the domain first")
    base = f"https://tesla.com/_ak/{c['domain']}"
    return f"{base}?vin={vin}" if vin else base


async def fleet_status(vins: list[str]) -> dict:
    """Ask Tesla which of these cars have our key paired (cheap, no wake)."""
    token = await access_token()
    r_ = await region()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
        r = await cli.post(f"{SERVERS[r_]}/api/1/vehicles/fleet_status", json={"vins": vins},
                           headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise FleetError(f"fleet_status failed (HTTP {r.status_code}): {r.text[:200]}")
    resp = r.json().get("response") or {}
    paired = set(resp.get("key_paired_vins") or [])
    now = int(time.time())
    if paired:
        cur = (await _cfg()).get("paired") or {}
        cur.update({v: cur.get(v) or now for v in paired})
        await _save({"paired": cur})
    return {"paired": sorted(paired), "unpaired": sorted(resp.get("unpaired_vins") or []),
            "details": resp.get("vehicle_info") or {}}


async def mark_paired(vin: str, ok: bool) -> None:
    cur = dict((await _cfg()).get("paired") or {})
    if ok:
        cur.setdefault(vin, int(time.time()))
    else:
        cur.pop(vin, None)
    await _save({"paired": cur})


async def ready() -> bool:
    c = await _cfg()
    return bool(c.get("client_id") and (c.get("token") or {}).get("refresh") and private_key_path().is_file())


def export_state_for_backup() -> str:
    """For `omt backup`: the key file path, so it is included (it is not in hub.db)."""
    return json.dumps({"private_key": str(private_key_path())})
