"""2026.20+ 加密行车记录仪片段的解密。

固件 2026.20 起，写到 U 盘上的片段默认是加密容器，密钥绑 Tesla 账号。
第三方播放器全部直接失效 —— 这是 2026 年整个行车记录仪生态最大的一刀。

密码学部分是完全确定的（格式已被公开逆向，见 XGxF3/tesla-dashcam-decrypt
的 WRITEUP.md，MIT）：eCryptfs 风格容器，两页 4096 字节头，密文从 0x2000
起，AES-128-CBC 按 4096 字节分页，每页 IV 由文件密钥和页号派生。这些是
事实性的格式描述，下面的实现是照着规格独立写的。

**但文件密钥拿不到手。** 它以 wrapped_key 的形式存在头里，只有 Tesla 服务端
能解包。所以"完全离线解密"在密码学上不可能。可行且已被官方网页版、
ExportDash、ClipSalvage 三家验证过的做法是：只把几十字节元数据
（key_id / VIN / 时间戳 / wrapped_key / 公钥）发给 Tesla 换密钥，
**视频字节一个都不上传**，解密全在本机完成。

值得对用户说清楚的一点：dashcam.tesla.com 用的登录 token 作用域里
**不含 vehicle_cmds 和 vehicle_device_data** —— 它只能换解密密钥，
不能控车、也读不到车辆数据。不讲这一句，没人敢把 token 贴进来。

验证边界（诚实说明）：
- 分页解密、IV 派生、头部解析：有合成往返测试覆盖，可以确信。
- 向 Tesla 换密钥那一步：**没有真实加密样片和账号，没能端到端验证过。**
  代码按公开规格实现，并在解开第一页后检查 MP4 的 ftyp box —— AES-CBC
  没有 AEAD，密钥错了会安静地产出垃圾而不是报错，不自检的话用户会得到
  一个"解密成功"但放不出来的文件。
"""

import hashlib
import logging
import struct
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PAGE_SIZE = 4096
CIPHERTEXT_OFFSET = 0x2000          # 两页 4096 字节的头
METADATA_OFFSET = 0x1000            # 第二页头：换密钥要用的那些字段

DECRYPT_ENDPOINT = "https://dashcam.tesla.com/api/1/decrypt/batch"


@dataclass
class ClipHeader:
    plaintext_size: int
    key_id: int
    public_key: bytes
    vin: str
    timestamp: int
    wrapped_key: bytes


def is_encrypted(path: Path) -> bool:
    """判断是不是 Tesla 的加密容器。

    判据取自头部第 0x14 处的元数据偏移必须等于 0x1000，且 key_id 非零。
    只看"ffprobe 打不开"是不够的：截断的普通 mp4 也打不开，
    把两者混为一谈会让用户去关一个根本没开的加密开关。
    """
    try:
        with path.open("rb") as f:
            probe = f.read(METADATA_OFFSET + 4)
    except OSError:
        return False
    if len(probe) < METADATA_OFFSET + 4:
        return False
    try:
        metadata_offset = struct.unpack(">I", probe[0x14:0x18])[0]
    except struct.error:
        return False
    return (metadata_offset == METADATA_OFFSET
            and probe[METADATA_OFFSET:METADATA_OFFSET + 4] != b"\x00" * 4)


def read_header(path: Path) -> ClipHeader:
    """读出换密钥需要的元数据。不碰视频内容。"""
    with path.open("rb") as f:
        head = f.read(CIPHERTEXT_OFFSET)
    if len(head) < CIPHERTEXT_OFFSET:
        raise ValueError("file is too short to be an encrypted container")

    return ClipHeader(
        plaintext_size=struct.unpack(">Q", head[0x0000:0x0008])[0],
        key_id=struct.unpack(">I", head[0x1000:0x1004])[0],
        public_key=head[0x1004:0x1045],
        # VIN 是定长 17 字节 ASCII，尾部可能有 NUL 填充
        vin=head[0x1045:0x1056].decode("ascii", "ignore").strip("\x00").strip(),
        timestamp=struct.unpack(">Q", head[0x1056:0x105E])[0],
        wrapped_key=head[0x105E:0x105E + 44],
    )


