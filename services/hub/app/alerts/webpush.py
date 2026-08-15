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


async def subscribe(subscription: dict) -> None:
    subs = await store.get_setting("push_subs", []) or []
    endpoint = subscription.get("endpoint")
    if not endpoint:
        raise ValueError("subscription missing endpoint")
    subs = [s for s in subs if s.get("endpoint") != endpoint]
    subs.append(subscription)
    await store.set_setting("push_subs", subs[-20:])  # 最多记 20 台设备


async def unsubscribe(endpoint: str) -> None:
    subs = await store.get_setting("push_subs", []) or []
    await store.set_setting("push_subs", [s for s in subs if s.get("endpoint") != endpoint])


async def count() -> int:
    return len(await store.get_setting("push_subs", []) or [])


def _send_sync(sub: dict, payload: str, priv_pem: str) -> tuple[bool, int]:
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


async def send(title: str, body: str, url: str = "/", level: str = "info") -> bool:
    subs = await store.get_setting("push_subs", []) or []
    if not subs:
        return False
    kp = await keys()
    payload = json.dumps(
        {"title": title, "body": body, "url": url, "level": level}, ensure_ascii=False
    )

    results = await asyncio.gather(
        *[asyncio.to_thread(_send_sync, s, payload, kp["private_pem"]) for s in subs],
        return_exceptions=True,
    )
    ok_any = False
    dead = []
    for sub, res in zip(subs, results):
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
