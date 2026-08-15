/* 浏览器端 TeslaCam SEI 逐帧遥测解码（固件 2025.44.25+）。
 * 与服务端 app/dashcam/sei.py 同构：mdat NAL 遍历 → 0x42 填充串 + 0x69 标记
 * 定位 protobuf 载荷 → 手写 wire format 解码。字段号见官方 dashcam.proto。 */
(function () {
  const GEARS = { 0: "P", 1: "D", 2: "R", 3: "N" };
  const AP = { 0: "", 1: "FSD", 2: "AP", 3: "TACC" };
  const FIELDS = {
    2: ["gear", "varint"], 3: ["seq", "varint"],
    4: ["speed_mps", "f32"], 5: ["accel_pedal", "f32"], 6: ["steering_angle", "f32"],
    7: ["blinker_left", "bool"], 8: ["blinker_right", "bool"], 9: ["brake", "bool"],
    10: ["ap_state", "varint"], 11: ["lat", "f64"], 12: ["lon", "f64"],
    13: ["heading", "f64"], 14: ["accel_x", "f64"], 15: ["accel_y", "f64"],
  };

  function decodeProto(bytes) {
    const out = {};
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let i = 0;
    const n = bytes.length;
    function varint() {
      let shift = 0n, result = 0n;
      while (i < n) {
        const b = BigInt(bytes[i++]);
        result |= (b & 0x7Fn) << shift;
        if (!(b & 0x80n)) return result;
        shift += 7n;
      }
      throw new Error("truncated");
    }
    try {
      while (i < n) {
        const tag = Number(varint());
        const field = tag >> 3, wire = tag & 7;
        let v;
        if (wire === 0) v = Number(varint());
        else if (wire === 5) { v = dv.getFloat32(i, true); i += 4; }
        else if (wire === 1) { v = dv.getFloat64(i, true); i += 8; }
        else if (wire === 2) { i += Number(varint()); continue; }
        else return null;
        const spec = FIELDS[field];
        if (spec) out[spec[0]] = spec[1] === "bool" ? !!v : v;
      }
    } catch (e) { return null; }
    return ("seq" in out || "speed_mps" in out) ? out : null;
  }

  function stripEmulation(bytes) {
    const out = new Uint8Array(bytes.length);
    let o = 0, zeros = 0;
    for (const b of bytes) {
      if (zeros >= 2 && b === 3) { zeros = 0; continue; }
      out[o++] = b;
      zeros = b === 0 ? zeros + 1 : 0;
    }
    return out.subarray(0, o);
  }

  function seiPayload(nal) {
    const limit = Math.min(nal.length - 1, 64);
    let i = 2;
    while (i < limit && nal[i] !== 0x42) i++;
    let run = 0;
    while (i < limit && nal[i] === 0x42) { i++; run++; }
    if (run >= 4 && i < nal.length - 1 && nal[i] === 0x69) {
      return stripEmulation(nal.subarray(i + 1, nal.length - 1));
    }
    return null;
  }

  function isSei(b0, b1, b2) {
    if ((b0 & 0x1f) === 6 && b1 === 5) return true;           // H.264
    const t = (b0 >> 1) & 0x3f;                                // HEVC prefix/suffix SEI
    return t === 39 || t === 40;
  }

  /* buffer: ArrayBuffer of a full clip mp4 → frames array */
  function extractFrames(buffer) {
    const dv = new DataView(buffer);
    const u8 = new Uint8Array(buffer);
    // 找 mdat
    let pos = 0, mdatStart = -1, mdatEnd = -1;
    while (pos + 8 <= buffer.byteLength) {
      let size = dv.getUint32(pos);
      const type = String.fromCharCode(u8[pos + 4], u8[pos + 5], u8[pos + 6], u8[pos + 7]);
      let hdr = 8;
      if (size === 1) { size = Number(dv.getBigUint64(pos + 8)); hdr = 16; }
      if (type === "mdat") {
        mdatStart = pos + hdr;
        // 截断的坏文件 size 可能超出实际长度，钳制到文件末尾
        mdatEnd = Math.min(size ? pos + size : buffer.byteLength, buffer.byteLength);
        break;
      }
      if (size < hdr) return [];
      pos += size;
    }
    if (mdatStart < 0) return [];

    const frames = [];
    let p = mdatStart;
    while (p + 4 <= mdatEnd) {
      const nalSize = dv.getUint32(p);
      p += 4;
      if (nalSize < 4 || nalSize > 10_000_000 || p + nalSize > mdatEnd) { p += nalSize; continue; }
      if (isSei(u8[p], u8[p + 1], u8[p + 2])) {
        const payload = seiPayload(u8.subarray(p, p + nalSize));
        if (payload) {
          const meta = decodeProto(payload);
          if (meta) frames.push(meta);
        }
      }
      p += nalSize;
    }
    return frames;
  }

  /* File → 按 step 秒抽样的样本数组（t 为片段内秒）。fps 按帧数/时长估算。 */
  async function extract(file, { step = 0.5, durationS = 60 } = {}) {
    if (file.size > 400 * 1024 * 1024) return [];  // 超大文件跳过，防浏览器内存爆
    const buffer = await file.arrayBuffer();
    let frames;
    try { frames = extractFrames(buffer); } catch (e) { return []; }  // 坏文件不炸 UI
    if (!frames.length) return [];
    const fps = Math.min(Math.max(frames.length / durationS, 5), 60);
    const stride = Math.max(Math.round(step * fps), 1);
    const samples = [];
    for (let idx = 0; idx < frames.length; idx += stride) {
      const f = frames[idx];
      samples.push({
        t: +(idx / fps).toFixed(2),
        gear: GEARS[f.gear] ?? "",
        speed_kmh: f.speed_mps != null ? +(f.speed_mps * 3.6).toFixed(1) : null,
        accel_pedal: f.accel_pedal != null ? +f.accel_pedal.toFixed(1) : null,
        steering_angle: f.steering_angle != null ? +f.steering_angle.toFixed(1) : null,
        blinker_left: !!f.blinker_left, blinker_right: !!f.blinker_right,
        brake: !!f.brake, ap: AP[f.ap_state ?? 0] ?? "",
        lat: f.lat, lon: f.lon,
        accel_x: f.accel_x, accel_y: f.accel_y,
      });
    }
    return samples;
  }

  window.omtSei = { extract };
})();
