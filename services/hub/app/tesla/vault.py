"""TeslaMate 的 Cloak 加密信封 —— 读和写。

TeslaMate 用 Elixir 的 Cloak 库加密存储 Tesla token。报文结构：

    ┌──────────────── HEADER ────────────────┬──── BODY ────┐
    │ Type(1) Length(1) Tag(n) │ IV(12) │ Ciphertag(16) │ Ciphertext(n) │

    Type = 1，Tag = "AES.GCM.V1"
    密钥 = sha256(ENCRYPTION_KEY)
    算法 = AES-256-GCM，AAD 固定为 "AES256GCM"

能**写**这个格式，OhmyTesla 就能提供自己的登录页，把 token 直接存进去——
用户再也不用打开 TeslaMate 的界面。这是白标化的关键一环。

⚠️ 这是全项目对 TeslaMate 内部结构耦合最深的地方。所有失败路径都优雅降级，
上游一旦改格式，登录页会明确报错而不是静默写坏数据。
"""

import hashlib
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings

log = logging.getLogger(__name__)

_AAD = b"AES256GCM"
_TAG = b"AES.GCM.V1"
_TYPE = 1


class VaultError(Exception):
    pass


def _key() -> bytes:
    if not settings.encryption_key:
        raise VaultError(
            "缺少 ENCRYPTION_KEY —— 它必须与 TeslaMate 使用同一个值"
            "（本项目的 docker-compose 默认已如此配置）"
        )
    return hashlib.sha256(settings.encryption_key.encode()).digest()


def encrypt(plaintext: str) -> bytes:
    """按 Cloak 的信封格式加密，产出可直接写进 TeslaMate 数据库的 bytea。"""
    iv = os.urandom(12)
    blob = AESGCM(_key()).encrypt(iv, plaintext.encode(), _AAD)
    ciphertext, ciphertag = blob[:-16], blob[-16:]
    header = bytes([_TYPE, len(_TAG)]) + _TAG
    return header + iv + ciphertag + ciphertext


def decrypt(blob: bytes) -> str | None:
    """解开 Cloak 信封。解不开返回 None（密钥不对/格式变了）。"""
    try:
        if len(blob) < 2 or blob[0] != _TYPE:
            return None
        tag_len = blob[1]
        pos = 2 + tag_len
        iv = blob[pos : pos + 12]
        ciphertag = blob[pos + 12 : pos + 28]
        ciphertext = blob[pos + 28 :]
        if len(iv) != 12 or len(ciphertag) != 16 or not ciphertext:
            return None
        return AESGCM(_key()).decrypt(iv, ciphertext + ciphertag, _AAD).decode("utf-8")
    except VaultError:
        raise
    except Exception as e:  # noqa: BLE001
        log.debug("cloak decrypt failed: %s", e)
        return None


def selftest() -> bool:
    """加解密自检——写入前先确认我们的实现是自洽的。"""
    try:
        probe = "ohmytesla-selftest"
        return decrypt(encrypt(probe)) == probe
    except Exception:  # noqa: BLE001
        return False


async def save_tokens(access: str, refresh: str) -> None:
    """把 token 写进 TeslaMate 的 private.tokens。

    TeslaMate 只保留一行；写入后它会在下一个刷新周期自然接管并续期。
    """
    from .. import db

    if not selftest():
        raise VaultError("加密自检未通过，拒绝写入（避免写坏数据）")

    enc_a, enc_r = encrypt(access.strip()), encrypt(refresh.strip())
    last: Exception | None = None
    for table in ("private.tokens", "public.tokens"):
        try:
            row = await db.fetchrow(f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1")  # noqa: S608
            if row:
                await db.execute(
                    f"UPDATE {table} SET access = $1, refresh = $2, updated_at = now() WHERE id = $3",  # noqa: S608
                    enc_a, enc_r, row["id"],
                )
            else:
                await db.execute(
                    f"INSERT INTO {table} (access, refresh, inserted_at, updated_at)"  # noqa: S608
                    " VALUES ($1, $2, now(), now())",
                    enc_a, enc_r,
                )
            log.info("Tesla tokens saved into %s", table)
            return
        except db.DbUnavailable:
            # 数据库连不上要原样往上抛，让接口回 503「稍后再试」。
            # 以前这里被 except Exception 一并吞成 VaultError，登录页就会在
            # Postgres 重启的十几秒里告诉用户「token 无效」——完全是误导。
            raise
        except Exception as e:  # noqa: BLE001 - 表不存在就试下一个
            last = e
            continue
    raise VaultError(f"写入 token 失败：{last}")


async def has_tokens() -> bool:
    """库里有没有存过 token。

    数据库不可用时抛 db.DbUnavailable —— **不要**改成返回 False。
    首页用它决定是否跳转引导页，返回 False 会在 Postgres 抖一下的时候
    把已经登录的用户踢去 /welcome，看起来就像"账号掉了"。
    """
    from .. import db

    reachable = False
    for table in ("private.tokens", "public.tokens"):
        try:
            n = await db.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608
            reachable = True
            if n:
                return True
        except db.DbUnavailable:
            raise
        except Exception:
            log.debug("suppressed", exc_info=True)
            continue
    if not reachable:
        raise db.DbUnavailable("tokens table unreachable")
    return False
