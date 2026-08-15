"""接管 TeslaMate 的配置界面 —— 登录、地理围栏、车辆设置。

把这三样搬进 OhmyTesla 之后，用户就不需要再打开 TeslaMate 的网页了。
数据仍然写进 TeslaMate 自己的表，采集内核完全不变。
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..tesla import vault

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])


# ---------------- 登录 ----------------

@router.get("/status")
async def status():
    """引导页要知道：连上数据库了吗？登录了吗？有车了吗？"""
    out = {
        "db": False, "signed_in": False, "cars": 0,
        "vault_ok": vault.selftest(), "error": "",
    }
    try:
        out["cars"] = int(await db.fetchval("SELECT count(*) FROM cars") or 0)
        out["db"] = True
        out["signed_in"] = await vault.has_tokens()
    except db.DbUnavailable as e:
        out["error"] = str(e)
    except Exception as e:  # noqa: BLE001 - 首次启动表还没建好
        out["error"] = str(e)
    return out


class SignIn(BaseModel):
    access: str
    refresh: str


@router.post("/signin")
async def signin(body: SignIn):
    if not body.access.strip() or not body.refresh.strip():
        raise HTTPException(400, "Both tokens are required")
    # 粗校验：Tesla 的 access token 是 JWT，三段式
    if body.access.count(".") < 2:
        raise HTTPException(400, "That doesn't look like an access token — it should be a long JWT with two dots")
    try:
        await vault.save_tokens(body.access, body.refresh)
    except vault.VaultError as e:
        raise HTTPException(400, str(e)) from e
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return {"ok": True}


# ---------------- 地理围栏 ----------------

@router.get("/geofences")
async def list_geofences():
    try:
        rows = await db.fetch(
            "SELECT id, name, latitude, longitude, radius, billing_type,"
            " cost_per_unit, session_fee FROM geofences ORDER BY name"
        )
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return [
        {
            "id": r["id"], "name": r["name"],
            "latitude": float(r["latitude"]), "longitude": float(r["longitude"]),
            "radius": r["radius"],
            "billing_type": r["billing_type"],
            "cost_per_unit": float(r["cost_per_unit"]) if r["cost_per_unit"] is not None else None,
            "session_fee": float(r["session_fee"]) if r["session_fee"] is not None else None,
        }
        for r in rows
    ]


class Geofence(BaseModel):
    name: str
    latitude: float
    longitude: float
    radius: int = 100


@router.post("/geofences")
async def create_geofence(body: Geofence):
    if not body.name.strip():
        raise HTTPException(400, "Name is required")
    if not (-90 <= body.latitude <= 90 and -180 <= body.longitude <= 180):
        raise HTTPException(400, "Coordinates are out of range")
    radius = max(10, min(int(body.radius), 10000))
    try:
        await db.execute(
            "INSERT INTO geofences (name, latitude, longitude, radius, inserted_at, updated_at)"
            " VALUES ($1, $2, $3, $4, now(), now())",
            body.name.strip(), body.latitude, body.longitude, radius,
        )
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return {"ok": True}


@router.put("/geofences/{gid}")
async def update_geofence(gid: int, body: Geofence):
    radius = max(10, min(int(body.radius), 10000))
    status = await db.execute(
        "UPDATE geofences SET name = $1, latitude = $2, longitude = $3, radius = $4,"
        " updated_at = now() WHERE id = $5",
        body.name.strip(), body.latitude, body.longitude, radius, gid,
    )
    if str(status).endswith(" 0"):
        raise HTTPException(404, "Geofence not found")
    return {"ok": True}


@router.delete("/geofences/{gid}")
async def delete_geofence(gid: int):
    # 先解开引用，否则外键会挡住删除
    for stmt in (
        "UPDATE charging_processes SET geofence_id = NULL WHERE geofence_id = $1",
        "UPDATE drives SET start_geofence_id = NULL WHERE start_geofence_id = $1",
        "UPDATE drives SET end_geofence_id = NULL WHERE end_geofence_id = $1",
    ):
        try:
            await db.execute(stmt, gid)
        except Exception:  # noqa: BLE001
            pass
    await db.execute("DELETE FROM geofences WHERE id = $1", gid)
    return {"ok": True}


# ---------------- 车辆设置 ----------------

@router.get("/cars")
async def list_cars():
    try:
        rows = await db.fetch(
            """
            SELECT c.id, c.name, c.model, c.marketing_name, c.vin, c.efficiency,
                   s.suspend_min, s.suspend_after_idle_min, s.req_not_unlocked,
                   s.free_supercharging, s.use_streaming_api, s.enabled, s.lfp_battery
            FROM cars c LEFT JOIN car_settings s ON s.id = c.settings_id
            ORDER BY c.id
            """
        )
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return [dict(r) for r in rows]


class CarSettings(BaseModel):
    name: str | None = None
    suspend_min: int | None = None
    suspend_after_idle_min: int | None = None
    free_supercharging: bool | None = None
    lfp_battery: bool | None = None
    enabled: bool | None = None


@router.post("/cars/{car_id}")
async def update_car(car_id: int, body: CarSettings):
    try:
        if body.name is not None:
            await db.execute(
                "UPDATE cars SET name = $1, updated_at = now() WHERE id = $2",
                body.name.strip() or None, car_id,
            )
        fields, args = [], []
        for col, val, lo, hi in (
            ("suspend_min", body.suspend_min, 12, 300),
            ("suspend_after_idle_min", body.suspend_after_idle_min, 1, 300),
        ):
            if val is not None:
                fields.append(col)
                args.append(max(lo, min(int(val), hi)))
        for col, val in (
            ("free_supercharging", body.free_supercharging),
            ("lfp_battery", body.lfp_battery),
            ("enabled", body.enabled),
        ):
            if val is not None:
                fields.append(col)
                args.append(bool(val))
        if fields:
            sets = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(fields))
            await db.execute(
                f"UPDATE car_settings SET {sets} WHERE id ="  # noqa: S608
                f" (SELECT settings_id FROM cars WHERE id = ${len(fields) + 1})",
                *args, car_id,
            )
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return {"ok": True}
