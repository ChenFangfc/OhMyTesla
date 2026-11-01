"""TeslaMate Postgres 只读连接池。

TeslaMate 首次启动时才建表，因此这里对连接失败保持容错：
拿不到池子时抛出 DbUnavailable，由 API 层转成友好提示。

三类故障必须分开报，因为用户该做的事完全不同：

- **连不上**（DbUnavailable）：数据库没起来 / 网络断了。连接池会被丢掉重建。
- **查询超时**（DbTimeout，是 DbUnavailable 的子类，老调用方照样能兜住）：
  数据库好好的，只是这一条查询太慢。**不能**因此拆掉连接池 —— 以前
  `asyncio.TimeoutError` 在 3.11+ 就是 `TimeoutError`，而它是 `OSError` 的子类，
  被当成"连接丢了"，一条慢查询会把所有人的连接一起关掉。
- **SQL 本身出错**（asyncpg.PostgresError 原样抛出）：参数不对、表不存在、
  TeslaMate 改了 schema。由 insights/guard.py 翻译成准确的状态码与说明，
  而不是一律"数据库尚未就绪"。

另外这里做一件"大声失败"的事：TeslaMate 4.3 起明确说「数据库不是接口」，
schema 改名不会提前通知。所以第一次连上时核对 schema_migrations 的最大版本
与本 hub 用到的列；比验证过的版本新、或者缺列，就记 ERROR 日志并在
/api/health/teslamate 上挂出标志（见 teslamate_schema()）。
"""

import asyncio
import logging
import time

import asyncpg

from .config import settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_lock = asyncio.Lock()
# 建池失败后的冷却截止时刻。用单调钟而不是 time.time()：容器里 NTP 校时
# 会让墙钟跳变，冷却要么立刻失效要么卡住很久。
_fail_until: float = 0.0
_RETRY_COOLDOWN_S = 10.0
# 单条查询的默认上限（秒）。调用方可以按查询传更小的 timeout。
COMMAND_TIMEOUT_S = 30.0
# 持有正在后台关闭的池的任务引用。不留引用的话 asyncio 只保存弱引用，
# 任务可能在 await 中途被回收，连接就漏了。
_CLOSING: set[asyncio.Task] = set()
_BG: set[asyncio.Task] = set()


class DbUnavailable(Exception):
    """数据库连不上（或暂时不可用）。"""


class DbTimeout(DbUnavailable):
    """查询超时。数据库本身是好的，连接池保留。

    继承 DbUnavailable 是为了兼容：hub 里很多地方（livestate、告警、setup）
    只认 DbUnavailable 来降级，超时也应该走同一条降级路径，而不是 500。
    需要区分的地方（API 层）先判 DbTimeout。
    """


async def get_pool() -> asyncpg.Pool:
    global _pool, _fail_until
    if _pool is not None:
        return _pool
    # 刚失败过就直接拒绝，别让每个请求都去撞一次超时。TeslaMate 首次启动要
    # 跑几分钟建表迁移，这段时间里门户每一次刷新都会串行地卡在 _lock 上。
    if time.monotonic() < _fail_until:
        raise DbUnavailable("database was unreachable moments ago; backing off")
    async with _lock:
        if _pool is not None:
            return _pool
        if time.monotonic() < _fail_until:
            raise DbUnavailable("database was unreachable moments ago; backing off")
        try:
            _pool = await asyncpg.create_pool(
                host=settings.tm_db_host,
                port=settings.tm_db_port,
                user=settings.tm_db_user,
                password=settings.tm_db_pass,
                database=settings.tm_db_name,
                # min_size=1 会在建池时就要求一条可用连接，数据库没起来时
                # 这一步要一直等到 TCP 超时（可达数十秒），而 _lock 是全局的
                # —— 所有请求连同 MQTT 监听里的库调用一起排在后面。
                # 0 表示"池子先建起来，用的时候再连"。
                min_size=0,
                max_size=5,
                command_timeout=COMMAND_TIMEOUT_S,
                timeout=5,              # 单次建连的上限
            )
        except Exception as e:
            _fail_until = time.monotonic() + _RETRY_COOLDOWN_S
            log.warning("TeslaMate database not reachable yet: %s", e)
            raise DbUnavailable(str(e)) from e
        _fail_until = 0.0
        _spawn(_probe_schema_quietly())
        return _pool


