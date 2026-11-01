"""Notification channel configuration: `.env` gives defaults, Settings overrides.

A desktop install (the macOS app) has no `.env` anyone will edit, so every
channel must be configurable from the UI. Values set there are stored in the
hub's settings table — secrets encrypted with the same key that protects the
Tesla tokens — and win over the environment. Clearing a channel in the UI
falls back to whatever `.env` says, so a Docker install keeps working
untouched.
"""

from __future__ import annotations

import base64
import secrets

from ..config import settings
from ..tesla import vault
from .store import store

_KEY = "channels"

# channel -> (fields, secret fields)
SCHEMA: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "ntfy": (("url", "topic", "token"), ("token",)),
    "bark": (("server", "key", "critical"), ("key",)),
    "telegram": (("token", "chat_id"), ("token",)),
    "email": (("host", "port", "user", "password", "sender", "to"), ("password",)),
    "webhook": (("url",), ("url",)),     # the URL usually *is* the key (DingTalk, WeCom, Slack…)
    "apprise": (("urls",), ("urls",)),
}

_PUBLIC_MAIL = ("gmail.com", "qq.com", "163.com", "outlook.com", "office365.com", "yahoo.com",
                "126.com", "icloud.com", "zoho.com", "sendgrid.net")


def _from_env() -> dict[str, dict]:
    s = settings
    return {
        "ntfy": {"url": s.ntfy_url or "https://ntfy.sh", "topic": s.ntfy_topic, "token": ""},
        "bark": {"server": "https://api.day.app", "key": "", "critical": True},
        "telegram": {"token": s.telegram_bot_token, "chat_id": s.telegram_chat_id},
        "email": {"host": s.smtp_host, "port": s.smtp_port, "user": s.smtp_user,
                  "password": s.smtp_pass, "sender": s.smtp_from, "to": s.smtp_to},
        "webhook": {"url": s.alert_webhook_url},
        "apprise": {"urls": s.apprise_urls},
    }


def _enc(v: str) -> str:
    return "enc:" + base64.b64encode(vault.encrypt(v)).decode()


def _dec(v):
    if isinstance(v, str) and v.startswith("enc:"):
        try:
            return vault.decrypt(base64.b64decode(v[4:])) or ""
        except Exception:  # noqa: BLE001 — key rotated: treat as unset
            return ""
    return v


async def _stored() -> dict:
    return dict(await store.get_setting(_KEY, {}) or {})


async def config() -> dict[str, dict]:
    """Effective configuration per channel (secrets decrypted, UI over env)."""
    env = _from_env()
    ui = await _stored()
    out = {}
    for name, base in env.items():
        mine = ui.get(name) or {}
        merged = dict(base)
        if mine.get("_set"):
            merged.update({k: _dec(v) for k, v in mine.items() if not k.startswith("_")})
            merged["_source"] = "settings"
        else:
            merged["_source"] = "env" if any(v for k, v in base.items()
                                             if k not in ("url", "server", "critical", "port")) else ""
        merged["_disabled"] = bool(mine.get("_disabled"))
        out[name] = merged
    return out


def problem(name: str, c: dict) -> str:
    """'' when the channel can send; otherwise what's missing (UI shows it)."""
    if name == "ntfy":
        return "" if c.get("topic") else "missing_topic"
    if name == "bark":
        return "" if c.get("key") else "missing_key"
    if name == "telegram":
        return "" if c.get("token") and c.get("chat_id") else "missing_token"
    if name == "email":
        if not c.get("host"):
            return "missing_host"
        if not c.get("to"):
            return "missing_to"
        needs_auth = any(str(c["host"]).endswith(d) for d in _PUBLIC_MAIL)
        if needs_auth and not (c.get("user") and c.get("password")):
            return "missing_password" if c.get("user") else "missing_user"
        return ""
    if name == "webhook":
        return "" if c.get("url") else "missing_url"
    if name == "apprise":
        return "" if c.get("urls") else "missing_urls"
    return "unknown"


async def active() -> dict[str, dict]:
    """Channels that are configured, complete and not switched off."""
    return {n: c for n, c in (await config()).items()
            if not c.get("_disabled") and not problem(n, c)}


async def describe() -> list[dict]:
    """For Settings: what each channel is, whether it works, never the secrets."""
    out = []
    for name, c in (await config()).items():
        secret = SCHEMA[name][1]
        fields = {k: ("" if k in secret and c.get(k) else c.get(k)) for k in SCHEMA[name][0]}
        for k in secret:
            fields[k + "_set"] = bool(c.get(k))
        p = problem(name, c)
        configured = p not in ("missing_topic", "missing_key", "missing_token", "missing_host",
                               "missing_url", "missing_urls")
        out.append({"name": name, "fields": fields, "source": c.get("_source") or None,
                    "configured": configured, "problem": p, "enabled": not c.get("_disabled"),
                    "ready": not p and not c.get("_disabled")})
    return out


async def save(name: str, values: dict) -> None:
    if name not in SCHEMA:
        raise ValueError("unknown channel")
    fields, secret = SCHEMA[name]
    cur = await _stored()
    old = cur.get(name) or {}
    new: dict = {"_set": True, "_disabled": bool(values.get("_disabled", old.get("_disabled")))}
    for k in fields:
        if k not in values:
            if k in old:
                new[k] = old[k]
            continue
        v = values[k]
        if k in secret:
            # An empty secret field in the form means "unchanged", not "clear".
            if v in (None, ""):
                if k in old:
                    new[k] = old[k]
                continue
            new[k] = _enc(str(v))
        elif k == "critical":
            new[k] = bool(v)
        elif k == "port":
            new[k] = int(v or 587)
        else:
            new[k] = str(v or "").strip()
    cur[name] = new
    await store.set_setting(_KEY, cur)


async def set_enabled(name: str, enabled: bool) -> None:
    cur = await _stored()
    entry = dict(cur.get(name) or {})
    entry["_disabled"] = not enabled
    cur[name] = entry
    await store.set_setting(_KEY, cur)


async def reset(name: str) -> None:
    cur = await _stored()
    cur.pop(name, None)
    await store.set_setting(_KEY, cur)


def new_ntfy_topic() -> str:
    """An unguessable topic. On a public ntfy server the topic *is* the password."""
    return "omt-" + secrets.token_hex(10)
