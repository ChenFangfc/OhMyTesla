"""Hub 自有的 SQLite 存储：告警历史、规则开关、合成任务。"""

import json
import logging
import time

import aiosqlite

from ..config import settings

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    car_id TEXT NOT NULL,
    car_name TEXT,
    rule TEXT NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    delivered INTEGER DEFAULT 0,
    channels TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts (ts DESC);

-- 车辆指令流水。这个页面能解锁你的车，"谁在什么时候下了什么、成没成"
-- 必须留痕：出了事要能回溯，平时也要能看出是不是有人在乱点。
CREATE TABLE IF NOT EXISTS command_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    car_id TEXT,
    command TEXT NOT NULL,
    params TEXT,
    source TEXT,
    ok INTEGER DEFAULT 0,
    error TEXT,
    ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cmdlog_ts ON command_log (ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sentry_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id TEXT NOT NULL,
    car_name TEXT,
    source TEXT NOT NULL,
    started REAL NOT NULL,
    last_seen REAL NOT NULL,
    samples INTEGER DEFAULT 1,
    peak_state TEXT,
    escalated INTEGER DEFAULT 0,
    geofence TEXT,
    soc TEXT,
    extras TEXT,
    closed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sentry_started ON sentry_sessions (started DESC);

CREATE TABLE IF NOT EXISTS compose_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    event_id TEXT NOT NULL,
    params TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL DEFAULT 0,
    output TEXT,
    error TEXT
);
"""


class Store:
    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(settings.sqlite_path)
        self._db.row_factory = aiosqlite.Row
        # 全应用（告警、哨兵会话、电价、token、合成任务、偏好）共用这一条连接，
        # 每一次写都排在同一条队里。WAL 让读不再被写挡住，busy_timeout 让偶发的
        # 锁竞争等一会儿而不是直接抛 "database is locked"。
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

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
            except Exception:  # noqa: BLE001 - 某张表还没建就跳过
                log.debug("prune skipped for %s", table, exc_info=True)
        if removed:
            await self.db.commit()
            log.info("pruned old rows: %s", removed)
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
        except Exception:  # noqa: BLE001 - 记流水失败绝不能让指令本身失败
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

    async def update_job(self, job_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.db.execute(
            f"UPDATE compose_jobs SET {cols} WHERE id = ?",
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