def _spawn(coro) -> None:
    try:
        t = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _BG.add(t)
    t.add_done_callback(_BG.discard)


# 连接层面的故障（数据库重启/网络断开），区别于 SQL 本身出错。
# 注意 TimeoutError 也是 OSError —— 查询阶段的超时在 _run 里先单独接住，
# 不会落到这里。
_CONN_ERRORS = (
    asyncpg.InterfaceError,          # 连接已关闭等客户端侧错误
    asyncpg.PostgresConnectionError,  # 含 ConnectionDoesNotExistError
    asyncpg.CannotConnectNowError,   # 服务端正在启动/恢复
    asyncpg.AdminShutdownError,
    asyncpg.CrashShutdownError,
    ConnectionError,
    OSError,
)


def _discard(pool: asyncpg.Pool, err: Exception, what: str) -> None:
    """让下次调用重建连接池，数据库恢复后自动自愈。

    必须比对身份：只有当全局池仍是**我这次用的这个**时才置空。
    否则并发下会出现——A 失败置空、B 建好新池、慢一步的 C 拿着旧池
    失败后把 B 刚建好的池关掉，之后所有查询都 "pool is closing"。
    """
    global _pool
    if _pool is pool:
        _pool = None
        _CLOSING.add(asyncio.create_task(_close_quietly(pool)))
    log.warning("%s: %s", what, err or type(err).__name__)


def _short(query: str) -> str:
    return " ".join(query.split())[:120]


async def _run(method: str, query: str, *args, timeout_s: float | None = None):
    """统一入口：连接故障 → DbUnavailable（拆池重建）；超时 → DbTimeout
    （保留连接池）；SQL 错误原样抛出，交给 API 层按种类翻译。"""
    pool = await get_pool()
    try:
        conn = await pool.acquire()
    except (TimeoutError, *_CONN_ERRORS) as e:
        # 取连接阶段的超时 = 建连超时 = 数据库确实连不上，跟连接故障同一类
        _discard(pool, e, "could not get a database connection")
        raise DbUnavailable(str(e) or type(e).__name__) from e
    try:
        return await getattr(conn, method)(query, *args, timeout=timeout_s)
    except TimeoutError as e:
        limit = timeout_s or COMMAND_TIMEOUT_S
        log.warning("TeslaMate query timed out after %.0fs: %s", limit, _short(query))
        raise DbTimeout(f"query timed out after {limit:.0f}s") from e
    except asyncpg.QueryCanceledError as e:
        # statement_timeout（例如 MCP run_sql 设的）或被管理员取消
        raise DbTimeout(str(e)) from e
    except _CONN_ERRORS as e:
        _discard(pool, e, "database connection lost")
        raise DbUnavailable(str(e) or type(e).__name__) from e
    finally:
        try:
            await pool.release(conn, timeout=5)
        except Exception:  # 归还失败不该盖掉真正的异常
            log.debug("connection release failed", exc_info=True)


async def _close_quietly(pool: asyncpg.Pool) -> None:
    try:
        await pool.close()
    except Exception:
        log.debug("suppressed", exc_info=True)
    finally:
        _CLOSING.discard(asyncio.current_task())


async def fetch(query: str, *args, timeout_s: float | None = None):
    return await _run("fetch", query, *args, timeout_s=timeout_s)


async def fetchrow(query: str, *args, timeout_s: float | None = None):
    return await _run("fetchrow", query, *args, timeout_s=timeout_s)


