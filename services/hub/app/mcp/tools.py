"""MCP 工具集：只读地回答关于这辆车的问题。

设计取舍：**给成型的分析工具，而不是只给一个 run_sql。** 让模型自己写
TeslaMate 的 SQL，它得先知道 `positions.date` 是 naive UTC、
`cars.efficiency` 的单位是 kWh/km、rated_range 差值才是能耗口径 ——
这些坑本项目已经踩过一遍并写在各自的查询里了。把那些查询包成工具，
答案才稳定。`run_sql` 作为兜底保留，但走的是硬只读通道。
"""

import json
import logging
import re

from .. import db
from ..alerts.store import store
from ..insights import queries
from ..livestate import live

log = logging.getLogger(__name__)

# run_sql 的护栏。三层：语句白名单 + 只读事务 + 行数/超时上限。
SQL_MAX_ROWS = 200
SQL_TIMEOUT_MS = 8000
_SQL_FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "vacuum", "reindex", "call", "do ",
    # 纵深防御：即使降权角色的读权限出了纰漏，也不让模型碰到 token 表。
    "tokens",
    # pg_sleep 不改数据，但它是时序侧信道的燃料：跨站发起的查询即使读不到
    # 响应体，也能靠"这次请求花了 3 秒还是 0 秒"把结果一位一位问出来。
    # statement_timeout 给的 8 秒预算正好够用。
    "pg_sleep", "pg_read_file", "pg_ls_dir", "lo_import", "lo_export",
    "dblink", "pg_stat_file",
)


def _first_car() -> int:
    for k in live.cars:
        if k.isdigit():
            return int(k)
    return 1


async def t_vehicle_state(**_) -> dict:
    """当前实时状态。"""
    snap = live.snapshot()
    return {"cars": snap, "note": "值全部是字符串，_seed_age 表示这是数据库补的历史值而非实时值"}


async def t_list_drives(car_id: int | None = None, limit: int = 20, **_) -> dict:
    cid = car_id or _first_car()
    return {"drives": await queries.drives_list(cid, min(max(limit, 1), 200))}


