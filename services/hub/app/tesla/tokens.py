"""复用 TeslaMate 已保存的 Tesla API Token（零配置车辆指令）。

TeslaMate 用 Cloak 把 token 加密存在 `private.tokens`（旧版本在 public）：
  密钥 = sha256(ENCRYPTION_KEY)，算法 AES-256-GCM，AAD 固定为 "AES256GCM"
  报文 = <<1, tag_len, tag, iv(12), ciphertag(16), ciphertext>>

因此只要 hub 拿到同一个 ENCRYPTION_KEY，就能读出 access token 直接下发指令，
用户无需再去申请开发者账号。解不出来时回退到用户手填 token。

注意：这是对 TeslaMate 内部结构的唯一一处耦合，全部失败路径都可降级。
"""

import hashlib
import logging
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .. import db
from ..alerts.store import store
from ..config import settings

log = logging.getLogger(__name__)

_AAD = b"AES256GCM"
_cache: dict[str, tuple[float, str]] = {}


def _decrypt(blob: bytes, key: bytes) -> str | None:
    try:
        if len(blob) < 2 or blob[0] != 1:
            return None
        tag_len = blob[1]
        pos = 2 + tag_len
        iv = blob[pos : pos + 12]
        ciphertag = blob[pos + 12 : pos + 28]
        ciphertext = blob[pos + 28 :]
        if len(iv) != 12 or len(ciphertag) != 16 or not ciphertext:
            return None
        plain = AESGCM(key).decrypt(iv, ciphertext + ciphertag, _AAD)
        return plain.decode("utf-8")
    except Exception as e:  # noqa: BLE001
        log.debug("token decrypt failed: %s", e)
        return None


class DbDown(Exception):
    """数据库不可用——与"密钥不对"区分开，避免给出误导性提示。"""


async def _from_teslamate() -> str | None:
    if not settings.encryption_key:
        return None
    key = hashlib.sha256(settings.encryption_key.encode()).digest()
    reachable = False
    for table in ("private.tokens", "public.tokens"):
        try:
            row = await db.fetchrow(
                f"SELECT access, refresh FROM {table} ORDER BY id DESC LIMIT 1"  # noqa: S608
            )
            reachable = True
        except db.DbUnavailable:
            raise DbDown from None
        except Exception:  # noqa: BLE001 - 表不存在（旧/新版本差异）属正常
            continue
        if row is None or row["access"] is None:
            continue
        token = _decrypt(bytes(row["access"]), key)
        if token:
            log.info("Tesla token loaded from %s", table)
            return token
        log.warning(
            "found TeslaMate token in %s but could not decrypt it —— "
            "hub 的 ENCRYPTION_KEY 与 TeslaMate 不一致？", table,
        )
    if not reachable:
        raise DbDown
    return None


def invalidate_cache() -> None:
    """收到 401 后调用，强制下次重新解密（TeslaMate 可能已刷新 token）。"""
    _cache.pop("tm", None)


async def access_token(*, raise_db_down: bool = False) -> str | None:
    """取可用的 access token：优先用户手填，其次 TeslaMate 解密，带 10 分钟缓存。

    raise_db_down=True 时，数据库不可用且没有缓存会抛 DbDown 而不是返回 None
    —— 供 token_source() 区分"没登录"和"数据库连不上"。
    """
    manual = await store.get_setting("tesla_access_token")
    if manual:
        return manual
    if settings.tesla_access_token:
        return settings.tesla_access_token

    cached = _cache.get("tm")
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    try:
        token = await _from_teslamate()
    except DbDown:
        if cached:
            return cached[1]        # 数据库临时不可用时沿用旧 token
        if raise_db_down:
            raise
        return None
    if token:
        _cache["tm"] = (time.time(), token)
    return token


async def set_manual_token(token: str) -> None:
    await store.set_setting("tesla_access_token", token.strip() or None)
    invalidate_cache()


async def token_source() -> str:
    """返回 manual / env / teslamate / db_down / none。"""
    if await store.get_setting("tesla_access_token"):
        return "manual"
    if settings.tesla_access_token:
        return "env"
    try:
        # 必须让 DbDown 冒上来。access_token() 默认把它吞成 None，于是
        # 这里永远走到 "none" —— 界面显示「没有可用 token / 请先登录」，
        # 而真相是 Postgres 连不上。DbDown 这个类就是为了不给这种误导提示。
        return "teslamate" if await access_token(raise_db_down=True) else "none"
    except DbDown:
        return "db_down"
