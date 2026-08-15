"""Hub 自有的 SQLite 存储：告警历史、规则开关、合成任务。"""

import json
import time

import aiosqlite

from ..config import settings

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
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

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
