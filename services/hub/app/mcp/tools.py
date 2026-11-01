"""MCP 工具集：只读地回答关于这辆车的问题。

设计取舍：**给成型的分析工具，而不是只给一个 run_sql。** 让模型自己写
TeslaMate 的 SQL，它得先知道 `positions.date` 是 naive UTC、
`cars.efficiency` 的单位是 kWh/km、rated_range 差值才是能耗口径 ——
这些坑本项目已经踩过一遍并写在各自的查询里了。把那些查询包成工具，
答案才稳定。`run_sql` 作为兜底保留，但走的是硬只读通道。
"""

import contextlib
import hashlib
import json
import logging
import re
import secrets

from .. import db
from ..alerts.store import store
from ..config import settings
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
    # 换身份/执行字符串里的 SQL：受限登录下本就无效，过滤器再挡一层。
    "set_config", "query_to_xml", "query_to_xml_and_xmlschema",
    "dblink", "pg_stat_file",
)


async def _car(car_id: int | None) -> tuple[int, dict]:
    """解析要回答的是哪辆车，并带上给人看的车名。

    没指定车时取"最近活跃、且没归档"的那辆（insights/cars.py）。以前取
    live.cars 里的第一个 —— 本机三车账号里那可能是一辆早已离开账号、只剩数据库
    残影的车；写工具沿用同一个默认值时，确认过的"开锁"会发给错的车。
    每个工具的结果都带 car 字段，模型据此在回答里说清楚是哪辆车。
    """
    from ..insights import cars as registry

    cid = car_id or await registry.default_car_id()
    if cid is None:
        raise ValueError("TeslaMate 里还没有车辆")
    meta = await registry.get_car(int(cid))
    if meta is None:
        raise ValueError(f"没有 id 为 {cid} 的车")
    return int(cid), {"id": meta["id"], "label": meta["label"], "model": meta["model_line"],
                      "archived": meta["archived"], "last_seen": meta["last_seen"],
                      "defaulted": car_id is None}


def _with_car(car: dict, payload) -> dict:
    out = {"car": car}
    if isinstance(payload, dict):
        out.update(payload)
    else:
        out["data"] = payload
    if car.get("archived"):
        out["note_archived"] = ("这辆车已归档（不在 Tesla 账号里或很久没有数据），"
                                "以下是历史数据，不是当前状态。")
    return out


async def t_list_cars(**_) -> dict:
    """全部车辆、哪辆是默认车、哪些已归档。"""
    from ..insights import cars as registry

    cars = await registry.list_cars()
    return {"default_car_id": await registry.default_car_id(),
            "cars": [{k: c.get(k) for k in ("id", "label", "name", "model_line", "vin", "archived",
                                             "last_seen", "days_since_seen", "mqtt_live")}
                     for c in cars]}


async def t_vehicle_state(car_id: int | None = None, **_) -> dict:
    """当前实时状态（MQTT 快照）。"""
    snap = live.snapshot()
    from ..insights import cars as registry

    meta = {c["id"]: c for c in await registry.list_cars()} if snap else {}
    cars = {}
    for k, v in snap.items():
        if car_id is not None and k != str(car_id):
            continue
        m = meta.get(int(k)) if k.isdigit() else None
        cars[k] = {"label": m["label"] if m else k, "archived": bool(m and m["archived"]), **v}
    return {"cars": cars,
            "default_car_id": await registry.default_car_id(),
            "note": "值全部是字符串；_seed_age 表示这是数据库补的历史值而非实时值；"
                    "archived=true 的车已不在账号里，它的值是最后一次见到时的残影"}


async def t_list_drives(car_id: int | None = None, limit: int = 20, **_) -> dict:
    cid, car = await _car(car_id)
    return _with_car(car, {"drives": await queries.drives_list(cid, min(max(limit, 1), 200))})


