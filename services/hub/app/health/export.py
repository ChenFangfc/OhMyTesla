"""批量导出与配置备份。

「保证用户永远不会被锁死」是自托管相对商业产品最实在的一条承诺，
但只有真的能把数据完整拿走才算数。这里给三样：

- 行程 / 充电 / 停放 的 CSV（表格软件、报销、报税）
- 全量 JSON（换工具时能整份搬走）
- 配置备份（电价方案、告警规则、行程标记、保养记录……这些在 hub 自有库里，
  只备份 Postgres 是拿不到的）

配置备份里**不包含 token**：那是加密存在 TeslaMate 库里的，把它导成明文
放进一个用户会随手丢进网盘的文件里，是在给自己挖坑。
"""

import csv
import io
import json
import logging
import time
from datetime import timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import db
from ..alerts.sentry import tracker as sentry_tracker
from ..alerts.store import store
from ..i18n import prefs as user_prefs
from ..i18n import units

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/export", tags=["export"])

# 会被备份的设置键。**刻意列白名单而不是整表导出** —— 免得哪天加了个
# 存敏感信息的键，被无声地一起导出去。
BACKUP_KEYS = [
    "tariff_plan", "charge_costs", "cost_written_ids", "tariff_dirty",
    "alert_rules", "sentry_policies", "alert_snooze",
    "drive_tags", "maintenance", "maintenance_notified",
    "battery_baseline", "ice_baseline", "speed_limit_kmh",
    "prefs", "watcher_known", "clock_offsets",
]


def _iso(dt):
    return None if dt is None else dt.replace(tzinfo=timezone.utc).isoformat()


def _csv(rows: list[dict], header: list[str], filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([r.get(h, "") for h in header])
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _charges(car_id: int) -> list[dict]:
    rows = await db.fetch(
        """
        SELECT cp.id, cp.start_date, cp.end_date, cp.charge_energy_added,
               cp.charge_energy_used, cp.start_battery_level, cp.end_battery_level,
               cp.duration_min, cp.cost,
               COALESCE(g.name, a.display_name, '') AS location,
               COALESCE(fc.fast, false) AS is_dc, fc.max_power
        FROM charging_processes cp
        LEFT JOIN geofences g ON g.id = cp.geofence_id
        LEFT JOIN addresses a ON a.id = cp.address_id
        LEFT JOIN LATERAL (
            SELECT bool_or(fast_charger_present) AS fast, MAX(charger_power) AS max_power
            FROM charges WHERE charging_process_id = cp.id
        ) fc ON TRUE
        WHERE cp.car_id = $1 AND cp.charge_energy_added > 0.05
        ORDER BY cp.start_date DESC
        """,
        car_id,
    )
    computed = await store.get_setting("charge_costs", {}) or {}
    out = []
    for r in rows:
        cost = r["cost"]
        out.append({
            "id": r["id"],
            "start": _iso(r["start_date"]),
            "end": _iso(r["end_date"]),
            "location": r["location"],
            "kwh_added": round(float(r["charge_energy_added"] or 0), 2),
            "kwh_used": round(float(r["charge_energy_used"] or 0), 2),
            "soc_start": r["start_battery_level"],
            "soc_end": r["end_battery_level"],
            "duration_min": r["duration_min"],
            "peak_kw": r["max_power"],
            "type": "DC" if r["is_dc"] else "AC",
            "cost": float(cost) if cost is not None else computed.get(str(r["id"])),
            # 费用是车主手填/TeslaMate 算的，还是本工具的电价引擎推的 ——
            # 导出去做账时这个区别很要紧
            "cost_source": "recorded" if cost is not None else (
                "computed" if str(r["id"]) in computed else "unset"),
        })
    return out


@router.get("/charges.csv")
async def charges_csv(car_id: int):
    try:
        rows = await _charges(car_id)
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return _csv(rows, ["id", "start", "end", "location", "kwh_added", "kwh_used",
                       "soc_start", "soc_end", "duration_min", "peak_kw", "type",
                       "cost", "cost_source"], "ohmytesla-charges.csv")


@router.get("/idles.csv")
async def idles_csv(car_id: int, days: int = 365):
    """停放时段与掉电。"""
    from ..insights import queries

    try:
        v = await queries.vampire_drain(car_id, days)
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
    # vampire_drain 的 recent 只留最近 30 条（界面够用），但**导出不该截断** ——
    # 导出的意义就是"全部数据都能拿走"。这里直接用完整列表。
    p = await user_prefs.get()
    rows = [{
        "from": s["from"],
        "hours": s["hours"],
        f"range_lost_{units.dist(1.0, p)[1]}": units.dist(s["km_lost"], p, digits=2)[0],
        "kwh_per_day": s["kwh_per_day"],
    } for s in v.get("all", v.get("recent", []))]
    header = list(rows[0].keys()) if rows else ["from", "hours", "range_lost_km", "kwh_per_day"]
    return _csv(rows, header, "ohmytesla-idles.csv")


@router.get("/all.json")
async def all_json(car_id: int):
    """全量导出。换工具时能整份搬走 —— 不被锁死这件事得能兑现。"""
    from ..insights import queries

    try:
        return {
            "exported_at": time.time(),
            "car": dict((await db.fetchrow(
                "SELECT id, vin, name, model, efficiency FROM cars WHERE id = $1", car_id)) or {}),
            "summary": await queries.summary(car_id),
            "battery": await queries.battery_health(car_id),
            "drives": await queries.drives_list(car_id, 10000),
            "charges": await _charges(car_id),
            "vampire": await queries.vampire_drain(car_id, 3650),
            "drive_tags": await store.get_setting("drive_tags", {}) or {},
            "alerts": await store.recent_alerts(5000),
            "sentry_sessions": await sentry_tracker.recent(5000),
            "commands": await store.recent_commands(5000),
        }
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e


@router.get("/config.json")
async def config_backup():
    """配置备份。这些设置只存在 hub 自有的 SQLite 里，
    只备份 Postgres 是拿不到的（`./omt backup` 现在两个都备）。

    **不含 token** —— 那是加密存在 TeslaMate 库里的，导成明文丢进网盘等于自废加密。
    """
    data = {}
    for k in BACKUP_KEYS:
        v = await store.get_setting(k, None)
        if v is not None:
            data[k] = v
    body = json.dumps(
        {"version": 1, "exported_at": time.time(), "settings": data},
        ensure_ascii=False, indent=1,
    )
    return Response(
        body, media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="ohmytesla-config.json"'},
    )


@router.post("/config/restore")
async def config_restore(payload: dict):
    """恢复配置备份。只认白名单里的键，其余一律忽略。"""
    settings_in = (payload or {}).get("settings")
    if not isinstance(settings_in, dict):
        raise HTTPException(400, "not a config backup (missing 'settings')")
    restored = []
    for k, v in settings_in.items():
        if k not in BACKUP_KEYS:
            continue        # 备份文件是用户可编辑的，别让它往库里塞任意键
        await store.set_setting(k, v)
        restored.append(k)

    # 告警引擎把规则开关缓存在进程里，只有 set_rule 会失效它。恢复备份是
    # 直接写 settings 表的，不清缓存的话：备份里明明关掉的规则照样在推，
    # 界面上却显示"已关闭" —— 而且要等到下次重启才对得上。
    if "alert_rules" in restored:
        from ..alerts.engine import engine

        engine._enabled_cache = None
    log.info("restored %d config keys", len(restored))
    return {"ok": True, "restored": sorted(restored),
            "ignored": sorted(set(settings_in) - set(restored))}
