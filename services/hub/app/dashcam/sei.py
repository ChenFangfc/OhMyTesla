"""TeslaCam SEI 逐帧遥测提取（固件 2025.44.25+）。

自 2025 假日版固件起，行车记录仪视频流内嵌 SEI (Supplemental
Enhancement Information) NAL 单元，内容为 protobuf 编码的逐帧遥测：
档位 / 车速 / 油门踏板 / 方向盘转角 / 转向灯 / 刹车 / Autopilot 状态
/ GPS / 航向角 / 三轴加速度。

字段定义参考特斯拉官方公开 schema（github.com/teslamotors/dashcam
dashcam.proto，proto3）。本文件为独立实现：手写 protobuf wire format
解码，无 protobuf 依赖。

注意:
- 停车（哨兵）片段通常没有 SEI；
- 2026.20+ 固件默认加密的片段无法解析（需在车机 安全 设置里关闭加密）；
- SEI 无电量/功率/海拔/温度 —— 这些由 TeslaMate 数据库补齐。
"""

import logging
import struct
from pathlib import Path

log = logging.getLogger(__name__)

GEARS = {0: "P", 1: "D", 2: "R", 3: "N"}
AP_STATES = {0: "", 1: "FSD", 2: "AP", 3: "TACC"}

# SeiMetadata 字段编号 -> (名称, 类型)  [proto3, 见官方 dashcam.proto]
_FIELDS = {
    1: ("version", "varint"),
    2: ("gear", "varint"),
    3: ("frame_seq_no", "varint"),
    4: ("speed_mps", "float"),
    5: ("accel_pedal", "float"),
    6: ("steering_angle", "float"),
    7: ("blinker_left", "bool"),
    8: ("blinker_right", "bool"),
    9: ("brake", "bool"),
    10: ("ap_state", "varint"),
    11: ("lat", "double"),
    12: ("lon", "double"),
    13: ("heading", "double"),
    14: ("accel_x", "double"),
    15: ("accel_y", "double"),
    16: ("accel_z", "double"),
}


def _decode_protobuf(data: bytes) -> dict | None:
    """极简 protobuf wire 解码，只认 SeiMetadata 的字段。"""
    out: dict = {}
    i, n = 0, len(data)

    def varint():
        nonlocal i
        shift = result = 0
        while i < n:
            b = data[i]
            i += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
        raise ValueError("truncated varint")

    try:
        while i < n:
            tag = varint()
            field_no, wire = tag >> 3, tag & 7
            if wire == 0:
                v = varint()
            elif wire == 5:  # fixed32
                if i + 4 > n:
                    return None
                (v,) = struct.unpack_from("<f", data, i)
                i += 4
            elif wire == 1:  # fixed64
                if i + 8 > n:
                    return None
                (v,) = struct.unpack_from("<d", data, i)
                i += 8
            elif wire == 2:  # length-delimited（跳过）
                ln = varint()
                i += ln
                continue
            else:
                return None
            spec = _FIELDS.get(field_no)
            if spec:
                name, typ = spec
                out[name] = bool(v) if typ == "bool" else v
    except (ValueError, struct.error):
        return None
    # proto3 不序列化默认值：停在 P 挡（0）、车速 0.0 的帧里根本没有这两个字段。
    # 以前要求至少有 frame_seq_no 或 speed_mps 才算有效 —— 序号恰好为 0 的
    # 静止帧就被丢掉，后面每一帧的时间（idx / fps）都往前错一格。
    # 载荷已经由 UUID 前缀（≥4 个 0x42 + 0x69）定位过，能完整解码即视为有效。
    return out


def _strip_emulation(data: bytes) -> bytes:
    """去掉 H.26x 码流的防竞争字节 (00 00 后的 03)。"""
    out = bytearray()
    zeros = 0
    for b in data:
        if zeros >= 2 and b == 0x03:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def _sei_payload(nal: bytes) -> bytes | None:
    """在 SEI NAL 内定位 protobuf 载荷（UUID 前缀后紧跟数据）。

    H.264 NAL 头 1 字节、HEVC 头 2 字节，之后是 payload type/size 字节，
    因此 0x42 填充串的起点不固定——先找到 0x42 连续串，串后紧跟 0x69
    标记，再往后即 protobuf 数据。
    """
    limit = min(len(nal) - 1, 64)
    i = 2
    while i < limit and nal[i] != 0x42:
        i += 1
    run = 0
    while i < limit and nal[i] == 0x42:
        i += 1
        run += 1
    if run >= 4 and i < len(nal) - 1 and nal[i] == 0x69:
        return _strip_emulation(nal[i + 1 : -1])
    return None


def _is_sei_nal(first: int, second: int) -> bool:
    # H.264: nal_unit_type == 6 (SEI)，随后 payload type 5 = user data unregistered
    if (first & 0x1F) == 6 and second == 5:
        return True
    # HEVC: nal_unit_type 39/40 (PREFIX/SUFFIX SEI)，header 2 字节，payload type 在第 3 字节
    return (first >> 1) & 0x3F in (39, 40)