async def t_drive_detail(drive_id: int, **_) -> dict:
    """单次行程：统计量 + 能耗 + 速度分布（不含逐点序列，给模型没意义）。"""
    from ..insights import analytics

    d = await analytics.drive_detail(drive_id, max_points=50)
    series = d.pop("series", {})
    d.pop("route", None)
    d["sample"] = [{"t": t, "speed_kmh": v, "power_kw": p, "soc": s}
                   for t, v, p, s in list(zip(series.get("t", []), series.get("speed_kmh", []),
                                              series.get("power_kw", []), series.get("soc", []),
                                              strict=False))[::3]]
    return d


async def t_charging(car_id: int | None = None, days: int = 0, **_) -> dict:
    cid, car = await _car(car_id)
    return _with_car(car, await queries.charging(cid, days))


async def t_charge_curve(charging_process_id: int, **_) -> dict:
    """单次充电：墙端/电池端电量、效率、交直流、相数、费用、按 SOC 的功率曲线。"""
    from ..insights import analytics

    d = await analytics.charge_detail(charging_process_id, max_points=60)
    series = d.pop("series", {})
    d["sample"] = [{"min": m, "soc": s, "kw": k}
                   for m, s, k in zip(series.get("minutes", []), series.get("soc", []),
                                      series.get("kw", []), strict=False)][::2]
    return d


async def t_driving_stats(car_id: int | None = None, days: int = 0, **_) -> dict:
    cid, car = await _car(car_id)
    return _with_car(car, await queries.drives(cid, days))


async def t_battery_health(car_id: int | None = None, **_) -> dict:
    """电池健康，TeslaMate Battery Health 面板的算法（容量/衰减/100% 续航趋势）。"""
    from ..insights import analytics

    cid, _car_meta = await _car(car_id)
    b = await analytics.battery(cid)
    b["capacity_by_mileage"] = b.get("capacity_by_mileage", [])[-12:]
    return b


async def t_vampire_drain(car_id: int | None = None, days: int = 90, **_) -> dict:
    cid, car = await _car(car_id)
    v = await queries.vampire_drain(cid, days)
    v.pop("all", None)
    return _with_car(car, v)


async def t_summary(car_id: int | None = None, **_) -> dict:
    cid, car = await _car(car_id)
    return _with_car(car, await queries.summary(cid))


async def t_records(car_id: int | None = None, **_) -> dict:
    cid, car = await _car(car_id)
    return _with_car(car, await queries.records(cid))


async def t_tpms(car_id: int | None = None, **_) -> dict:
    cid, car = await _car(car_id)
    return _with_car(car, await queries.tpms(cid))


def _period(days: int | None = None, from_date: str | None = None, to_date: str | None = None,
            default_days: int | None = None):
    from ..insights.period import parse_period

    return parse_period(days, from_date, to_date, default_days=default_days)


async def t_statistics(car_id: int | None = None, period: str = "month", days: int | None = None,
                       from_date: str | None = None, to_date: str | None = None, **_) -> dict:
    """按日/周/月/年/总计的里程、能耗（净/毛）、充电量、费用 —— TeslaMate Statistics 面板。"""
    from ..insights import analytics

    cid, _c = await _car(car_id)
    r = await analytics.statistics(cid, _period(days, from_date, to_date), period)
    r["rows"] = r["rows"][:60]
    return r


async def t_efficiency(car_id: int | None = None, days: int | None = None,
                       from_date: str | None = None, to_date: str | None = None, **_) -> dict:
    """能耗：净/毛、按气温、按速度分档 —— TeslaMate Efficiency 面板 + 速度分档。"""
    from ..insights import analytics

    cid, _c = await _car(car_id)
    return await analytics.efficiency(cid, _period(days, from_date, to_date))


async def t_states(car_id: int | None = None, days: int = 7, **_) -> dict:
    """在线/睡眠/离线/行驶/充电的时间占比（不含逐段明细）。"""
    from ..insights import analytics

    cid, _c = await _car(car_id)
    r = await analytics.states_timeline(cid, _period(days, default_days=7))
    r["segments"] = r["segments"][-40:]
    return r


async def t_mileage(car_id: int | None = None, bucket: str = "month", days: int | None = None,
                    **_) -> dict:
    """里程表增长（含 TeslaMate 没记上的里程）。"""
    from ..insights import analytics

    cid, _c = await _car(car_id)
    return await analytics.mileage(cid, _period(days), bucket)