async def fetchval(query: str, *args, timeout_s: float | None = None):
    return await _run("fetchval", query, *args, timeout_s=timeout_s)


async def execute(query: str, *args, timeout_s: float | None = None):
    """写操作。仅用于电价回写 charging_processes.cost（TeslaMateAgile 同款做法）。"""
    return await _run("execute", query, *args, timeout_s=timeout_s)


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------- schema 守卫

# 本 hub 的 SQL 逐条核对过的 TeslaMate schema 版本（v4.4.0 的最后一条迁移，
# 20261014120000_rename_range_enum_to_preferred_range）。比它新的版本没人看过。
VERIFIED_TM_SCHEMA = 20261014120000
VERIFIED_TM_RELEASE = "4.4.0"

# 分析/兼容层实际读到的列。缺任何一列都意味着 TeslaMate 改了 schema，
# 对应的接口会报"schema 已变化"而不是给出一个悄悄算错的数。
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "cars": ("id", "name", "model", "efficiency", "vin", "trim_badging", "marketing_name",
             "settings_id", "exterior_color", "wheel_type", "spoiler_type", "eid", "vid",
             "inserted_at", "updated_at"),
    "car_settings": ("id", "enabled", "free_supercharging", "lfp_battery", "suspend_min",
                     "suspend_after_idle_min", "req_not_unlocked", "use_streaming_api"),
    "drives": ("id", "car_id", "start_date", "end_date", "distance", "duration_min",
               "start_km", "end_km", "start_rated_range_km", "end_rated_range_km",
               "start_ideal_range_km", "end_ideal_range_km", "speed_max", "power_max",
               "power_min", "outside_temp_avg", "inside_temp_avg", "start_address_id",
               "end_address_id", "start_geofence_id", "end_geofence_id",
               "start_position_id", "end_position_id", "ascent", "descent"),
    "positions": ("id", "car_id", "drive_id", "date", "latitude", "longitude", "speed",
                  "power", "odometer", "battery_level", "usable_battery_level",
                  "ideal_battery_range_km", "rated_battery_range_km", "est_battery_range_km",
                  "outside_temp", "inside_temp", "elevation", "tpms_pressure_fl",
                  "tpms_pressure_fr", "tpms_pressure_rl", "tpms_pressure_rr"),
    "charging_processes": ("id", "car_id", "start_date", "end_date", "charge_energy_added",
                           "charge_energy_used", "start_battery_level", "end_battery_level",
                           "start_rated_range_km", "end_rated_range_km",
                           "start_ideal_range_km", "end_ideal_range_km", "duration_min",
                           "outside_temp_avg", "position_id", "address_id", "geofence_id",
                           "cost"),
    "charges": ("charging_process_id", "date", "battery_level", "usable_battery_level",
                "charge_energy_added", "charger_power", "charger_voltage",
                "charger_actual_current", "charger_phases", "charger_pilot_current",
                "fast_charger_present", "fast_charger_brand", "fast_charger_type",
                "rated_battery_range_km", "ideal_battery_range_km", "outside_temp",
                "battery_heater", "battery_heater_on"),
    "states": ("car_id", "state", "start_date", "end_date"),
    "updates": ("id", "car_id", "start_date", "end_date", "version"),
    "addresses": ("id", "display_name", "name", "road", "house_number", "neighbourhood",
                  "city", "county", "state", "state_district", "country", "latitude",
                  "longitude", "raw"),
    "geofences": ("id", "name", "latitude", "longitude", "radius"),
    "settings": ("unit_of_length", "unit_of_temperature", "unit_of_pressure",
                 "preferred_range", "language", "base_url", "grafana_url"),
}

_schema: dict = {"status": "unknown", "checked_at": None}
_schema_checked_mono: float = 0.0
_schema_lock = asyncio.Lock()
_schema_logged: str = ""


def teslamate_schema() -> dict:
    """最近一次 schema 核对的结果（同步、不查库）。"""
    return dict(_schema)