def page_iv(file_key: bytes, page_number: int) -> bytes:
    """每页的 IV：MD5( MD5(file_key) + ASCII(页号) + 补零到 32 字节 )。

    IV 不落盘，所以这一步算错的表现不是报错，而是解出一堆看似正常的字节。
    """
    root = hashlib.md5(file_key).digest()                    # noqa: S324 - 格式规定，非安全用途
    material = root + str(page_number).encode("ascii")
    material = material.ljust(32, b"\x00")[:32]
    return hashlib.md5(material).digest()                    # noqa: S324 - 同上


def _looks_like_mp4(first_page: bytes) -> bool:
    """解开第一页之后自检。

    AES-CBC 没有 AEAD，密钥错了不会抛异常，只会安静地产出垃圾。
    不自检的话用户拿到的是一个"解密成功"但放不出来的文件，
    而他会以为是播放器的问题。MP4 的 ftyp box 在最前面，够用了。
    """
    return len(first_page) >= 12 and first_page[4:8] == b"ftyp"


def decrypt_file(path: Path, file_key: bytes, out_path: Path) -> int:
    """用已经换到的文件密钥解密整个片段，返回写出的字节数。

    file_key 是 16 字节的 AES-128 密钥（服务端返回的是 base64）。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:  # pragma: no cover - 依赖缺失时给人话
        raise RuntimeError(
            "decryption needs the 'cryptography' package; add it to requirements.txt") from e

    if len(file_key) != 16:
        raise ValueError(f"file key must be 16 bytes, got {len(file_key)}")

    header = read_header(path)
    written = 0
    with path.open("rb") as src, out_path.open("wb") as dst:
        src.seek(CIPHERTEXT_OFFSET)
        page_no = 0
        while True:
            chunk = src.read(PAGE_SIZE)
            if not chunk:
                break
            # 尾页可能不足一个分组；CBC 只能整块解，剩下的零头原样丢弃
            usable = len(chunk) - (len(chunk) % 16)
            if usable == 0:
                break
            cipher = Cipher(algorithms.AES(file_key),
                            modes.CBC(page_iv(file_key, page_no)))
            dec = cipher.decryptor()
            plain = dec.update(chunk[:usable]) + dec.finalize()

            if page_no == 0 and not _looks_like_mp4(plain):
                # 立刻停手并删掉半成品。留下一个坏文件比什么都不留更糟：
                # 扫描器会把它当成有效片段收进素材库。
                dst.close()
                out_path.unlink(missing_ok=True)
                raise ValueError(
                    "decryption produced data that is not an MP4 — the key is probably wrong")

            # 按头里记的明文长度截断，去掉最后一页的填充
            remaining = header.plaintext_size - written
            if remaining <= 0:
                break
            dst.write(plain[:remaining])
            written += min(len(plain), remaining)
            page_no += 1
    return written


def key_request_items(paths: list[Path]) -> list[dict]:
    """把一批加密片段的元数据整理成换密钥请求的 items。

    单独拆出来是为了让"到底往 Tesla 发了什么"可以被审计：
    调用方（和用户）能先看一眼这个列表，确认里面没有视频内容。
    """
    import base64

    items = []
    for p in paths:
        h = read_header(p)
        items.append({
            "id": p.name,
            "vin": h.vin,
            "key_id": h.key_id,
            "timestamp": h.timestamp,
            "wrapped_key": base64.b64encode(h.wrapped_key).decode(),
            "public_key": base64.b64encode(h.public_key).decode(),
        })
    return items


async def fetch_keys(items: list[dict], bearer: str) -> dict[str, bytes]:
    """向 Tesla 换密钥。返回 {item id: 16 字节密钥}。

    **这一步没有端到端验证过** —— 需要真实加密样片和一个有效账号，两样都没有。
    实现按公开规格写；上线前请拿一段真片子试一次。
    """
    import base64

    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            DECRYPT_ENDPOINT,
            json={"items": items},
            headers={"Authorization": f"Bearer {bearer}",
                     "Content-Type": "application/json"},
        )
        r.raise_for_status()
        body = r.json()

    keys: dict[str, bytes] = {}
    for res in body.get("results", []):
        if res.get("error"):
            log.warning("key request failed for %s: %s", res.get("id"), res["error"])
            continue
        if res.get("key"):
            keys[res["id"]] = base64.b64decode(res["key"])
    return keys