async def t_locations(car_id: int | None = None, days: int | None = None, **_) -> dict:
    """常去地点（围栏优先）、去过的城市与国家。"""
    from ..insights import analytics

    cid, _c = await _car(car_id)
    r = await analytics.locations(cid, _period(days), 15)
    r["cities"] = r["cities"][:30]
    return r


async def t_updates(car_id: int | None = None, **_) -> dict:
    """软件更新史：每个版本装了多久、用了几天、期间的 100% 续航。"""
    from ..insights import analytics

    cid, _c = await _car(car_id)
    return await analytics.updates(cid)


async def t_charging_stats(car_id: int | None = None, days: int | None = None, **_) -> dict:
    """充电总览：交直流占比、每 kWh/每 100 km 成本、常用充电站、学到的定时充电时刻。"""
    from ..insights import analytics

    cid, _c = await _car(car_id)
    return await analytics.charging_stats(cid, _period(days))


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
# 查询必须**以这个角色登录**来跑，不能在超级用户的连接上 SET ROLE：
# Postgres 按登录用户判断能不能切角色，一条
# `SELECT set_config('role','none',true) …` 就能在同一条查询里切回超级用户
# （审查时实测读到了 private.tokens 并调用了 pg_read_file）。所以 run_sql 有
# 自己的连接池，用这个角色和一个每次启动随机生成的密码登录；登录用户本身
# 不是超级用户，就没有可以切回去的身份。
_RO_ROLE = "omt_mcp_readonly"
_role_ready = False
_ro_pool = None
_ro_password = ""


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
        # A login of its own, with nothing it could escalate to. The password
        # is derived from ENCRYPTION_KEY, which every hub on this TeslaMate
        # shares, so two hubs (a restart overlapping the old container, a dev
        # copy) don't keep resetting each other's; random when there's no key.
        # Hex, so it needs no quoting in the utility statement.
        global _ro_password
        _ro_password = (hashlib.sha256(f"{_RO_ROLE}:{settings.encryption_key}".encode()).hexdigest()[:48]
                        if settings.encryption_key else secrets.token_hex(24))
        await conn.execute(
            f'ALTER ROLE "{_RO_ROLE}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
            f"NOBYPASSRLS CONNECTION LIMIT 4 PASSWORD '{_ro_password}'")
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
            except Exception:
                log.debug("suppressed", exc_info=True)
        _role_ready = True
        return True
    except Exception:
        log.exception("could not prepare the restricted SQL role")
        return False


async def _readonly_pool():
    """Pool that logs in as the restricted role. None = refuse (fail closed)."""
    global _ro_pool, _role_ready
    if _ro_pool is not None:
        return _ro_pool
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            if not await _ensure_role(conn):
                return None
        import asyncpg

        _ro_pool = await asyncpg.create_pool(
            host=settings.tm_db_host, port=settings.tm_db_port, database=settings.tm_db_name,
            user=_RO_ROLE, password=_ro_password, min_size=0, max_size=2, timeout=5,
            command_timeout=SQL_TIMEOUT_MS / 1000 + 2,
            server_settings={"default_transaction_read_only": "on"},
        )
        async with _ro_pool.acquire() as conn:
            who = await conn.fetchrow("SELECT current_user AS u, session_user AS s, "
                                      "(SELECT rolsuper FROM pg_roles WHERE rolname = session_user) AS su")
        if who["s"] != _RO_ROLE or who["su"]:
            log.error("restricted SQL login came back as %s (superuser=%s); refusing", who["s"], who["su"])
            await _ro_pool.close()
            _ro_pool = None
            return None
        return _ro_pool
    except Exception:
        log.exception("could not open the restricted SQL login")
        _ro_pool = None
        _role_ready = False     # re-run the role setup (and a new password) next time
        return None


