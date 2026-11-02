"""Deliver one alert to every configured channel, independently.

Each channel is sent on its own and reported on its own. The previous design
pushed everything through a single Apprise call, so one broken target (an
email with a wrong password) marked the whole alert undelivered — and then
the cooldown never applied and the next alert re-sent to everyone (audit F9).

Richness per channel, because a sentry alarm is not a newsletter:

* ntfy — JSON publish (UTF-8 titles), priority 5 for critical, a tap target
  that opens the event, and the clip thumbnail when there is one.
* Bark (iOS) — ``level=critical`` rings through silent mode and Do Not
  Disturb; that is the only way to get that on iPhone without an App Store app.
* Urgent alerts (``payload["urgent"]``: someone at the car right now) are
  time-sensitive everywhere: ntfy priority at least 4 (the ntfy iOS app maps
  4 to ``.timeSensitive``, which passes a Focus mode; 3 is ``.active``), Bark
  ``timeSensitive``, Web Push ``Urgency: high``.
* Icons: Bark (iOS 15+) and ntfy on Android show our mark. The ntfy iOS app
  has no custom icons; it shows its own. Titles carry their own emoji, so ntfy
  gets no emoji tag — it would put a second one in front.
* Telegram — photo + caption when a thumbnail exists.
* Web Push (PWA) — see webpush.py.
* Email and anything else Apprise understands — archival channels.

Critical alerts that failed on a channel are retried in the background
(15 s, 60 s, 180 s) — a missed alarm is the one failure this product exists
to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
from base64 import b64encode
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from . import channels as chan

log = logging.getLogger(__name__)

_PRIORITY = {"critical": 5, "warning": 4, "info": 3}
_TAGS = {"critical": "rotating_light", "warning": "warning", "info": "information_source"}
_RETRY_DELAYS = (15, 60, 180)
_TIMEOUT = httpx.Timeout(15.0)
# The phone fetches the icon itself, so it must be public: the repository's copy.
_ICON_URL = "https://raw.githubusercontent.com/ChenFangfc/OhMyTesla/main/services/hub/app/web/static/icon-512.png"


def _tags(title: str, level: str) -> list[str]:
    """ntfy turns emoji tags into a prefix on the title; a title that already
    starts with its own emoji gets none."""
    if title[:1] and unicodedata.category(title[0]) == "So":
        return []
    return [_TAGS.get(level, "")]


async def configured_channels() -> list[str]:
    names = list(await chan.active())
    try:
        from . import webpush

        if await webpush.count():
            names.append("webpush")
    except Exception:
        log.debug("suppressed", exc_info=True)
    return names


async def email_config_problem() -> str:
    c = (await chan.config())["email"]
    return chan.problem("email", c) if c.get("host") else ""


def _rfc2047(s: str) -> str:
    """HTTP headers are Latin-1; ntfy accepts RFC 2047 for anything else."""
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + b64encode(s.encode()).decode() + "?="


# ── channel senders: each returns True on confirmed acceptance ───────────

async def _read(path: str | None) -> tuple[str, bytes] | None:
    """Attachment bytes, read off the event loop (or None if there isn't one)."""
    if not path:
        return None

    def load():
        p = Path(path)
        return (p.name, p.read_bytes()) if p.is_file() else None
    return await asyncio.to_thread(load)


async def _ntfy(c: dict, title: str, body: str, level: str, url: str | None,
                attach: str | None, map_url: str | None = None, urgent: bool = False) -> bool:
    prio = max(_PRIORITY.get(level, 3), 4 if urgent else 0)
    base = (c.get("url") or "https://ntfy.sh").rstrip("/")
    if "://" not in base:
        base = "https://" + base
    headers = {}
    if c.get("token"):
        headers["Authorization"] = f"Bearer {c['token']}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
        file = await _read(attach)
        if file:
            # Attachment publish: the file is the body; text goes in headers.
            h = dict(headers)
            h.update({"Title": _rfc2047(title), "Message": _rfc2047(body.replace("\n", "\\n")),
                      "Priority": str(prio), "Icon": _ICON_URL, "Filename": file[0]})
            if tags := _tags(title, level):
                h["Tags"] = ",".join(tags)
            if url:
                h["Click"] = url
            r = await cli.put(f"{base}/{c['topic']}", content=file[1], headers=h)
        else:
            msg = {"topic": c["topic"], "title": title, "message": body,
                   "priority": prio, "tags": _tags(title, level), "icon": _ICON_URL}
            actions = []
            if url:
                msg["click"] = url
                actions.append({"action": "view", "label": "Open", "url": url})
            if map_url:
                actions.append({"action": "view", "label": "Map", "url": map_url})
            if actions:
                msg["actions"] = actions
            r = await cli.post(base, json=msg, headers=headers)
    if r.status_code >= 400:
        log.warning("ntfy rejected the alert: HTTP %s %s", r.status_code, r.text[:160])
    return r.status_code < 400


async def _bark(c: dict, title: str, body: str, level: str, url: str | None,
                urgent: bool = False) -> bool:
    server = (c.get("server") or "https://api.day.app").rstrip("/")
    payload = {"device_key": c["key"], "title": title, "body": body, "group": "OhmyTesla",
               "icon": c.get("icon") or _ICON_URL}
    if level == "critical" and c.get("critical", True):
        payload.update({"level": "critical", "volume": 8, "sound": "alarm"})
    elif level == "warning" or urgent:
        payload["level"] = "timeSensitive"
    if url:
        payload["url"] = url
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
        r = await cli.post(f"{server}/push", json=payload)
    ok = r.status_code < 400 and (r.json().get("code") == 200 if r.content else True)
    if not ok:
        log.warning("bark rejected the alert: HTTP %s", r.status_code)
    return ok


async def _telegram(c: dict, title: str, body: str, attach: str | None) -> bool:
    api = f"https://api.telegram.org/bot{c['token']}"
    text = f"{title}\n{body}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
        file = await _read(attach)
        if file:
            r = await cli.post(f"{api}/sendPhoto", data={"chat_id": c["chat_id"], "caption": text[:1000]},
                               files={"photo": file})
        else:
            r = await cli.post(f"{api}/sendMessage", json={"chat_id": c["chat_id"], "text": text[:4000],
                                                           "disable_web_page_preview": True})
    if r.status_code >= 400:
        # Don't log r.text: Telegram echoes the request, and the URL carries the token.
        log.warning("telegram rejected the alert: HTTP %s", r.status_code)
    return r.status_code < 400


async def _webhook(c: dict, title: str, body: str, payload: dict) -> bool:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as cli:
        r = await cli.post(c["url"], json={"title": title, "body": body, **payload})
    return r.is_success


def _apprise_sync(urls: list[str], title: str, body: str, attach: str | None) -> bool:
    import apprise

    ap = apprise.Apprise()
    for u in urls:
        ap.add(u)
    kwargs = {"attach": attach} if attach and os.path.isfile(attach) else {}
    return bool(ap.notify(title=title, body=body, **kwargs))


def _email_url(c: dict) -> str:
    port = int(c.get("port") or 587)
    scheme = "mailtos" if port in (465, 587) else "mailto"
    user = quote(str(c.get("user") or ""), safe="")
    pw = quote(str(c.get("password") or ""), safe="")
    auth = f"{user}:{pw}@" if user else ""
    url = f"{scheme}://{auth}{c['host']}:{port}/?to={quote(c['to'], safe='')}"
    if c.get("sender"):
        url += f"&from={quote(c['sender'], safe='')}"
    if port == 465:
        url += "&mode=ssl"
    elif port == 587:
        url += "&mode=starttls"
    return url


async def _send_one(name: str, c: dict, title: str, body: str, level: str, payload: dict,
                    url: str | None, attach: str | None) -> bool:
    try:
        if name == "ntfy":
            return await _ntfy(c, title, body, level, url, attach, payload.get("map_url"),
                               urgent=bool(payload.get("urgent")))
        if name == "bark":
            return await _bark(c, title, body, level, url, urgent=bool(payload.get("urgent")))
        if name == "telegram":
            return await _telegram(c, title, body, attach)
        if name == "webhook":
            return await _webhook(c, title, body, payload)
        if name == "email":
            return await asyncio.to_thread(_apprise_sync, [_email_url(c)], title, body, attach)
        if name == "apprise":
            urls = [u.strip() for u in str(c.get("urls") or "").split(",") if u.strip()]
            return await asyncio.to_thread(_apprise_sync, urls, title, body, attach)
    except Exception as e:  # noqa: BLE001 — one channel failing must not affect the others
        log.warning("%s delivery failed: %s", name, e.__class__.__name__)
    return False


def _absolute(url: str | None) -> str | None:
    """Phones can only open absolute links."""
    if not url:
        return None
    if urlparse(url).scheme:
        return url
    from ..config import settings

    base = (settings.public_url or "").rstrip("/")
    return f"{base}{url}" if base else None


async def send(title: str, body: str, payload: dict | None = None, attach: str | None = None,
               *, level: str | None = None, url: str | None = None,
               only: list[str] | None = None) -> tuple[bool, list[str]]:
    """Send to every active channel. Returns (any delivered, [channel names that delivered]).

    ``only`` restricts delivery to those channels (used by per-channel tests).
    """
    payload = dict(payload or {})
    level = level or payload.get("level") or "info"
    link = _absolute(url)
    active = await chan.active()
    if only is not None:
        active = {k: v for k, v in active.items() if k in only}

    names = list(active)
    tasks = [_send_one(n, c, title, body, level, payload, link, attach) for n, c in active.items()]

    want_webpush = (only is None or "webpush" in only)
    if want_webpush:
        from . import webpush

        if await webpush.count():
            names.append("webpush")
            tasks.append(webpush.send(title, body, url=url or "/security", level=level,
                                      tag=payload.get("tag"), urgent=bool(payload.get("urgent"))))
    if not tasks:
        log.warning("no notification channel configured; alert dropped: %s", title)
        return False, []

    results = await asyncio.gather(*tasks, return_exceptions=True)
    delivered = [n for n, r in zip(names, results, strict=True) if r is True]
    failed = [n for n, r in zip(names, results, strict=True) if r is not True]
    if failed and level == "critical":
        asyncio.get_running_loop().create_task(
            _retry(failed, title, body, level, payload, link, attach, active))
    return bool(delivered), delivered


async def _retry(failed: list[str], title: str, body: str, level: str, payload: dict,
                 link: str | None, attach: str | None, active: dict) -> None:
    pending = list(failed)
    for delay in _RETRY_DELAYS:
        await asyncio.sleep(delay)
        still = []
        for n in pending:
            if n == "webpush":
                from . import webpush

                ok = await webpush.send(title, body, url=link or "/security", level=level,
                                        tag=payload.get("tag"), urgent=bool(payload.get("urgent")))
            else:
                ok = await _send_one(n, active[n], title, body, level, payload, link, attach)
            if not ok:
                still.append(n)
            else:
                log.info("critical alert delivered to %s on retry", n)
        pending = still
        if not pending:
            return
    log.error("critical alert never reached: %s", ", ".join(pending))


async def verify_ntfy(marker: str, wait_s: float = 8.0) -> bool:
    """Read the topic back to prove a test message really arrived on the server."""
    c = (await chan.config())["ntfy"]
    if chan.problem("ntfy", c):
        return False
    base = (c.get("url") or "https://ntfy.sh").rstrip("/")
    headers = {"Authorization": f"Bearer {c['token']}"} if c.get("token") else {}
    deadline = asyncio.get_running_loop().time() + wait_s
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
        while asyncio.get_running_loop().time() < deadline:
            r = await cli.get(f"{base}/{c['topic']}/json", params={"poll": "1", "since": "60s"},
                              headers=headers)
            if r.status_code == 200 and marker in r.text:
                return True
            await asyncio.sleep(1.5)
    return False
