"""零散但会出错的地方（审计 D13 / D16 / D17 / L1 / L2 / L9）。

  - D13：proto3 不写默认值。停在 P 挡、车速 0 的帧解出来是 P / 0.0，而不是"未知"
         （用真的 H.264 码流验证：帧里插 Tesla 格式的 SEI，再交给解码器）
  - D16：ffprobe 超时后进程被杀掉，并且短时间内不会重复起
  - D17：自动校时拟合不好就拒绝、不覆盖原偏移；拟合好时找得回真实偏移
  - L1 ：插值不跨大空档（一侧是 NULL 时也不行）；慢变量沿用最后值
  - L2 ：SEI 落桶和取桶用同一种取整，帧率略高于整数时不提前半秒
  - L9 ：解密 —— 截断的密文报错且不留半成品；拒绝原地解密；IV 由独立实现验证；
         解密任务把原件留成 .mp4.enc、换上明文

跑法：python3 services/hub/tests/test_dashcam_misc.py（需要 hub 运行时依赖；SEI 码流部分需要 ffmpeg + libx264）
"""

import asyncio
import hashlib
import json
import math
import os
import random
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break
TMP = Path(tempfile.mkdtemp(prefix="omt-misc-"))
ROOT = TMP / "teslacam"
os.environ.update(DATA_DIR=str(TMP / "data"), TESLACAM_ROOT=str(ROOT), TESLACAM_TZ="America/Chicago")
(TMP / "data").mkdir()
ROOT.mkdir()
try:
    import aiosqlite  # noqa: F401
    import pydantic_settings  # noqa: F401
except ImportError:
    print("— skipped: needs the hub runtime (aiosqlite, pydantic-settings); run with tests/run.sh --docker")
    sys.exit(0)

from app.alerts.store import store  # noqa: E402
from app.dashcam import align, ff, probe, sei, telemetry  # noqa: E402
from app.dashcam import decrypt as D
from app.dashcam.scanner import Event, Segment  # noqa: E402

FAILS: list[str] = []
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- D13

def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _proto(fr: dict) -> bytes:
    """proto3：值为 0 的字段不写（这正是要测的情形）。"""
    o = bytearray()
    for no, key, kind in ((1, "version", "v"), (2, "gear", "v"), (3, "seq", "v"), (4, "speed", "f"),
                          (11, "lat", "d"), (12, "lon", "d")):
        v = fr.get(key, 0)
        if not v:
            continue
        if kind == "v":
            o += _varint(no << 3) + _varint(int(v))
        elif kind == "f":
            o += _varint(no << 3 | 5) + struct.pack("<f", v)
        else:
            o += _varint(no << 3 | 1) + struct.pack("<d", v)
    return bytes(o)


