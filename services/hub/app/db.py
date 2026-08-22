"""TeslaMate Postgres 只读连接池。

TeslaMate 首次启动时才建表，因此这里对连接失败保持容错：
拿不到池子时抛出 DbUnavailable，由 API 层转成友好提示。
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
# 持有正在后台关闭的池的任务引用。不留引用的话 asyncio 只保存弱引用，
# 任务可能在 await 中途被回收，连接就漏了。
_CLOSING: set[asyncio.Task] = set()


class DbUnavailable(Exception):
    pass


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
                command_timeout=30,
                timeout=5,              # 单次取连接的上限
            )
        except Exception as e:
            _fail_until = time.monotonic() + _RETRY_COOLDOWN_S
            log.warning("TeslaMate database not reachable yet: %s", e)
            raise DbUnavailable(str(e)) from e
        _fail_until = 0.0
        return _pool


# 连接层面的故障（数据库重启/网络断开），区别于 SQL 本身出错
_CONN_ERRORS = (
    asyncpg.InterfaceError,          # 含 ConnectionDoesNotExistError 等
    asyncpg.PostgresConnectionError,
    ConnectionError,
    OSError,
)


async def _run(method: str, query: str, *args):
    """统一入口：把"连不上数据库"归一成 DbUnavailable，交给上层出友好提示。

    连接池建立后数据库才挂掉时，asyncpg 抛的是 InterfaceError 一系，
    不走 get_pool 的那条路——不归一的话用户看到的是 500 Internal Server Error。
    """
    global _pool
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            return await getattr(conn, method)(query, *args)
    except _CONN_ERRORS as e:
        # 让下次调用重建连接池，数据库恢复后自动自愈。
        # 必须比对身份：只有当全局池仍是**我这次用的这个**时才置空。
        # 否则并发下会出现——A 失败置空、B 建好新池、慢一步的 C 拿着旧池
        # 失败后把 B 刚建好的池关掉，之后所有查询都 "pool is closing"。
        if _pool is pool:
            _pool = None
            _CLOSING.add(asyncio.create_task(_close_quietly(pool)))
        log.warning("database connection lost: %s", e)
        raise DbUnavailable(str(e)) from e


async def _close_quietly(pool: asyncpg.Pool) -> None:
    try:
        await pool.close()
    except Exception:
        log.debug("suppressed", exc_info=True)
    finally:
        _CLOSING.discard(asyncio.current_task())


async def fetch(query: str, *args):
    return await _run("fetch", query, *args)


async def fetchrow(query: str, *args):
    return await _run("fetchrow", query, *args)


async def fetchval(query: str, *args):
    return await _run("fetchval", query, *args)


async def execute(query: str, *args):
    """写操作。仅用于电价回写 charging_processes.cost（TeslaMateAgile 同款做法）。"""
    return await _run("execute", query, *args)


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
