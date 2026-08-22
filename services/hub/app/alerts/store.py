"""Hub 自有的 SQLite 存储：告警历史、规则开关、合成任务。"""

import asyncio
import contextlib
import json
import logging
import os
import time

import aiosqlite

from ..config import settings

log = logging.getLogger(__name__)

# schema 不再写在这里 —— 见 app/migrations.py。
# 之前是一大段 `CREATE TABLE IF NOT EXISTS` 在 open() 里无脑重跑，
# 那招只能新增表，改列就无能为力，而且没有任何地方记得住"当前是第几版"。



class Store:
    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._schema: dict = {"version": 0, "applied": [], "drifted": []}
        # 每个 settings key 一把锁，见 mutate_setting
        self._setting_locks: dict[str, asyncio.Lock] = {}

    @property
    def schema(self) -> dict:
        """当前 schema 版本与本次启动做过的迁移，供 /healthz 暴露。"""
        return self._schema

    async def open(self) -> None:
        self._db = await aiosqlite.connect(settings.sqlite_path)
        # 这个文件里有 VAPID 私钥、Tesla token 覆盖、全部告警历史与位置线索。
        # SQLite 建库用的是 0644，容器里同 uid 的任何进程、以及宿主机上
        # 挂载目录的所有用户都读得到。-wal / -shm 里是同一批数据的最新副本，
        # 只改主库权限等于没改。
        # sqlite_path 是 Path，不是 str —— 拼后缀要先转字符串
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.chmod(f"{settings.sqlite_path}{suffix}", 0o600)
        self._db.row_factory = aiosqlite.Row
        # 全应用（告警、哨兵会话、电价、token、合成任务、偏好）共用这一条连接，
        # 每一次写都排在同一条队里。WAL 让读不再被写挡住，busy_timeout 让偶发的
        # 锁竞争等一会儿而不是直接抛 "database is locked"。
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        # 外键约束默认是关的，不显式打开的话 REFERENCES 只是注释
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.commit()

        from ..migrations import migrate

        self._schema = await migrate(self._db)
        if self._schema["applied"]:
            log.info("hub.db migrated: %s", ", ".join(self._schema["applied"]))

    async def prune(self, keep_days: int) -> dict[str, int]:
        """按保留期清掉旧记录。

        这三张表原本只增不删：一台跑上几年的机器，告警和哨兵会话会一直堆下去，
        而它们全在同一个 SQLite 文件里，写入又是串行的。保留期到了就删，
        免得体积拖慢每一次写。
        """
        if keep_days <= 0:
            return {}
        cutoff = time.time() - keep_days * 86400
        removed: dict[str, int] = {}
        for table, col in (("alerts", "ts"),
                           ("sentry_sessions", "started"),
                           ("compose_jobs", "created"),
                           ("command_log", "ts")):
            try:
                cur = await self.db.execute(
                    f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))  # noqa: S608
                if cur.rowcount and cur.rowcount > 0:
                    removed[table] = cur.rowcount
            except Exception:
                log.debug("prune skipped for %s", table, exc_info=True)
        if removed:
            await self.db.commit()
            log.info("pruned old rows: %s", removed)
        return removed

    async def prune_sei_orphans(self, existing_paths: set[str] | None = None) -> dict[str, int]:
        """清掉素材已经不在了的 SEI 索引行。

        这两张表不能按时间删：clip_sei 是"这个文件解析过了"的记账，按年龄
        删掉之后 index_event 会把同一批片段重新解一遍，白烧 CPU。它们的正确
        生命周期跟着**文件本身**走 —— 片段被删了（U 盘轮转、用户清理），
        对应的行才该走。

        existing_paths 由调用方从素材库扫出来。传 None 表示扫不到素材目录
        （U 盘没插、路径配错），这时候**什么都不删** —— 把"暂时看不见"
        当成"已经没了"会一次清空整个索引。
        """
        if existing_paths is None:
            return {}
        removed: dict[str, int] = {}
        rows = await self.db.execute_fetchall("SELECT path FROM clip_sei")
        stale = [r["path"] for r in rows if r["path"] not in existing_paths]
        if not stale:
            return {}
        # SQLite 的变量上限默认 999，分批删
        for i in range(0, len(stale), 500):
            chunk = stale[i:i + 500]
            marks = ",".join("?" * len(chunk))
            await self.db.execute(f"DELETE FROM sei_event WHERE path IN ({marks})", chunk)  # noqa: S608
            await self.db.execute(f"DELETE FROM clip_sei  WHERE path IN ({marks})", chunk)  # noqa: S608
        await self.db.commit()
        removed["clip_sei"] = len(stale)
        log.info("pruned %d SEI index row(s) whose clips are gone", len(stale))
        return removed

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "store not opened"
        return self._db

    # ---------- 告警历史 ----------

    async def clear_alerts(self) -> int:
        """清空告警历史。返回删掉的条数。

        历史里可能留着旧版本产生的记录（语言不对、或已修复的误报），
        用户应该能自己清掉，而不是等它慢慢滚出去。
        """
        cur = await self.db.execute("SELECT count(*) FROM alerts")
        n = (await cur.fetchone())[0]
        await self.db.execute("DELETE FROM alerts")
        await self.db.commit()
        return int(n)

    async def add_alert(
        self, car_id: str, car_name: str, rule: str, level: str,
        title: str, body: str, delivered: bool, channels: list[str],
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO alerts (ts, car_id, car_name, rule, level, title, body, delivered, channels)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), car_id, car_name, rule, level, title, body,
             1 if delivered else 0, json.dumps(channels, ensure_ascii=False)),
        )
        await self.db.commit()
        return cur.lastrowid

    # ---------- 指令流水 ----------

    async def log_command(self, car_id: str, command: str, params: dict | None,
                          source: str, ok: bool, error: str, ms: int) -> None:
        try:
            await self.db.execute(
                "INSERT INTO command_log (ts, car_id, command, params, source, ok, error, ms)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), str(car_id), command,
                 json.dumps(params, ensure_ascii=False) if params else None,
                 source, 1 if ok else 0, error[:300] if error else None, ms),
            )
            await self.db.commit()
        except Exception:
            log.exception("failed to log command")

    async def recent_commands(self, limit: int = 100) -> list[dict]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM command_log ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    async def recent_alerts(self, limit: int = 100) -> list[dict]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    # ---------- 设置 ----------

    async def get_setting(self, key: str, default=None):
        row = await self.db.execute_fetchall(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        rows = list(row)
        if not rows:
            return default
        return json.loads(rows[0]["value"])

    async def mutate_setting(self, key: str, fn, default=None):
        """读-改-写一个 JSON 设置项，全程持锁。

        settings 表里存的是整块 JSON（行程标签、电价方案、推送订阅、告警规则
        开关……），改一条要先读出整块、改完再整块写回。两个请求同时进来时，
        后写的那个是基于**改动前**的快照算出来的，前一个的修改就这么没了。
        实测路径很普通：在行程页快速给两段行程打标签，第二次点击会把第一次
        的标签抹掉。每个 key 一把锁，互不相干的设置项不会互相排队。
        """
        lock = self._setting_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cur = await self.get_setting(key, default)
            new = fn(cur)
            await self.set_setting(key, new)
            return new

    async def set_setting(self, key: str, value) -> None:
        await self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await self.db.commit()

    # ---------- 合成任务 ----------

    async def create_job(self, event_id: str, params: dict) -> int:
        cur = await self.db.execute(
            "INSERT INTO compose_jobs (created, event_id, params) VALUES (?,?,?)",
            (time.time(), event_id, json.dumps(params, ensure_ascii=False)),
        )
        await self.db.commit()
        return cur.lastrowid

    # 能被 update_job 改的列。列名要拼进 SQL，所以必须白名单 ——
    # `**fields` 的键看着像 Python 标识符，但 `update_job(1, **{"x=1;--": 2})`
    # 是完全合法的调用，那串东西会原样进 SET 子句。现在的调用方都规矩，
    # 靠的是运气而不是机制。
    _JOB_COLUMNS = frozenset({"status", "progress", "output", "error"})

    async def update_job(self, job_id: int, **fields) -> None:
        bad = set(fields) - self._JOB_COLUMNS
        if bad:
            raise ValueError(f"update_job: unknown column(s) {sorted(bad)}")
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.db.execute(
            f"UPDATE compose_jobs SET {cols} WHERE id = ?",  # noqa: S608 - 列名已过白名单
            (*fields.values(), job_id),
        )
        await self.db.commit()

    async def get_job(self, job_id: int) -> dict | None:
        rows = list(await self.db.execute_fetchall(
            "SELECT * FROM compose_jobs WHERE id = ?", (job_id,)
        ))
        return dict(rows[0]) if rows else None

    async def list_jobs(self, limit: int = 50) -> list[dict]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM compose_jobs ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]


store = Store()