async def _probe_schema_quietly() -> None:
    try:
        await check_teslamate_schema(force=True)
    except Exception:  # 守卫本身出错不能拖垮建池
        log.debug("schema probe failed", exc_info=True)


async def check_teslamate_schema(force: bool = False) -> dict:
    """核对 TeslaMate 的 schema 版本与所需列。结果缓存（正常 10 分钟、异常 1 分钟）。

    status：
      ok         版本 == 已验证版本，所需列齐全
      older      比已验证的旧（老版本 TeslaMate；列齐全就照常工作）
      newer      比已验证的新 —— 没人核对过，ERROR 日志 + 健康标志
      incompatible  缺列（不论版本）—— 相关接口会失败并说明原因
      not_ready  schema_migrations 还不存在（TeslaMate 首次启动建表中）
      unreachable 连不上数据库
    """
    global _schema, _schema_checked_mono, _schema_logged
    ttl = 600.0 if _schema.get("status") in ("ok", "older") else 60.0
    if not force and _schema.get("checked_at") and time.monotonic() - _schema_checked_mono < ttl:
        return dict(_schema)
    async with _schema_lock:
        out: dict = {"verified_version": VERIFIED_TM_SCHEMA,
                     "verified_release": VERIFIED_TM_RELEASE,
                     "version": None, "migrations": None, "missing_columns": [],
                     "checked_at": time.time()}
        try:
            row = await fetchrow(
                "SELECT max(version) AS v, count(*) AS n FROM schema_migrations", timeout_s=10)
            out["version"] = int(row["v"]) if row and row["v"] is not None else None
            out["migrations"] = int(row["n"]) if row else 0
            cols = await fetch(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = ANY($1::text[])",
                list(REQUIRED_COLUMNS), timeout_s=10,
            )
            have: dict[str, set[str]] = {}
            for r in cols:
                have.setdefault(r["table_name"], set()).add(r["column_name"])
            missing = [f"{t}.{c}" for t, want in REQUIRED_COLUMNS.items()
                       for c in want if c not in have.get(t, set())]
            out["missing_columns"] = missing
            v = out["version"]
            if missing:
                out["status"] = "incompatible"
            elif v is None:
                out["status"] = "not_ready"
            elif v > VERIFIED_TM_SCHEMA:
                out["status"] = "newer"
            elif v < VERIFIED_TM_SCHEMA:
                out["status"] = "older"
            else:
                out["status"] = "ok"
        except asyncpg.UndefinedTableError:
            out["status"] = "not_ready"
        except DbUnavailable as e:
            out["status"] = "unreachable"
            out["error"] = str(e)[:200]
        except asyncpg.PostgresError as e:
            out["status"] = "error"
            out["error"] = str(e)[:200]
        _schema = out
        _schema_checked_mono = time.monotonic()

        # 同一种异常只大声报一次，别每分钟刷屏
        key = f"{out['status']}:{out.get('version')}:{len(out['missing_columns'])}"
        if key != _schema_logged:
            _schema_logged = key
            if out["status"] == "newer":
                log.error(
                    "TeslaMate schema version %s is NEWER than the last version this hub was "
                    "verified against (%s, TeslaMate %s). TeslaMate says its database is not an "
                    "interface and may change without notice: analytics may fail or be wrong "
                    "until OhmyTesla is re-verified. See /api/health/teslamate.",
                    out["version"], VERIFIED_TM_SCHEMA, VERIFIED_TM_RELEASE)
            elif out["status"] == "incompatible":
                log.error("TeslaMate schema %s is missing columns OhmyTesla reads: %s",
                          out["version"], ", ".join(out["missing_columns"][:20]))
            elif out["status"] == "older":
                log.info("TeslaMate schema %s is older than the verified %s; continuing",
                         out["version"], VERIFIED_TM_SCHEMA)
        return dict(out)
