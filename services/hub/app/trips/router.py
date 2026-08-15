"""行程标记（公务/私人/自定义）与导出（CSV / GPX）。

为什么值得做：所有商业记录器（TeslaFi、Stats、Tessie）都有"给行程打标签，
然后导出一份能报销/报税的里程表"，而这恰恰是自托管方案最该擅长的事
——数据本来就在你自己机器上。

两条硬规矩：
1. **标记存 hub 自己的 SQLite**，只用 TeslaMate 的 drive_id 做外键，
   绝不往它的表里加列。它的迁移是自动跑的，加列必冲突。
2. **导出的里程用 TeslaMate 的原始值**，不做任何重新推算。报销单据上的
   数字必须能和 TeslaMate/Grafana 对得上，不然没人敢用。
"""

import csv
import io
import logging
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from .. import db
from ..alerts.store import store
from ..i18n import prefs as user_prefs
from ..i18n import units

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trips", tags=["trips"])

# 预置分类。自定义标签走 `tag` 自由文本，这几个只是常用项，
# 报税/报销场景下 business/personal 的区分是刚需。
CATEGORIES = ("business", "personal", "commute", "charging", "service", "other")


class TagBody(BaseModel):
    category: str | None = None
    note: str | None = None


async def _tags() -> dict:
    return await store.get_setting("drive_tags", {}) or {}


@router.get("/tags")
async def list_tags():
    return await _tags()


@router.post("/tags/{drive_id}")
async def set_tag(drive_id: int, body: TagBody):
    cat = (body.category or "").strip().lower()
    if cat and cat not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {', '.join(CATEGORIES)}")
    tags = await _tags()
    key = str(drive_id)
    if not cat and not (body.note or "").strip():
        tags.pop(key, None)            # 清空 = 取消标记
    else:
        entry = tags.get(key) or {}
        if body.category is not None:
            entry["category"] = cat
        if body.note is not None:
            entry["note"] = body.note.strip()[:200]
        tags[key] = entry
    await store.set_setting("drive_tags", tags)
    return {"ok": True, "tags": tags.get(key)}


async def _rows(car_id: int, days: int, category: str | None) -> list[dict]:
    win = ""
    args: list = [car_id]
    if days and days > 0:
        win = " AND d.start_date >= now() - ($2::int * INTERVAL '1 day')"
        args.append(int(days))
    rows = await db.fetch(
        f"""
        SELECT d.id, d.start_date, d.end_date, d.distance, d.duration_min,
               d.start_km, d.end_km, d.speed_max, d.outside_temp_avg,
               GREATEST(d.start_rated_range_km - d.end_rated_range_km, 0) AS rated_used,
               c.efficiency,
               COALESCE(sg.name, sa.display_name, '') AS from_loc,
               COALESCE(eg.name, ea.display_name, '') AS to_loc
        FROM drives d
        JOIN cars c ON c.id = d.car_id
        LEFT JOIN addresses sa ON sa.id = d.start_address_id
        LEFT JOIN addresses ea ON ea.id = d.end_address_id
        LEFT JOIN geofences sg ON sg.id = d.start_geofence_id
        LEFT JOIN geofences eg ON eg.id = d.end_geofence_id
        WHERE d.car_id = $1 AND d.distance > 0.1{win}
        ORDER BY d.start_date DESC
        """,  # noqa: S608 - win 是本文件的固定片段
        *args,
    )
    tags = await _tags()
    out = []
    for r in rows:
        t = tags.get(str(r["id"])) or {}
        if category and (t.get("category") or "") != category:
            continue
        out.append({**dict(r), "category": t.get("category", ""), "note": t.get("note", "")})
    return out


@router.get("/export.csv")
async def export_csv(
    car_id: int,
    days: int = 0,
    category: str | None = Query(None, description="只导出这个分类"),
):
    """报销/报税用的里程表。单位跟用户偏好走——报销单上写 km 还是 mi 很要紧。"""
    try:
        rows = await _rows(car_id, days, category)
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e

    # 单位跟用户偏好走 —— 报销单上写 km 还是 mi 是要紧事。
    # units.dist 返回 (值, 单位名)，这里只要值。
    p = await user_prefs.get()
    dist_unit = units.dist(1.0, p)[1]

    def to_dist(km: float) -> float:
        return units.dist(km, p, digits=3)[0]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "drive_id", "start", "end", f"distance_{dist_unit}", "duration_min",
        f"odometer_start_{dist_unit}", f"odometer_end_{dist_unit}",
        "energy_kwh", "from", "to", "category", "note",
    ])
    for r in rows:
        kwh = float(r["rated_used"] or 0) * float(r["efficiency"] or 0)
        w.writerow([
            r["id"],
            r["start_date"].isoformat(sep=" ", timespec="seconds") if r["start_date"] else "",
            r["end_date"].isoformat(sep=" ", timespec="seconds") if r["end_date"] else "",
            round(to_dist(float(r["distance"] or 0)), 2),
            r["duration_min"] or 0,
            round(to_dist(float(r["start_km"] or 0)), 1),
            round(to_dist(float(r["end_km"] or 0)), 1),
            round(kwh, 2),
            r["from_loc"], r["to_loc"], r["category"], r["note"],
        ])
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ohmytesla-trips.csv"'},
    )


@router.get("/{drive_id}/export.gpx")
async def export_gpx(drive_id: int):
    """单段行程导出 GPX，可直接喂给任何地图/运动软件。"""
    try:
        head = await db.fetchrow(
            "SELECT start_date, end_date FROM drives WHERE id = $1", drive_id)
        if head is None:
            raise HTTPException(404, "drive not found")
        pts = await db.fetch(
            "SELECT date, latitude, longitude, elevation, speed"
            " FROM positions WHERE drive_id = $1 AND latitude IS NOT NULL"
            " ORDER BY date", drive_id,
        )
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e

    name = f"OhmyTesla drive {drive_id}"
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="OhmyTesla" xmlns="http://www.topografix.com/GPX/1/1">',
        f"<trk><name>{xml_escape(name)}</name><trkseg>",
    ]
    for p in pts:
        seg = f'<trkpt lat="{float(p["latitude"]):.6f}" lon="{float(p["longitude"]):.6f}">'
        if p["elevation"] is not None:
            seg += f"<ele>{float(p['elevation']):.1f}</ele>"
        if p["date"] is not None:
            # positions.date 是 naive UTC，GPX 要求带 Z
            seg += f"<time>{p['date'].isoformat(timespec='seconds')}Z</time>"
        seg += "</trkpt>"
        parts.append(seg)
    parts.append("</trkseg></trk></gpx>")

    return Response(
        "\n".join(parts),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="drive-{drive_id}.gpx"'},
    )


@router.get("/summary")
async def summary(car_id: int, days: int = 0):
    """按分类汇总里程与能耗 —— 报销时要的就是这张表。"""
    try:
        rows = await _rows(car_id, days, None)
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e

    buckets: dict[str, dict] = {}
    for r in rows:
        cat = r["category"] or "untagged"
        b = buckets.setdefault(cat, {"category": cat, "drives": 0, "km": 0.0, "kwh": 0.0})
        b["drives"] += 1
        b["km"] += float(r["distance"] or 0)
        b["kwh"] += float(r["rated_used"] or 0) * float(r["efficiency"] or 0)
    return {
        "categories": sorted(
            [{**v, "km": round(v["km"], 1), "kwh": round(v["kwh"], 1)} for v in buckets.values()],
            key=lambda x: x["km"], reverse=True,
        ),
        "untagged": sum(1 for r in rows if not r["category"]),
        "total_drives": len(rows),
    }
