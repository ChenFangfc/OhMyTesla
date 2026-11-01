"""把数据库层的异常翻译成**准确的** HTTP 错误。

以前所有路由都是一句"TeslaMate 数据库尚未就绪（可能仍在初始化建表）"，
不管真实原因是什么：LIMIT 传了负数是它，查询超时是它，TeslaMate 改了 schema
也是它 —— 排查方向直接跑偏。现在按种类分：

  状态码  X-Error-Code        含义
  503     db_unavailable      连不上 TeslaMate 的数据库（没起来 / 网络断了）
  504     db_timeout          连得上，但这条查询超时了（换短一点的时间段）
  503     tm_schema           表/列不存在：TeslaMate 首次启动建表中，或升级后改了 schema
  500     db_query_failed     其它 SQL 错误（本项目的 bug，带原始报错）

detail 仍然是字符串（前端现有代码按字符串显示），机器可读的分类放在
X-Error-Code 头里。
"""

import functools
import logging

import asyncpg
from fastapi import HTTPException

from .. import db

log = logging.getLogger(__name__)

_SCHEMA_ERRORS = (
    asyncpg.UndefinedTableError,
    asyncpg.UndefinedColumnError,
    asyncpg.UndefinedFunctionError,
    asyncpg.InvalidSchemaNameError,
    asyncpg.UndefinedObjectError,
)


def http_error(e: BaseException) -> HTTPException | None:
    """异常 → HTTPException；不认识的返回 None（让它照常冒泡）。"""
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, db.DbTimeout):
        return HTTPException(
            504, f"TeslaMate query timed out — try a shorter period ({e})",
            headers={"X-Error-Code": "db_timeout"})
    if isinstance(e, db.DbUnavailable):
        return HTTPException(
            503, f"TeslaMate database is not reachable right now: {e}",
            headers={"X-Error-Code": "db_unavailable"})
    if isinstance(e, _SCHEMA_ERRORS):
        s = db.teslamate_schema()
        ver = s.get("version")
        return HTTPException(
            503,
            "TeslaMate schema is not ready or has changed"
            f" (schema {ver or 'unknown'}, verified up to {db.VERIFIED_TM_SCHEMA}): {e}",
            headers={"X-Error-Code": "tm_schema"})
    if isinstance(e, asyncpg.PostgresError):
        log.error("TeslaMate query failed: %s", e)
        return HTTPException(500, f"TeslaMate query failed: {e}",
                             headers={"X-Error-Code": "db_query_failed"})
    return None


def guard(fn):
    """装饰 async 函数：数据库异常 → 准确的 HTTPException。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            err = http_error(e)
            if err is None:
                raise
            raise err from e

    return wrapper


def call(fn):
    """`await call(queries.summary)(car_id)` 这种老写法的兼容入口。"""
    return guard(fn)
