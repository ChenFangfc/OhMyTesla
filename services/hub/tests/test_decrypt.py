"""加密片段解密的往返验证。

没有真实的加密样片，所以这里**自己按规格造一个**：用同样的分页 AES-128-CBC
和同样的 IV 派生把一段已知明文加密成容器，再让解密器还原，比对字节。

这能证明什么、不能证明什么，要说清楚：
  能证明 —— 分页逻辑、IV 派生、头部解析、明文长度截断、密钥错误时的自检，
            这几样只要规格没读错就是对的，而规格是公开的。
  不能证明 —— 我们对规格的理解和 Tesla 真实文件一致。真实样片的头部里
            可能还有本实现忽略的字段。上线前必须拿一段真片子验一次。

跑法：docker exec ohmytesla-ohmytesla-1 python3 /opt/ohmytesla/tests/test_decrypt.py
"""

import os
import struct
import sys
import tempfile
from pathlib import Path

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break

from app.dashcam import decrypt as D  # noqa: E402

FAILS: list[str] = []


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def make_container(plaintext: bytes, file_key: bytes, vin: str = "7G2CEHED0RA000000",
                   key_id: int = 42, timestamp: int = 1787000000) -> bytes:
    """按规格造一个加密容器。加密方向用的是和解密器**独立**写的代码路径
    （Cipher.encryptor 而不是 decryptor），所以往返成功不是自己证明自己。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    head = bytearray(D.CIPHERTEXT_OFFSET)
    struct.pack_into(">Q", head, 0x0000, len(plaintext))
    struct.pack_into(">I", head, 0x0014, D.METADATA_OFFSET)   # 容器识别靠这个
    struct.pack_into(">I", head, 0x1000, key_id)
    head[0x1004:0x1045] = b"\x04" + bytes(range(64))          # 65 字节未压缩 EC 点
    head[0x1045:0x1056] = vin.encode().ljust(17, b"\x00")
    struct.pack_into(">Q", head, 0x1056, timestamp)
    head[0x105E:0x105E + 44] = bytes(range(44))

    body = bytearray()
    padded = plaintext + b"\x00" * ((-len(plaintext)) % D.PAGE_SIZE)
    for page in range(len(padded) // D.PAGE_SIZE):
        block = padded[page * D.PAGE_SIZE:(page + 1) * D.PAGE_SIZE]
        c = Cipher(algorithms.AES(file_key), modes.CBC(D.page_iv(file_key, page)))
        enc = c.encryptor()
        body += enc.update(block) + enc.finalize()
    return bytes(head) + bytes(body)


def main() -> int:
    key = bytes(range(16))
    tmp = Path(tempfile.mkdtemp())

    # 一段像样的假 MP4：前 12 字节是 ftyp box，后面塞够跨越多页的内容
    plain = (b"\x00\x00\x00\x18ftypisom" + b"\x00\x00\x02\x00isomiso2"
             + bytes(range(256)) * 40)          # 约 10KB，跨 3 页
    enc_path = tmp / "clip.mp4"
    enc_path.write_bytes(make_container(plain, key))

    # ── 识别 ────────────────────────────────────────────────
    chk("识别为加密容器", D.is_encrypted(enc_path))

    plain_path = tmp / "plain.mp4"
    plain_path.write_bytes(plain)
    chk("普通 mp4 不误判为加密", not D.is_encrypted(plain_path))

    short = tmp / "short.mp4"
    short.write_bytes(b"\x00" * 100)
    chk("过短文件不误判", not D.is_encrypted(short))

    # ── 头部解析 ────────────────────────────────────────────
    h = D.read_header(enc_path)
    chk("明文长度正确", h.plaintext_size == len(plain), h.plaintext_size)
    chk("VIN 正确", h.vin == "7G2CEHED0RA000000", h.vin)
    chk("key_id 正确", h.key_id == 42, h.key_id)
    chk("timestamp 正确", h.timestamp == 1787000000, h.timestamp)
    chk("wrapped_key 长度 44", len(h.wrapped_key) == 44, len(h.wrapped_key))
    chk("公钥以 0x04 开头", h.public_key[:1] == b"\x04")

    # ── IV 派生：每页必须不同且确定 ──────────────────────────
    ivs = [D.page_iv(key, i) for i in range(4)]
    chk("每页 IV 各不相同", len(set(ivs)) == 4)
    chk("IV 是确定的", D.page_iv(key, 2) == ivs[2])
    chk("IV 长度 16", all(len(v) == 16 for v in ivs))
    chk("换个密钥 IV 就变", D.page_iv(b"\xff" * 16, 0) != ivs[0])

    # ── 往返 ────────────────────────────────────────────────
    out = tmp / "out.mp4"
    n = D.decrypt_file(enc_path, key, out)
    got = out.read_bytes()
    chk("写出长度等于明文长度", n == len(plain), f"{n} vs {len(plain)}")
    chk("字节完全一致", got == plain,
        "前 16 字节: " + got[:16].hex() if got != plain else "")

    # ── 密钥错误必须被自检拦住，且不留半成品 ─────────────────
    bad_out = tmp / "bad.mp4"
    try:
        D.decrypt_file(enc_path, b"\x01" * 16, bad_out)
        chk("错误密钥被拒绝", False, "居然成功了")
    except ValueError as e:
        chk("错误密钥被拒绝", "not an MP4" in str(e), str(e)[:60])
    chk("错误密钥不留半成品文件", not bad_out.exists())

    # ── 换密钥请求里不能带视频内容 ───────────────────────────
    items = D.key_request_items([enc_path])
    chk("请求项字段齐全",
        set(items[0]) == {"id", "vin", "key_id", "timestamp", "wrapped_key", "public_key"},
        sorted(items[0]))
    body = repr(items).encode()
    chk("请求里不含明文视频片段", plain[16:64] not in body)
    chk("请求体很小（只有元数据）", len(body) < 1000, f"{len(body)} 字节")

    print()
    print("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