def _epb(rbsp: bytes) -> bytes:
    out, zeros = bytearray(), 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def make_sei_clip(out: Path, fps: int = 30, secs: float = 2.0) -> list[dict]:
    """真实 H.264 码流里每帧前插一个 Tesla 格式的 SEI。前 1 秒停在 P 挡、0 km/h。"""
    raw = out.with_suffix(".h264")
    subprocess.run([ff.ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size=160x120:rate={fps}:duration={secs}", "-c:v", "libx264",
                    "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(raw)], check=True)
    data = raw.read_bytes()
    starts, i = [], 0
    while i < len(data) - 3:
        if data[i] == 0 and data[i + 1] == 0 and (data[i + 2] == 1 or (data[i + 2] == 0 and data[i + 3] == 1)):
            sc = 3 if data[i + 2] == 1 else 4
            starts.append((i, i + sc))
            i += sc
        else:
            i += 1
    nals = [data[s:(starts[k + 1][0] if k + 1 < len(starts) else len(data))] for k, (_, s) in enumerate(starts)]
    truth, buf, k = [], bytearray(), 0
    for nal in nals:
        if nal[0] & 0x1F in (1, 5) and nal[1] & 0x80:      # 一帧的第一个 slice
            t = k / fps
            fr = {"version": 1, "seq": k, "gear": 0 if t < 1 else 1, "speed": 0.0 if t < 1 else 50 / 3.6,
                  "lat": 1.0, "lon": 2.0}
            payload = b"B" * 15 + b"i" + _proto(fr)
            msg = bytes([5, len(payload)]) + payload + b"\x80"
            buf += b"\x00\x00\x00\x01\x06" + _epb(msg)
            truth.append(fr)
            k += 1
        buf += b"\x00\x00\x00\x01" + nal
    sei_raw = out.with_suffix(".sei.h264")
    sei_raw.write_bytes(bytes(buf))
    subprocess.run([ff.ffmpeg(), "-v", "error", "-y", "-framerate", str(fps), "-f", "h264", "-i", str(sei_raw),
                    "-c", "copy", str(out)], check=True)
    return truth


def test_proto3_zero_values():
    print("D13 proto3 默认值")
    chk("空载荷是合法帧（全部字段为默认值）", sei._decode_protobuf(b"") == {})
    only_seq = sei._decode_protobuf(_proto({"seq": 7}))
    chk("只有序号的帧不再被丢掉", only_seq == {"frame_seq_no": 7}, only_seq)
    chk("序号为 0、静止的帧也不丢", sei._decode_protobuf(_proto({"version": 1})) == {"version": 1})
    if not HAVE_FFMPEG:
        print("  — 没有 ffmpeg，跳过真实码流验证")
        return
    clip = TMP / "sei" / "2026-10-01_10-00-00-front.mp4"
    clip.parent.mkdir()
    make_sei_clip(clip)
    s = sei.extract(clip, step=0.5)
    first, last = (s[0], s[-1]) if s else ({}, {})
    chk("真实码流：静止帧解出 P 挡、0.0 km/h", first.get("gear") == "P" and first.get("speed_kmh") == 0.0, first)
    chk("真实码流：行驶帧解出 D 挡、50 km/h", last.get("gear") == "D" and last.get("speed_kmh") == 50.0, last)
    chk("采样时间连续（第 0 帧没被丢，时间轴不错位）", [x["t"] for x in s] == [0.0, 0.5, 1.0, 1.5],
        [x["t"] for x in s])


# ---------------------------------------------------------------- D16

async def test_probe_timeout():
    print("D16 ffprobe 超时")
    fake = TMP / "fakebin" / "ffprobe"
    fake.parent.mkdir()
    pids = TMP / "fake-ffprobe.pids"
    fake.write_text(f"#!/bin/sh\necho $$ >> {pids}\nexec sleep 30\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    old = os.environ.get("FFPROBE_BIN")
    os.environ["FFPROBE_BIN"] = str(fake)
    ff.reset_caches()
    probe.PROBE_TIMEOUT_S = 1
    target = TMP / "slow.mp4"
    target.write_bytes(b"\x00" * 4096)
    try:
        t0 = time.monotonic()
        r = await probe.probe(target)
        took = time.monotonic() - t0
        pid = int(pids.read_text().split()[0])
        await asyncio.sleep(0.2)
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        chk("超时按时返回 reason=timeout", r["reason"] == "timeout" and took < 3, f"{took:.1f}s")
        chk("超时的 ffprobe 进程被杀掉（以前留在后台）", not alive)
        await probe.probe(target)
        chk("短时间内不会再起一个注定超时的 ffprobe", len(pids.read_text().split()) == 1)
    finally:
        if old is None:
            os.environ.pop("FFPROBE_BIN", None)
        else:
            os.environ["FFPROBE_BIN"] = old
        ff.reset_caches()
        probe.PROBE_TIMEOUT_S = 12


# ---------------------------------------------------------------- D17

def _speed(t: float) -> float:
    return 50 + 25 * math.sin(t / 6.0) + 8 * math.sin(t / 1.7)


async def test_align():
    print("D17 自动校时")
    seg_starts = [datetime(2026, 10, 1, 10, 0), datetime(2026, 10, 1, 10, 1)]
    ev = Event(id="SavedClips/x", type="saved", timestamp=seg_starts[0],
               segments=[Segment(start=s, cameras={"front": f"f{i}.mp4"}) for i, s in enumerate(seg_starts)])
    bases = {f"f{i}.mp4": telemetry.local_naive_to_utc(s).timestamp() for i, s in enumerate(seg_starts)}
    sei.extract = lambda path, step=0.5, strict=False: [
        {"t": k * 0.5, "speed_kmh": round(_speed(bases[Path(path).name] + k * 0.5), 1)} for k in range(120)]
    true_offset = 7.25
    mode = {"kind": "true"}

    async def positions(car_id, start, end, pad_s=15):
        t0, t1 = start.timestamp(), end.timestamp()
        rng = random.Random(1)
        out = []
        t = t0
        while t <= t1:
            if mode["kind"] == "true":
                v = round(_speed(t - true_offset))          # TeslaMate 记的是整数 km/h
            elif mode["kind"] == "noise":
                v = rng.uniform(0, 120)
            else:
                v = 50.0
            out.append({"date": t, "speed": v})
            t += 1.0
        return out

    telemetry.fetch_positions = positions
    await telemetry.set_clock_offset(1, 3.0)
    mode["kind"] = "noise"
    try:
        await align.auto_align(ev, ROOT, 1)
        rejected = False
    except ValueError as e:
        rejected = "不可信" in str(e)
    chk("拿噪声硬拟合 → 拒绝（以前会存下 −3.25 s）", rejected)
    chk("  原偏移保持不变", await telemetry.clock_offset(1) == 3.0)
    mode["kind"] = "true"
    r = await align.auto_align(ev, ROOT, 1)
    chk("真实偏移 7.25 s 能找回来", abs(r["offset"] - true_offset) <= 0.25 and r["accepted"], r)
    chk("  并且存下了", abs(await telemetry.clock_offset(1) - r["offset"]) < 1e-6)
    sei.extract = lambda path, step=0.5, strict=False: [{"t": k * 0.5, "speed_kmh": 50.0} for k in range(120)]
    try:
        await align.auto_align(ev, ROOT, 1)
        flat = False
    except ValueError:
        flat = True
    chk("车速没有变化的片段 → 拒绝（任何偏移都对得上）", flat)


# ---------------------------------------------------------------- L1 / L2

def test_resample_and_buckets():
    print("L1 插值不跨大空档")
    rows = [{"date": 0.0, "speed": 95.0, "battery_level": 81.0},
            {"date": 7200.0, "speed": None, "battery_level": 80.0}]
    for r in rows:
        for f in telemetry.NUMERIC_FIELDS:
            r.setdefault(f, None)
    out = telemetry.resample(rows, 600.0, 600.0)
    chk("两小时空档中间的车速是 None（以前显示 95 km/h）", out[0]["speed"] is None, out[0]["speed"])
    chk("慢变量（电量）沿用最后已知值", out[0]["battery_level"] == 81.0, out[0]["battery_level"])
    near = telemetry.resample(rows, 10.0, 10.0)[0]
    chk("离有值的采样 10 秒以内仍沿用", near["speed"] == 95.0, near["speed"])
    dense = [{"date": float(k), "speed": 10.0 * k} for k in range(5)]
    for r in dense:
        for f in telemetry.NUMERIC_FIELDS:
            r.setdefault(f, None)
    mid = telemetry.resample(dense, 1.0, 3.0, step=0.5)
    chk("正常 1Hz 线性插值", [m["speed"] for m in mid] == [10.0, 15.0, 20.0, 25.0, 30.0],
        [m["speed"] for m in mid])

    print("L2 SEI 落桶")
    fps_scale = 36.0 / 36.03                       # 帧率略高于 36：t 比整半秒略早
    sei_track = [{"t": round(k * 0.5 * fps_scale, 3), "speed_kmh": 100.0 + k} for k in range(20)]
    overlay = [{"t": k * 0.5} for k in range(20)]
    ass = telemetry.build_ass(overlay, 640, 480, datetime(2026, 1, 1), step=0.5, lang="en",
                              sei=sei_track, prefs={"length": "km", "temp": "C", "pressure": "bar"})
    shown = [line.rsplit("}", 1)[-1] for line in ass.splitlines() if ",Big,," in line and "\\fs" not in line]
    chk("每一步显示的是自己那一桶的 SEI 车速", shown == [str(100 + k) for k in range(20)], shown[:6])


# ---------------------------------------------------------------- L9

def _iv_independent(key: bytes, page: int) -> bytes:
    """eCryptfs 的 IV 派生，照 fs/ecryptfs/crypto.c 的 ecryptfs_derive_iv 独立实现：
    src = root_iv(=MD5(key)) || sprintf("%lld", page)，补零到 32 字节，IV = MD5(src)。"""
    root = hashlib.md5(key).digest()                                     # noqa: S324
    src = bytearray(32)
    src[:16] = root
    num = b"%d" % page
    src[16:16 + len(num)] = num
    return hashlib.md5(bytes(src)).digest()                              # noqa: S324


def _container(plain: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    head = bytearray(D.CIPHERTEXT_OFFSET)
    struct.pack_into(">Q", head, 0, len(plain))
    struct.pack_into(">I", head, 0x14, D.METADATA_OFFSET)
    struct.pack_into(">I", head, 0x1000, 42)
    head[0x1045:0x1056] = b"7G2CEHED0RA000000"
    padded = plain + b"\x00" * ((-len(plain)) % D.PAGE_SIZE)
    body = bytearray()
    for p in range(len(padded) // D.PAGE_SIZE):
        enc = Cipher(algorithms.AES(key), modes.CBC(_iv_independent(key, p))).encryptor()
        body += enc.update(padded[p * D.PAGE_SIZE:(p + 1) * D.PAGE_SIZE]) + enc.finalize()
    return bytes(head) + bytes(body)


async def test_decrypt():
    print("L9 解密")
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("  — 没有 cryptography，跳过")
        return
    key = bytes(range(16))
    plain = b"\x00\x00\x00\x18ftypisom" + b"\x00\x00\x02\x00isomiso2" + os.urandom(3 * 4096 + 123)
    d = TMP / "enc"
    d.mkdir()
    enc = d / "clip.mp4"
    enc.write_bytes(_container(plain, key))
    out = d / "out.mp4"
    D.decrypt_file(enc, key, out)
    chk("用独立实现的 IV 加密，解密器还原出原文", out.read_bytes() == plain)
    chk("page_iv 和 eCryptfs 的派生一致（第 0、1、123 页）",
        all(D.page_iv(key, p) == _iv_independent(key, p) for p in (0, 1, 123)))

    trunc = d / "trunc.mp4"
    trunc.write_bytes(enc.read_bytes()[: D.CIPHERTEXT_OFFSET + 4096 + 100])
    out2 = d / "out2.mp4"
    try:
        D.decrypt_file(trunc, key, out2)
        raised = False
    except ValueError as e:
        raised = "truncated" in str(e)
    chk("密文被截断 → 报错（以前安静地返回一个短长度）", raised)
    chk("  而且不留半成品", not out2.exists())
    before = enc.read_bytes()
    try:
        D.decrypt_file(enc, key, enc)
        refused = False
    except ValueError:
        refused = True
    chk("拒绝原地解密（以前会把源文件截成 0 字节）", refused and enc.read_bytes() == before)

    # 解密任务：只把元数据发给 Tesla（这里打桩），解开后换上明文、原件留成 .enc
    ev = ROOT / "SentryClips/2026-10-01_10-00-00"
    ev.mkdir(parents=True)
    a, b = ev / "2026-10-01_09-59-00-front.mp4", ev / "2026-10-01_09-59-00-back.mp4"
    a.write_bytes(_container(plain, key))
    b.write_bytes(_container(plain, key))
    sent = []

    async def fake_keys(items, bearer):
        sent.append((items, bearer))
        return {a.name: key, b.name: bytes(16)}          # back 的密钥是错的

    D.fetch_keys = fake_keys
    st = D.decryptor.start("SentryClips/2026-10-01_10-00-00", D.find_encrypted([a, b]), ROOT, "tok", True)
    while D.decryptor.running:
        await asyncio.sleep(0.05)
    st = D.decryptor.state
    chk("只发了头部元数据（没有视频字节）", sent and all(set(i) == {"id", "vin", "key_id", "timestamp",
                                                         "wrapped_key", "public_key"} for i in sent[0][0]))
    chk("密钥对的片段换成明文、原件留成 .mp4.enc", a.read_bytes() == plain and (ev / (a.name + ".enc")).exists())
    chk("密钥错的片段报失败、原文件不动", st["decrypted"] == 1 and len(st["failed"]) == 1
        and D.is_encrypted(b) and not (ev / (b.name + ".enc")).exists(), json.dumps(st["failed"])[:120])
    chk("不留 .decrypting 临时文件", not list(ev.glob(".*.decrypting")))


async def main():
    await store.open()
    try:
        await test_probe_timeout()
        await test_align()
        await test_decrypt()
    finally:
        await store.close()


if __name__ == "__main__":
    test_proto3_zero_values()
    test_resample_and_buckets()
    asyncio.run(main())
    print()
    print("全部通过" if not FAILS else f"失败: {FAILS}")
    sys.exit(1 if FAILS else 0)