async def t_drive_detail(drive_id: int, **_) -> dict:
    track = await queries.drive_track(drive_id)
    # 轨迹点可能上千，给模型没意义；只回统计量和抽稀后的形状
    pts = track if isinstance(track, list) else track.get("points", [])
    speeds = [p.get("speed") for p in pts if p.get("speed") is not None]
    return {
        "drive_id": drive_id,
        "points": len(pts),
        "max_speed_kmh": max(speeds) if speeds else None,
        "avg_speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else None,
        "start_soc": pts[0].get("soc") if pts else None,
        "end_soc": pts[-1].get("soc") if pts else None,
        "sample": pts[:: max(len(pts) // 20, 1)][:20] if pts else [],
    }


async def t_charging(car_id: int | None = None, days: int = 0, **_) -> dict:
    return await queries.charging(car_id or _first_car(), days)


async def t_charge_curve(charging_process_id: int, **_) -> dict:
    return await queries.charge_curve(charging_process_id)


async def t_driving_stats(car_id: int | None = None, days: int = 0, **_) -> dict:
    return await queries.drives(car_id or _first_car(), days)


async def t_battery_health(car_id: int | None = None, **_) -> dict:
    return await queries.battery_health(car_id or _first_car())


async def t_vampire_drain(car_id: int | None = None, days: int = 90, **_) -> dict:
    return await queries.vampire_drain(car_id or _first_car(), days)


async def t_summary(car_id: int | None = None, **_) -> dict:
    return await queries.summary(car_id or _first_car())


async def t_records(car_id: int | None = None, **_) -> dict:
    return await queries.records(car_id or _first_car())


async def t_tpms(car_id: int | None = None, **_) -> dict:
    return await queries.tpms(car_id or _first_car())


async def t_alerts(limit: int = 30, **_) -> dict:
    return {"alerts": await store.recent_alerts(min(max(limit, 1), 200))}


async def t_sentry_sessions(limit: int = 30, **_) -> dict:
    from ..alerts.sentry import tracker
    return {"sessions": await tracker.recent(min(max(limit, 1), 200))}


async def t_schema(**_) -> dict:
    """TeslaMate 的表结构。模型要写 SQL 之前得先看这个。"""
    rows = await db.fetch(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ('cars','car_settings','drives','positions','charges',
                             'charging_processes','geofences','addresses','states','updates')
        ORDER BY table_name, ordinal_position
        """
    )
    tables: dict[str, list] = {}
    for r in rows:
        tables.setdefault(r["table_name"], []).append(f"{r['column_name']} {r['data_type']}")
    return {
        "tables": tables,
        "gotchas": [
            "所有 timestamp 列都是 naive UTC，没有时区信息",
            "cars.efficiency 单位是 kWh/km",
            "能耗口径 = (start_rated_range_km - end_rated_range_km) * cars.efficiency，不要用 charge_energy_used",
            "positions 没有 heading 列；方向要由相邻两点的经纬度推",
            "drives 表不存电量，SOC 要顺着 start_position_id / end_position_id 回 positions 取",
            "charging_processes.cost 多数情况下是空的，本项目另有电价引擎算出的值",
        ],
    }


# 受限角色。**这是最要紧的一层护栏**：compose 给 hub 的是 TeslaMate 自己的
# 数据库账号，而它是 superuser —— 也就是说没有这一层的话，run_sql 能调
# pg_read_file / pg_ls_dir 读容器里的任意文件，也能 SELECT private.tokens
# 拿到加密的 Tesla token 报文。而 run_sql 是开放给 AI 助手的。
#
# Postgres 默认就把 pg_read_file 这类函数限制给 superuser，所以只要把查询
# 降权到一个普通角色执行，那一整类问题自动消失；再对 private schema 不授权，
# token 表也读不到。关键字过滤保留作为纵深防御，但不再是唯一依靠。
_RO_ROLE = "omt_mcp_readonly"
_role_ready = False


async def _ensure_role(conn) -> bool:
    """幂等地建出受限角色并授权。建不出来就返回 False，调用方拒绝执行。"""
    global _role_ready
    if _role_ready:
        return True
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", _RO_ROLE)
        if not exists:
            await conn.execute(f'CREATE ROLE "{_RO_ROLE}" NOLOGIN')
        # 只给 public schema 的读权限；private（token 所在）一律不授
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{_RO_ROLE}"')
        await conn.execute(
            f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{_RO_ROLE}"')
        await conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            f'GRANT SELECT ON TABLES TO "{_RO_ROLE}"')
        # 上面那条 GRANT ALL TABLES 是按 schema 一把授的，装过旧版 TeslaMate
        # 的库里 tokens 表还留在 public（新版才搬进 private schema），
        # 于是这个"只读角色"顺带拿到了加密 token 报文的读权限。
        # 逐张收回，表不存在就跳过。
        for leaky in ("public.tokens", "public.oauth_tokens"):
            try:
                await conn.execute(f'REVOKE ALL ON TABLE {leaky} FROM "{_RO_ROLE}"')
            except Exception:  # noqa: BLE001 - 表不存在是正常情况
                pass
        _role_ready = True
        return True
    except Exception:
        log.exception("could not prepare the restricted SQL role")
        return False


async def t_run_sql(sql: str, **_) -> dict:
    """兜底的只读查询。四层护栏，任何一层不过就拒。"""
    q = (sql or "").strip().rstrip(";")
    low = q.lower()
    if not low.startswith(("select", "with")):
        return {"error": "只允许 SELECT / WITH 开头的只读查询"}
    if ";" in q:
        return {"error": "不允许多条语句"}
    # 注释对模型没有用处，却能用来把我们追加的 LIMIT 注释掉。直接不收。
    if "--" in q or "/*" in q or "*/" in q:
        return {"error": "不允许注释"}
    # 按**词**匹配，不是子串。子串匹配会把 `updated_at` / `inserted_at` 这类
    # 正当列名误判成 update / insert —— 而 describe_schema 恰恰把这些列名
    # 告诉了模型，等于自己给的建议自己不收。
    words = set(re.findall(r"[a-z_]+", low))
    for bad in _SQL_FORBIDDEN:
        if bad.strip() in words:
            return {"error": f"语句里出现了不允许的关键字：{bad.strip()}"}

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if not await _ensure_role(conn):
            return {"error": "受限角色未就绪，拒绝执行任意 SQL"}
        # 只读事务 + 降权 + 超时；跑完一律回滚
        tr = conn.transaction(readonly=True)
        await tr.start()
        try:
            await conn.execute(f'SET LOCAL ROLE "{_RO_ROLE}"')
            await conn.execute(f"SET LOCAL statement_timeout = {SQL_TIMEOUT_MS}")
            # 用服务端游标限行数，**不要**把查询包进 `SELECT * FROM (...) LIMIT n`：
            # 那种包法能被一个右括号顶出来 ——
            # `SELECT id FROM positions) _x LIMIT 5000 --` 实测能拿到 5000 行。
            # 游标是在协议层只取 N 行，查询自己写什么 LIMIT 都越不过去。
            cur = await conn.cursor(q)
            rows = await cur.fetch(SQL_MAX_ROWS)
            # 多取一行只为判断"是不是还有更多"，不返回给调用方
            more = await cur.fetch(1)
            return {
                "rows": [dict(r) for r in rows],
                "row_count": len(rows),
                "truncated": bool(more),
            }
        except Exception as e:  # noqa: BLE001 - 把数据库的报错原样告诉模型，它能据此改写
            return {"error": str(e)[:400]}
        finally:
            # rollback 会把 SET LOCAL ROLE 一并撤销，连接归还池子时是干净的
            await tr.rollback()


# name -> (handler, description, inputSchema)
def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


_CAR = {"car_id": {"type": "integer", "description": "TeslaMate 的车辆 id，省略则取第一辆"}}
_DAYS = {"days": {"type": "integer", "description": "只看最近 N 天，0 或省略 = 全部"}}

TOOLS: dict[str, tuple] = {
    "get_vehicle_state": (
        t_vehicle_state,
        "车辆当前实时状态：电量、位置、门窗锁、充电、哨兵、胎压等。",
        _schema({}),
    ),
    "list_drives": (
        t_list_drives,
        "最近的行程列表（时间、起终点、里程、时长、最高速）。",
        _schema({**_CAR, "limit": {"type": "integer", "description": "条数，最多 200"}}),
    ),
    "get_drive_detail": (
        t_drive_detail,
        "某一段行程的详细统计与抽稀轨迹。",
        _schema({"drive_id": {"type": "integer"}}, ["drive_id"]),
    ),
    "get_charging_stats": (
        t_charging,
        "充电统计：按月能耗与花费、常用地点、最近的充电记录。",
        _schema({**_CAR, **_DAYS}),
    ),
    "get_charge_curve": (
        t_charge_curve,
        "单次充电的功率-电量曲线（含电压电流），用于分析充电速度。",
        _schema({"charging_process_id": {"type": "integer"}}, ["charging_process_id"]),
    ),
    "get_driving_stats": (
        t_driving_stats,
        "行驶统计：按月里程与能耗、能耗随气温的变化曲线。",
        _schema({**_CAR, **_DAYS}),
    ),
    "get_battery_health": (
        t_battery_health,
        "电池健康：两种独立方法估算的容量与衰减百分比。",
        _schema(_CAR),
    ),
    "get_vampire_drain": (
        t_vampire_drain,
        "停放耗电：车停着不开时每天掉多少电。",
        _schema({**_CAR, **_DAYS}),
    ),
    "get_lifetime_summary": (
        t_summary,
        "全周期汇总：总里程、总充电量、总花费、平均能耗、固件版本。",
        _schema(_CAR),
    ),
    "get_records": (
        t_records,
        "历史极值：最高速、最大功率、最强回收、最长行程、最高海拔等。",
        _schema(_CAR),
    ),
    "get_tire_pressures": (
        t_tpms,
        "四轮胎压的最新值与 30 天趋势。",
        _schema(_CAR),
    ),
    "list_alerts": (
        t_alerts,
        "最近的告警历史（哨兵、胎压、充电中断、采集中断等）。",
        _schema({"limit": {"type": "integer"}}),
    ),
    "list_sentry_sessions": (
        t_sentry_sessions,
        "哨兵活动会话：时间、时长、活动次数、严重程度、地点。",
        _schema({"limit": {"type": "integer"}}),
    ),
    "describe_schema": (
        t_schema,
        "TeslaMate 数据库的表结构，以及写 SQL 前必须知道的口径与坑。写 SQL 之前先调这个。",
        _schema({}),
    ),
    "run_sql": (
        t_run_sql,
        "对 TeslaMate 库跑一条只读 SQL（只允许 SELECT/WITH，只读事务，最多 200 行）。"
        "先调 describe_schema 了解表结构和口径。",
        _schema({"sql": {"type": "string"}}, ["sql"]),
    ),
}


def _all_tools() -> dict:
    """只读工具 + （开关打开时的）写工具。"""
    from .write_tools import available

    return {**TOOLS, **available()}


def list_tools() -> list[dict]:
    return [
        {"name": name, "description": desc, "inputSchema": schema}
        for name, (_, desc, schema) in _all_tools().items()
    ]


async def call_tool(name: str, args: dict) -> str:
    entry = _all_tools().get(name)
    if entry is None:
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
    handler = entry[0]
    try:
        result = await handler(**(args or {}))
    except db.DbUnavailable as e:
        result = {"error": f"TeslaMate 数据库暂不可用：{e}"}
    except TypeError as e:
        result = {"error": f"参数不对：{e}"}
    except Exception as e:
        log.exception("mcp tool %s failed", name)
        result = {"error": str(e)[:300]}
    return json.dumps(result, ensure_ascii=False, default=str)