async def t_run_sql(sql: str, **_) -> dict:
    """兜底的只读查询。四层护栏，任何一层不过就拒。"""
    global _ro_pool, _role_ready
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

    ro = await _readonly_pool()
    if ro is None:
        return {"error": "受限角色未就绪，拒绝执行任意 SQL"}
    try:
        conn = await ro.acquire()
    except Exception:  # noqa: BLE001 — e.g. the role's password changed under us: set up again once
        with contextlib.suppress(Exception):
            await ro.close()
        _ro_pool, _role_ready = None, False
        ro = await _readonly_pool()
        if ro is None:
            return {"error": "受限角色未就绪，拒绝执行任意 SQL"}
        conn = await ro.acquire()
    try:
        # 只读事务 + 受限登录 + 超时；跑完一律回滚
        tr = conn.transaction(readonly=True)
        await tr.start()
        try:
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
            await tr.rollback()
    finally:
        await ro.release(conn)


# name -> (handler, description, inputSchema)
def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


_CAR = {"car_id": {"type": "integer",
                    "description": "TeslaMate 的车辆 id。省略 = 最近活跃且未归档的那辆（结果的 car.label 写明是哪辆）"}}
_DAYS = {"days": {"type": "integer", "description": "只看最近 N 天，0 或省略 = 全部"}}
_RANGE = {"from_date": {"type": "string", "description": "起始本地日期 YYYY-MM-DD（可选）"},
          "to_date": {"type": "string", "description": "结束本地日期 YYYY-MM-DD，含当天（可选）"}}

TOOLS: dict[str, tuple] = {
    "list_cars": (
        t_list_cars,
        "账号里的全部车辆：哪辆是默认车、哪些已归档（不在 Tesla 账号里 / 很久没数据）。"
        "多车时先调这个，回答里说清楚是哪辆车。",
        _schema({}),
    ),
    "get_vehicle_state": (
        t_vehicle_state,
        "车辆当前实时状态：电量、位置、门窗锁、充电、哨兵、胎压等。",
        _schema(_CAR),
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
        "单次充电详情：墙端/电池端电量与效率、交直流与相数、费用、按 SOC 的功率曲线。",
        _schema({"charging_process_id": {"type": "integer"}}, ["charging_process_id"]),
    ),
    "get_charging_overview": (
        t_charging_stats,
        "充电总览：交直流电量与成本、每 kWh / 每 100 km 成本、常用充电站、直流曲线、"
        "学到的惯常开始/停止充电时刻（谷电窗口）。",
        _schema({**_CAR, **_DAYS}),
    ),
    "get_statistics": (
        t_statistics,
        "按 day/week/month/year/total 汇总：里程、净/毛能耗、充电量（电池端/墙端）、费用、"
        "每 100 km 成本。period 取 day|week|month|year|total。",
        _schema({**_CAR, "period": {"type": "string"}, **_DAYS, **_RANGE}),
    ),
    "get_efficiency": (
        t_efficiency,
        "能耗分析：净能耗 vs 毛能耗（含停放损耗）、按车外温度分档、按速度分档。",
        _schema({**_CAR, **_DAYS, **_RANGE}),
    ),
    "get_states": (
        t_states,
        "最近 N 天行驶/充电/在线/睡眠/离线的时间占比与停车比例。",
        _schema({**_CAR, **_DAYS}),
    ),
    "get_mileage": (
        t_mileage,
        "里程表增长，按 day/week/month/year 分桶，并标出 TeslaMate 没记上的里程。",
        _schema({**_CAR, "bucket": {"type": "string"}, **_DAYS}),
    ),
    "get_locations": (
        t_locations,
        "常去地点（地理围栏优先）、停留时长、去过的城市与国家。",
        _schema({**_CAR, **_DAYS}),
    ),
    "get_software_updates": (
        t_updates,
        "固件更新史：每个版本的安装耗时、使用天数、期间平均 100% 续航。",
        _schema(_CAR),
    ),
    "get_driving_stats": (
        t_driving_stats,
        "行驶统计：按月里程与能耗、能耗随气温的变化曲线。",
        _schema({**_CAR, **_DAYS}),
    ),
    "get_battery_health": (
        t_battery_health,
        "电池健康（TeslaMate Battery Health 算法）：现在/新车容量、衰减%、100% 续航、"
        "循环次数、充电效率、按里程的容量变化。",
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
