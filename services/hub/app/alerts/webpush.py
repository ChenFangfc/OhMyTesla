"""Web Push（PWA 推送）——自生成 VAPID 密钥，无需任何第三方账号。

iOS 需 16.4+ 且必须把网页「添加到主屏幕」后才能收到推送（Safari 限制）；
Android/桌面 Chrome 直接可用。
"""

import asyncio
import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .store import store

log = logging.getLogger(__name__)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


async def keys() -> dict:
    """取（必要时生成）VAPID 密钥对，持久化在 hub 自有库里。"""
    saved = await store.get_setting("vapid")
    if saved:
        return saved

    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    data = {"private_pem": priv_pem, "public_key": _b64(pub_raw)}
    await store.set_setting("vapid", data)
    log.info("generated new VAPID keypair")
    return data


async def public_key() -> str:
    return (await keys())["public_key"]


MAX_DEVICES = 20


def _validate_endpoint(endpoint: str) -> None:
    """推送端点必须是公网 https。

    这个地址会被服务端主动 POST，等于把 hub 变成一个受控的 HTTP 客户端：
    不校验的话，任何能调到这个接口的人都可以让 hub 去打 http://127.0.0.1:...
    或内网地址，用返回耗时/错误码探测内网 —— 而 hub 恰好和 Postgres、
    mosquitto、TeslaMate 在同一个 compose 网络里。
    """
    from ipaddress import ip_address
    from urllib.parse import urlparse

    u = urlparse(endpoint)
    if u.scheme != "https":
        raise ValueError("push endpoint must use https")
    host = u.hostname
    if not host:
        raise ValueError("push endpoint has no host")
    try:
        ip = ip_address(host)
    except ValueError:
        # 是域名。真实的推送服务（FCM / Mozilla / Apple）都是公网域名，
        # 这里不做 DNS 解析：解析结果可能在校验后变化（DNS rebinding），
        # 拿它当安全依据是假的安全感。域名一律放行，IP 字面量才卡。
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValueError("push endpoint must not point at a private address")


async def subscribe(subscription: dict) -> None:
    endpoint = subscription.get("endpoint")
    if not endpoint:
        raise ValueError("subscription missing endpoint")
    _validate_endpoint(endpoint)

    # 两台设备同时订阅时，不持锁的读-改-写会让其中一台悄悄消失 ——
    # 而"没收到告警"是这个产品最不该有的失败方式。
    err: list[str] = []

    def apply(subs):
        subs = list(subs or [])
        # 先判断这台设备是不是已经在册。不能先移除再判满 —— 那样一台已注册
        # 的设备在满员时重新订阅（换了浏览器、清了缓存都会触发），会被先删掉
        # 再因为"满了"拒绝加回来，等于把自己踢下线。
        known = any(x.get("endpoint") == endpoint for x in subs)
        if not known and len(subs) >= MAX_DEVICES:
            err.append("full")
            return subs
        return [*(x for x in subs if x.get("endpoint") != endpoint), subscription]

    await store.mutate_setting("push_subs", apply, [])
    if err:
        # 原来是 `subs[-20:]` 静默淘汰最旧的。那意味着攻击者（或一个循环
        # 调用的脚本）灌 20 条就能把车主自己的手机悄悄挤掉，之后所有告警
        # 都推给别人而车主毫无察觉。满了就明确拒绝，让人自己去删。
        raise ValueError(
            f"already at {MAX_DEVICES} registered devices; remove one before adding another")


async def unsubscribe(endpoint: str) -> None:
    subs = await store.get_setting("push_subs", []) or []
    await store.set_setting("push_subs", [s for s in subs if s.get("endpoint") != endpoint])


async def count() -> int:
    return len(await store.get_setting("push_subs", []) or [])


# How long the push service keeps an undelivered message, and how urgently it
# wakes the device. pywebpush's defaults (TTL 0, no Urgency) mean a phone in
# Doze or briefly offline simply never sees the alert (audit F5).
_TTL = {"critical": 6 * 3600, "warning": 3600, "info": 1800}
_URGENCY = {"critical": "high", "warning": "high", "info": "normal"}


def _send_sync(sub: dict, payload: str, priv_pem: str, level: str = "info") -> tuple[bool, int]:
    from py_vapid import Vapid01
    from pywebpush import WebPushException, webpush

    try:
        # 必须传 Vapid 实例：pywebpush 对字符串走 Vapid.from_string，
        # 那条路解不了 PKCS8 PEM，会导致推送全部静默失败
        vapid = Vapid01.from_pem(priv_pem.encode())
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=vapid,
            vapid_claims={"sub": "mailto:ohmytesla@localhost"},
            ttl=_TTL.get(level, 1800),
            headers={"Urgency": _URGENCY.get(level, "normal")},
            timeout=15,
        )
        return True, 200
    except WebPushException as e:
        code = getattr(e.response, "status_code", 0) if e.response is not None else 0
        log.warning("web push failed (%s): %s", code, str(e)[:200])
        return False, code
    except Exception as e:  # noqa: BLE001
        log.warning("web push error: %s", str(e)[:200])
        return False, 0


async def send(title: str, body: str, url: str = "/", level: str = "info",
               tag: str | None = None) -> bool:
    subs = await store.get_setting("push_subs", []) or []
    if not subs:
        return False
    kp = await keys()
    # `tag` groups the pushes of one sentry incident: an escalation replaces the
    # earlier notification (and re-alerts) instead of stacking a second one.
    payload = json.dumps(
        {"title": title, "body": body, "url": url, "level": level, "tag": tag},
        ensure_ascii=False,
    )

    results = await asyncio.gather(
        *[asyncio.to_thread(_send_sync, s, payload, kp["private_pem"], level) for s in subs],
        return_exceptions=True,
    )
    ok_any = False
    dead = []
    for sub, res in zip(subs, results, strict=False):
        if isinstance(res, Exception):
            continue
        ok, code = res
        ok_any = ok_any or ok
        if code in (404, 410):  # 订阅已失效，清理
            dead.append(sub.get("endpoint"))
    if dead:
        # 重新读一次再过滤——发送期间可能有新设备订阅，不能用旧快照覆盖
        current = await store.get_setting("push_subs", []) or []
        await store.set_setting(
            "push_subs", [s for s in current if s.get("endpoint") not in dead]
        )
        log.info("pruned %d dead push subscriptions", len(dead))
    return ok_any
