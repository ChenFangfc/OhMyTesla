"""把行车记录仪的 SEI 遥测一次性解析入库，并在其上做驾驶行为分析。

为什么要入库：`sei.extract()` 解一段 60 秒的片段要遍历上千个 NAL，看一次解一次
还能忍，但"整个素材库里所有急刹在哪"、"这个月 FSD 占了多少里程"这类问题
不落库就根本问不出来。入库一次之后全是 SQL。

单位口径（重要）：
- `speed_mps` 的语义是确定的（sei.py 用 `* 3.6` 转 km/h），所以由它派生的
  **减速度、里程、均速都是可信的**。
- `accel_x/y/z` 官方 proto 只写了 double，**没有给单位也没有给符号约定**。
  没有真实样片可校准之前，这三个字段只入库、不参与任何判定 —— 拿一个不知道
  单位的数去跟 "0.4g" 比大小，得到的结论是编的。
  因此急刹检测走**速度差**：Δv/Δt 的单位由 speed_mps 决定，确定无疑。
"""

import asyncio
import logging
import time
from pathlib import Path

from ..alerts.store import store
from . import sei
from .scanner import Event, scanner
from .telemetry import local_naive_to_utc

log = logging.getLogger(__name__)

# 急刹阈值，m/s²。日常刹车约 2 m/s²，4 m/s²（≈0.41g）是公认的"用力刹"分界，
# 6 m/s² 以上基本是紧急制动。取 4 是为了既抓得住又不至于满屏都是。
HARD_BRAKE_MS2 = 4.0
# 低于这个速度的减速不算急刹：停车场里从 8 km/h 刹停也能算出很大的减速度，
# 但那不是值得回看的事。
MIN_BRAKE_SPEED_KMH = 25.0
# 自驾退出只在车确实在动时才算：停车场里挂 P 退出 AP 不是"接管"。
MIN_DISENGAGE_SPEED_KMH = 10.0

_SELF_DRIVING = {"FSD", "AP"}


def _pick_camera(cameras: dict[str, str]) -> tuple[str, str] | None:
    """一段里选一路摄像头做 SEI 解析。

    SEI 是整车级的遥测，四路里任何一路都带同一份数据，所以只解一路就够。
    优先前视：它最不容易缺（哨兵片段有时只有侧后）。
    """
    for cam in ("front", "F", "main"):
        if cam in cameras:
            return cam, cameras[cam]
    for cam, rel in cameras.items():
        return cam, rel
    return None


def _analyse(samples: list[dict], seg_epoch: float) -> tuple[dict, list[dict]]:
    """把一段的采样序列折成聚合量 + 检出的事件。"""
    agg = {
        "samples": len(samples),
        "duration_s": 0.0,
        "fsd_s": 0.0, "ap_s": 0.0, "tacc_s": 0.0, "manual_s": 0.0,
        "dist_km": 0.0,
        "speed_max": None, "speed_avg": None,
        "disengage": 0,
        "lat": None, "lon": None,
    }
    events: list[dict] = []
    if len(samples) < 2:
        return agg, events

    speeds = [s["speed_kmh"] for s in samples if s.get("speed_kmh") is not None]
    if speeds:
        agg["speed_max"] = round(max(speeds), 1)
        agg["speed_avg"] = round(sum(speeds) / len(speeds), 1)
    for s in samples:
        if s.get("lat") is not None and s.get("lon") is not None:
            agg["lat"], agg["lon"] = s["lat"], s["lon"]
            break

    for i in range(1, len(samples)):
        prev, cur = samples[i - 1], samples[i]
        dt = cur["t"] - prev["t"]
        # 采样是均匀的，异常的 dt 说明中间掉帧了；跨过它比拿它算减速度安全
        if dt <= 0 or dt > 3:
            continue
        agg["duration_s"] += dt

        mode = cur.get("ap") or ""
        key = {"FSD": "fsd_s", "AP": "ap_s", "TACC": "tacc_s"}.get(mode, "manual_s")
        agg[key] += dt

        v0, v1 = prev.get("speed_kmh"), cur.get("speed_kmh")
        if v0 is None or v1 is None:
            continue
        # 梯形法积里程：(v0+v1)/2 * dt，比拿单点速度乘 dt 更接近真实
        agg["dist_km"] += (v0 + v1) / 2 / 3600 * dt

        # 减速度由速度差算，单位 m/s²（km/h → m/s 要除 3.6）
        decel = (v0 - v1) / 3.6 / dt
        if decel >= HARD_BRAKE_MS2 and v0 >= MIN_BRAKE_SPEED_KMH:
            events.append({
                "kind": "hard_brake",
                "t_offset": cur["t"],
                "ts": seg_epoch + cur["t"],
                "magnitude": round(decel, 2),
                "speed_kmh": round(v0, 1),
                "mode": mode or "manual",
                "lat": cur.get("lat"), "lon": cur.get("lon"),
            })

        # 自驾退出：上一采样在 FSD/AP，这一采样不在，且车还在动
        prev_mode = prev.get("ap") or ""
        if prev_mode in _SELF_DRIVING and mode not in _SELF_DRIVING and v1 >= MIN_DISENGAGE_SPEED_KMH:
            agg["disengage"] += 1
            events.append({
                "kind": "disengage",
                "t_offset": cur["t"],
                "ts": seg_epoch + cur["t"],
                "magnitude": round(v1, 1),
                "speed_kmh": round(v1, 1),
                "mode": prev_mode,
                "lat": cur.get("lat"), "lon": cur.get("lon"),
            })

    for k in ("duration_s", "fsd_s", "ap_s", "tacc_s", "manual_s"):
        agg[k] = round(agg[k], 2)
    agg["dist_km"] = round(agg["dist_km"], 3)
    return agg, events