def _iter_mdat_nals(path: Path):
    """遍历 mp4 mdat 中长度前缀的 NAL 单元，产出 SEI 候选。"""
    with open(path, "rb") as fp:
        # 定位 mdat atom
        size = None
        while True:
            header = fp.read(8)
            if len(header) < 8:
                return
            size32, atom = struct.unpack(">I4s", header)
            if size32 == 1:
                ext = fp.read(8)
                if len(ext) < 8:
                    return
                atom_size = struct.unpack(">Q", ext)[0]
                hdr = 16
            else:
                atom_size = size32 or 0
                hdr = 8
            if atom == b"mdat":
                # size=None 表示 mdat 延伸到文件尾（atom size 字段为 0）
                _offset, size = fp.tell(), (atom_size - hdr if atom_size else None)
                break
            if atom_size < hdr:
                return
            fp.seek(atom_size - hdr, 1)

        consumed = 0
        while size is None or consumed < size:
            raw = fp.read(4)
            if len(raw) < 4:
                return
            nal_size = struct.unpack(">I", raw)[0]
            consumed += 4 + nal_size
            if nal_size < 4 or nal_size > 10_000_000:
                fp.seek(nal_size, 1)
                continue
            head = fp.read(3)
            if len(head) < 3:
                return
            if _is_sei_nal(head[0], head[1]) or _is_sei_nal(head[0], head[2]):
                rest = fp.read(nal_size - 3)
                yield head + rest
            else:
                fp.seek(nal_size - 3, 1)


def probe_fps_exact(path: Path) -> float | None:
    """真实帧率；探测不出来返回 None。

    区分"探测不出来"和"就是 30"很重要：SEI 采样的时间轴是 `idx / fps` 算出来的，
    fps 猜错的话，由它派生的减速度（Δv/Δt）和里程（∫v·dt）全都是错的，
    但看上去和正确的数字一模一样。

    走 probe 模块的共享缓存：同一个文件不再为帧率、时长、编码各起一次 ffprobe。
    """
    from . import probe

    try:
        return probe.probe_sync(path).get("fps")
    except Exception:  # noqa: BLE001 - 文件损坏 / 没有 ffprobe / 超时，都算探测不到
        return None


def probe_fps(path: Path) -> float:
    """给**播放**用的帧率，探测不到就猜 30。

    播放器猜错帧率最多是叠加层的时间轴略有偏移，用户还是能看到画面；
    索引不能用这个 —— 走 probe_fps_exact，拿不到就别入库。
    """
    fps = probe_fps_exact(path)
    return fps if fps is not None else 30.0


_cache: dict[str, list[dict]] = {}


def extract(path: Path, step: float = 0.5, strict: bool = False) -> list[dict]:
    """提取单个片段的 SEI 序列，按 step 秒抽样，t 为片段内相对秒数。

    没有 SEI（旧固件/停车片段/加密片段）时返回空列表。

    strict=True 时额外要求帧率能被 ffprobe 确定；确定不了就返回空。
    索引走严格模式，播放走宽松模式 —— 详见 probe_fps 的说明。
    """
    try:
        key = f"{path}:{path.stat().st_mtime}:{step}:{strict}"
    except OSError:
        return []  # 文件已消失（U 盘拔出等）
    if key in _cache:
        return _cache[key]
    if len(_cache) > 64:
        _cache.clear()

    frames: list[dict] = []
    try:
        for nal in _iter_mdat_nals(path):
            payload = _sei_payload(nal)
            if payload is None:
                continue
            meta = _decode_protobuf(payload)
            if meta is not None:          # {} 也是合法帧：全部字段都是默认值
                frames.append(meta)
    except OSError as e:
        log.warning("SEI read failed for %s: %s", path, e)
        _cache[key] = []
        return []

    if not frames:
        _cache[key] = []
        return []

    fps = probe_fps_exact(path) if strict else probe_fps(path)
    if fps is None:
        # 严格模式下宁可不给数据，也不给一份基于猜测帧率的"分析结果"
        log.warning("SEI: cannot determine fps for %s, refusing to guess", path.name)
        _cache[key] = []
        return []
    samples: list[dict] = []
    stride = max(round(step * fps), 1)
    for idx in range(0, len(frames), stride):
        f = frames[idx]
        samples.append({
            "t": round(idx / fps, 2),
            # proto3：字段缺失 = 默认值。gear 缺失就是 0 = P，speed 缺失就是 0 km/h ——
            # 这是真实的"停着"，不是"不知道"。当成未知的话，40 → 0 的急停算不出急刹，
            # 均速也被算成只有行驶时的平均。
            "gear": GEARS.get(f.get("gear", 0), ""),
            "speed_kmh": round(f.get("speed_mps", 0.0) * 3.6, 1),
            "accel_pedal": round(f.get("accel_pedal", 0), 1),
            "steering_angle": round(f.get("steering_angle", 0), 1),
            "blinker_left": f.get("blinker_left", False),
            "blinker_right": f.get("blinker_right", False),
            "brake": f.get("brake", False),
            "ap": AP_STATES.get(f.get("ap_state", 0), ""),
            "lat": f.get("lat"),
            "lon": f.get("lon"),
            "heading": f.get("heading"),
            "accel_x": round(f["accel_x"], 3) if f.get("accel_x") is not None else None,
            "accel_y": round(f["accel_y"], 3) if f.get("accel_y") is not None else None,
        })
    _cache[key] = samples
    log.info("SEI extracted: %s -> %d frames, %d samples", path.name, len(frames), len(samples))
    return samples
