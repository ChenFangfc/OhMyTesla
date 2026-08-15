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


# TeslaMate 每次增删改地理围栏，都会在同一个事务里重跑一遍空间回填，把
# 落在围栏里的历史行程和充电记录重新挂上（locations.ex:127-157）。我们发的是
# 裸 SQL，不跑这段的话：新建的围栏对历史数据完全没有影响（充电统计里永远
# 看不到这个地点），改小围栏后跑出范围的记录还挂着旧围栏，删除则是把记录
# 直接置空而不是改挂到次近的那个。
#
# 下面是它那三条语句的逐字移植，连 earth_box 粗筛 + earth_distance 精算的
# 两段式写法都保持一致（earth_box 能走 GiST 索引，直接算距离不能）。
_GEOFENCE_BACKFILL = [
    ("drives", "start_geofence_id", "start_position_id"),
    ("drives", "end_geofence_id", "end_position_id"),
    ("charging_processes", "geofence_id", "position_id"),
]


async def _apply_geofence(lat: float, lon: float, radius: int, except_id: int = -1) -> None:
    """重新计算落在这个围栏范围内的历史记录该归属哪个围栏。"""
    for table, geo_col, pos_col in _GEOFENCE_BACKFILL:
        try:
            await db.execute(
                f"""
                UPDATE {table} m
                SET {geo_col} = (
                    SELECT id FROM geofences g
                    WHERE earth_box(ll_to_earth(g.latitude, g.longitude), g.radius)
                              @> ll_to_earth(p.latitude, p.longitude)
                      AND earth_distance(ll_to_earth(g.latitude, g.longitude),
                                         ll_to_earth(p.latitude, p.longitude)) < g.radius
                      AND g.id != $4
                    ORDER BY earth_distance(ll_to_earth(g.latitude, g.longitude),
                                            ll_to_earth(p.latitude, p.longitude)) ASC
                    LIMIT 1
                )
                FROM positions p
                WHERE m.{pos_col} = p.id
                  AND earth_box(ll_to_earth($1::numeric, $2::numeric), $3)
                          @> ll_to_earth(p.latitude, p.longitude)
                  AND earth_distance(ll_to_earth($1::numeric, $2::numeric),
                                     ll_to_earth(p.latitude, p.longitude)) < $3
                """,  # noqa: S608 - 表名与列名都来自上面的常量表
                lat, lon, radius, except_id,
            )
        except db.DbUnavailable:
            raise
        except Exception:  # noqa: BLE001 - earthdistance 扩展缺失等，回填失败不该让 CRUD 整个失败
            log.warning("geofence backfill failed for %s.%s", table, geo_col, exc_info=True)


@router.post("/geofences")
async def create_geofence(body: Geofence):
    if not body.name.strip():
        raise HTTPException(400, "Name is required")
    if not (-90 <= body.latitude <= 90 and -180 <= body.longitude <= 180):
        raise HTTPException(400, "Coordinates are out of range")
    # 上界跟 TeslaMate 自己的约束保持一致（geo_fence.ex:32 是
    # greater_than: 0, less_than: 5000）。放到 10000 的话，我们能写进去
    # 一个它自己的界面会判为非法的围栏。
    radius = max(10, min(int(body.radius), 4999))
    try:
        await db.execute(
            "INSERT INTO geofences (name, latitude, longitude, radius, inserted_at, updated_at)"
            " VALUES ($1, $2, $3, $4, now(), now())",
            body.name.strip(), body.latitude, body.longitude, radius,
        )
        # 挂上历史记录，否则这个地点在充电/行程统计里永远是空的
        await _apply_geofence(body.latitude, body.longitude, radius)
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return {"ok": True}


@router.put("/geofences/{gid}")
async def update_geofence(gid: int, body: Geofence):
    # 上界跟 TeslaMate 自己的约束保持一致（geo_fence.ex:32 是
    # greater_than: 0, less_than: 5000）。放到 10000 的话，我们能写进去
    # 一个它自己的界面会判为非法的围栏。
    radius = max(10, min(int(body.radius), 4999))
    if not body.name.strip():
        raise HTTPException(400, "Name is required")
    if not (-90 <= body.latitude <= 90 and -180 <= body.longitude <= 180):
        raise HTTPException(400, "Coordinates are out of range")
    try:
        old = await db.fetchrow(
            "SELECT latitude, longitude, radius FROM geofences WHERE id = $1", gid)
        if old is None:
            raise HTTPException(404, "Geofence not found")
        # 先按旧范围松绑（把只因这个围栏才挂上的记录改挂到次近的），
        # 改完再按新范围重新挂 —— 与 TeslaMate 的顺序一致
        await _apply_geofence(float(old["latitude"]), float(old["longitude"]),
                              int(old["radius"]), except_id=gid)
        status = await db.execute(
            "UPDATE geofences SET name = $1, latitude = $2, longitude = $3, radius = $4,"
            " updated_at = now() WHERE id = $5",
            body.name.strip(), body.latitude, body.longitude, radius, gid,
        )
        if str(status).endswith(" 0"):
            raise HTTPException(404, "Geofence not found")
        await _apply_geofence(body.latitude, body.longitude, radius)
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return {"ok": True}


@router.delete("/geofences/{gid}")
async def delete_geofence(gid: int):
    try:
        row = await db.fetchrow(
            "SELECT latitude, longitude, radius FROM geofences WHERE id = $1", gid)
        if row is None:
            raise HTTPException(404, "Geofence not found")

        # 先把这个围栏范围内的记录改挂到**次近的**围栏（except 掉自己）。
        # 以前是直接把三列置空，于是删掉一个大围栏会连带把套在它里面的小围栏
        # 上的历史记录一起清空 —— TeslaMate 自己是重新分配而不是清空。
        await _apply_geofence(float(row["latitude"]), float(row["longitude"]),
                              int(row["radius"]), except_id=gid)

        # 回填只覆盖得到落在范围内的行；范围外仍指向它的（改过围栏位置留下的
        # 陈旧引用）还得解开，否则外键挡住删除
        for stmt in (
            "UPDATE charging_processes SET geofence_id = NULL WHERE geofence_id = $1",
            "UPDATE drives SET start_geofence_id = NULL WHERE start_geofence_id = $1",
            "UPDATE drives SET end_geofence_id = NULL WHERE end_geofence_id = $1",
        ):
            await db.execute(stmt, gid)
        await db.execute("DELETE FROM geofences WHERE id = $1", gid)
    except db.DbUnavailable as e:
        raise HTTPException(503, str(e)) from e
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

    # TeslaMate 把 cars / car_settings 缓存在自己的进程里：它自己的界面改这些
    # 值时会经 Phoenix.PubSub 广播出去（settings.ex:47-57），而我们发的是裸 SQL，
    # 广播不了。所以库里改了、跑着的 TeslaMate 还用旧值 —— 必须如实告诉用户，
    # 否则他会以为"设了休眠参数却不生效"是个 bug。
    return {"ok": True, "needs_restart": bool(fields) or body.name is not None}