async def index_event(ev: Event, base: Path, force: bool = False) -> int:
    """解析一个事件下所有分段的 SEI 并入库，返回新解析的分段数。

    幂等：路径 + mtime 没变就跳过。U 盘重新挂载导致 mtime 变化时会重解，
    代价是一次多余的解析，比漏掉新片段安全。
    """
    done = 0
    for seg in ev.segments:
        picked = _pick_camera(seg.cameras)
        if not picked:
            continue
        cam, rel = picked
        full = base / rel
        try:
            mtime = full.stat().st_mtime
        except OSError:
            log.debug("suppressed", exc_info=True)
            continue

        seg_epoch = local_naive_to_utc(seg.start).timestamp()
        if not force:
            # 按 (event_id, seg_start) 而不是 path 查。同一段在不同轮次可能被
            # 选到不同摄像头：scanner 会跳过 0 字节的文件，车正在写 front 时
            # 这一段以 back 入库，下次 front 完整了又以 front 入一行 ——
            # 那 60 秒的 FSD 时长、里程、急刹就被算了两遍。
            row = await store.db.execute_fetchall(
                "SELECT mtime FROM clip_sei WHERE event_id = ? AND seg_start = ?",
                (ev.id, seg_epoch),
            )
            if row and abs(row[0]["mtime"] - mtime) < 1e-6:
                continue

        # NAL 遍历是纯 CPU 的阻塞活，扔给线程池；事件循环还要伺候门户的
        # 其它请求（uvicorn 单进程，没有第二个 worker 顶上）。
        # strict=True：帧率探测不出来就不入库。这一条不是洁癖 ——
        # 没有它的时候，2010 个 ffprobe 报 "moov atom not found" 的损坏文件
        # 照样被"解析"出 9200 个采样和 343 次急刹，然后当成驾驶分析显示出来。
        samples = await asyncio.to_thread(sei.extract, full, 0.5, True)
        # 帧率探不出来 = 文件读不出来（损坏、截断、加密）。和"能读但没有 SEI"
        # 是两回事，界面要给出不同的排查方向。
        readable = await asyncio.to_thread(sei.probe_fps_exact, full) is not None
        # 读不出来时再分一层：是加密（有解法）还是损坏（没解法）
        encrypted = False
        if not readable:
            from . import decrypt as _dec

            encrypted = await asyncio.to_thread(_dec.is_encrypted, full)
        agg, events = _analyse(samples, seg_epoch)

        await store.db.execute(
            """INSERT INTO clip_sei
               (path, event_id, camera, seg_start, mtime, indexed_at, duration_s, samples,
                fsd_s, ap_s, tacc_s, manual_s, dist_km, speed_max, speed_avg, disengage,
                lat, lon, readable, encrypted)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id, seg_start) DO UPDATE SET
                 path=excluded.path, camera=excluded.camera,
                 mtime=excluded.mtime, indexed_at=excluded.indexed_at,
                 duration_s=excluded.duration_s, samples=excluded.samples,
                 fsd_s=excluded.fsd_s, ap_s=excluded.ap_s, tacc_s=excluded.tacc_s,
                 manual_s=excluded.manual_s, dist_km=excluded.dist_km,
                 speed_max=excluded.speed_max, speed_avg=excluded.speed_avg,
                 disengage=excluded.disengage, lat=excluded.lat, lon=excluded.lon,
                 readable=excluded.readable, encrypted=excluded.encrypted""",
            (rel, ev.id, cam, seg_epoch, mtime, time.time(),
             agg["duration_s"], agg["samples"], agg["fsd_s"], agg["ap_s"], agg["tacc_s"],
             agg["manual_s"], agg["dist_km"], agg["speed_max"], agg["speed_avg"],
             agg["disengage"], agg["lat"], agg["lon"], int(readable), int(encrypted)),
        )
        # 重解时先清掉这一段的旧事件。按 event_id + 时间窗删而不是按 path：
        # 上一轮可能是用另一路摄像头入的库，按 path 删会把旧记录留下来。
        await store.db.execute(
            "DELETE FROM sei_event WHERE event_id = ? AND ts >= ? AND ts < ?",
            (ev.id, seg_epoch, seg_epoch + 3600),
        )
        for e in events:
            await store.db.execute(
                """INSERT OR IGNORE INTO sei_event
                   (path, event_id, kind, ts, t_offset, magnitude, speed_kmh, mode, lat, lon)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (rel, ev.id, e["kind"], e["ts"], e["t_offset"], e["magnitude"],
                 e["speed_kmh"], e["mode"], e["lat"], e["lon"]),
            )
        done += 1
    await store.db.commit()
    return done


async def reindex(force: bool = False, limit: int | None = None) -> dict:
    """把素材库里所有事件过一遍。"""
    from .scanner import _teslacam_base

    base = _teslacam_base()
    if base is None:
        return {"ok": False, "error": "TeslaCam directory not configured or missing"}

    events = scanner.scan()
    if limit:
        events = events[:limit]
    total = 0
    for ev in events:
        try:
            total += await index_event(ev, base, force=force)
        except Exception as e:  # noqa: BLE001
            log.warning("SEI index failed for %s: %s", ev.id, e)
    return {"ok": True, "events": len(events), "segments_indexed": total}


# ---------------- 分析查询 ----------------

def _since_clause(days: int | None) -> tuple[str, list]:
    if not days or days <= 0:
        return "", []
    return " AND seg_start >= ?", [time.time() - days * 86400]


async def fsd_summary(days: int | None = 90) -> dict:
    """自驾使用总览：各模式的时长、里程占比、接管次数。

    这是 Owner API 和 TeslaMate 都拿不到的维度 —— 它只存在于行车记录仪的
    SEI 里，所以这份统计不花一分钱 API 费用，也不需要联网。
    """
    where, args = _since_clause(days)
    row = (await store.db.execute_fetchall(
        f"""SELECT COALESCE(SUM(fsd_s),0) fsd, COALESCE(SUM(ap_s),0) ap,
                   COALESCE(SUM(tacc_s),0) tacc, COALESCE(SUM(manual_s),0) manual,
                   COALESCE(SUM(dist_km),0) km, COALESCE(SUM(disengage),0) dis,
                   COUNT(*) clips, MAX(speed_max) vmax,
                   MIN(seg_start) first_seen, MAX(seg_start) last_seen
            FROM clip_sei WHERE 1=1{where}""", args))[0]  # noqa: S608 - where 由 _since_clause 生成的字面量；时间戳走 ? 绑定

    fsd, ap, tacc, manual = row["fsd"], row["ap"], row["tacc"], row["manual"]
    total = fsd + ap + tacc + manual
    auto = fsd + ap

    # 每次接管平均管了多久 —— 比"接管了 N 次"更能说明自驾好不好用
    per_disengage = round(auto / row["dis"] / 60, 1) if row["dis"] else None

    return {
        "days": days,
        "clips": row["clips"],
        "has_data": total > 0,
        "seconds": {"fsd": round(fsd), "ap": round(ap), "tacc": round(tacc),
                    "manual": round(manual), "total": round(total)},
        "share": {
            "fsd": round(fsd / total * 100, 1) if total else None,
            "ap": round(ap / total * 100, 1) if total else None,
            "tacc": round(tacc / total * 100, 1) if total else None,
            "autonomous": round(auto / total * 100, 1) if total else None,
        },
        "dist_km": round(row["km"], 1),
        "speed_max": row["vmax"],
        "disengagements": row["dis"],
        "minutes_per_disengagement": per_disengage,
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
    }


async def fsd_trend(days: int = 180, bucket_days: int = 7) -> list[dict]:
    """自驾占比随时间的走势，按固定天数分桶。

    用整数除法分桶而不是 SQLite 的 strftime：strftime 要先把 epoch 转成本地
    日历，而这里的时间基准是车机本地时钟，两次转换只会引入偏差。
    """
    where, args = _since_clause(days)
    span = bucket_days * 86400
    rows = await store.db.execute_fetchall(
        f"""SELECT CAST(seg_start / ? AS INTEGER) * ? AS bucket,
                   COALESCE(SUM(fsd_s),0) fsd, COALESCE(SUM(ap_s),0) ap,
                   COALESCE(SUM(tacc_s),0) tacc, COALESCE(SUM(manual_s),0) manual,
                   COALESCE(SUM(dist_km),0) km, COALESCE(SUM(disengage),0) dis
            FROM clip_sei WHERE 1=1{where}
            GROUP BY bucket ORDER BY bucket""",  # noqa: S608 - where 同上；分桶跨度走 ? 绑定
        [span, span, *args],
    )
    out = []
    for r in rows:
        total = r["fsd"] + r["ap"] + r["tacc"] + r["manual"]
        out.append({
            "start": r["bucket"],
            "autonomous_pct": round((r["fsd"] + r["ap"]) / total * 100, 1) if total else None,
            "fsd_pct": round(r["fsd"] / total * 100, 1) if total else None,
            "dist_km": round(r["km"], 1),
            "disengagements": r["dis"],
            "minutes": round(total / 60, 1),
        })
    return out


async def driving_events(kind: str | None = None, days: int | None = 90,
                         limit: int = 200) -> list[dict]:
    """检出的驾驶事件，最新在前。"""
    sql = "SELECT * FROM sei_event WHERE 1=1"
    args: list = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if days and days > 0:
        sql += " AND ts >= ?"
        args.append(time.time() - days * 86400)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    rows = await store.db.execute_fetchall(sql, args)
    return [
        {
            "kind": r["kind"], "ts": r["ts"], "event_id": r["event_id"],
            "path": r["path"], "seek": round(r["t_offset"], 1),
            "magnitude": r["magnitude"], "speed_kmh": r["speed_kmh"],
            "mode": r["mode"], "lat": r["lat"], "lon": r["lon"],
        }
        for r in rows
    ]


async def index_status() -> dict:
    """入库进度，用来在界面上说清"分析基于多少素材"。"""
    row = (await store.db.execute_fetchall(
        """SELECT COUNT(*) clips, COALESCE(SUM(samples),0) samples,
                  COALESCE(SUM(duration_s),0) secs, MAX(indexed_at) last,
                  COUNT(*) FILTER (WHERE samples = 0 AND readable = 1) without_sei,
                  COUNT(*) FILTER (WHERE readable = 0 AND encrypted = 0) unreadable,
                  COUNT(*) FILTER (WHERE encrypted = 1) encrypted
           FROM clip_sei"""))[0]
    ev = (await store.db.execute_fetchall(
        "SELECT COUNT(*) n, COUNT(DISTINCT kind) kinds FROM sei_event"))[0]
    return {
        "clips_indexed": row["clips"],
        # 停车哨兵片段和 2025.44.25 之前的固件没有 SEI，这不是错误。
        # 明确报出来，用户才不会以为解析坏了。
        "clips_without_sei": row["without_sei"],
        # 读不出来的片段。原因通常是文件损坏/截断，或者固件 2026.20+ 的加密。
        # 和"没有 SEI"分开报：前者查 U 盘，后者查固件版本。
        "clips_unreadable": row["unreadable"],
        # 加密单独一类：车机里 控制 > 安全 可以关，或者用账号换密钥解开。
        # 和"文件坏了"混在一起报的话，一半用户会去做完全无用的排查。
        "clips_encrypted": row["encrypted"],
        "samples": row["samples"],
        "seconds": round(row["secs"]),
        "events": ev["n"],
        "last_indexed": row["last"],
        "thresholds": {
            "hard_brake_ms2": HARD_BRAKE_MS2,
            "min_brake_speed_kmh": MIN_BRAKE_SPEED_KMH,
            "min_disengage_speed_kmh": MIN_DISENGAGE_SPEED_KMH,
        },
    }
