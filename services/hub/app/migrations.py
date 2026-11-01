"""hub.db 的 schema 版本管理。

为什么需要它：在这之前，建表全靠 `CREATE TABLE IF NOT EXISTS` 一把梭。那招只
对"新增表"有效，一旦要改列（加列、改类型、加约束、拆表）就完全没有办法——
既不知道当前库处在哪一版，也没有地方写"这一步只跑一次"。而且改错了没有回头路。

规则：
1. **迁移一经发布就不可修改。** 已应用的迁移会记下 SQL 的 sha256；下次启动
   发现对不上就报警而不是默默重跑——那说明有人改了历史，两台机器的 schema
   会悄悄分叉。要改就追加新的一版。
2. **每一版单独一个事务，记账行也在里面。** 中途失败只回滚那一版，前面成功的
   仍然记账，重启后从断点继续，不会出现"跑了一半又从头来"。
   ADD COLUMN 这种没有 IF NOT EXISTS 的语句，执行前对照实际表结构跳过已生效的，
   所以任何一版都可以安全重跑（见 _pending_sql）。
3. **对既有安装无痛。** 第 1 版就是历史上那段 `CREATE TABLE IF NOT EXISTS`，
   在老库上是空操作，跑完只是把版本号补记上。
4. **可查。** `/healthz` 会带上当前版本号，出问题时先看这个。
"""

import hashlib
import logging
import re

import aiosqlite

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 第 1 版
# 历史遗留的建表语句原样搬过来。老库上全部是空操作，新库上一次建齐。
V1_BASELINE = """
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

# ---------------------------------------------------------------- 第 2 版
# 行车记录仪 SEI 遥测的一次性入库结果。每次翻看片段都重新解一遍 SEI 太慢
# （一段 60 秒的 H.264 要遍历上千个 NAL），而且"整个素材库里所有急刹在哪"
# 这类问题只有落库之后才问得出来。
V2_SEI_INDEX = """
CREATE TABLE IF NOT EXISTS clip_sei (
    path       TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL,
    camera     TEXT,
    seg_start  REAL NOT NULL,
    mtime      REAL NOT NULL,
    indexed_at REAL NOT NULL,
    duration_s REAL,
    samples    INTEGER DEFAULT 0,
    fsd_s      REAL DEFAULT 0,
    ap_s       REAL DEFAULT 0,
    tacc_s     REAL DEFAULT 0,
    manual_s   REAL DEFAULT 0,
    dist_km    REAL DEFAULT 0,
    speed_max  REAL,
    speed_avg  REAL,
    disengage  INTEGER DEFAULT 0,
    lat        REAL,
    lon        REAL
);
CREATE INDEX IF NOT EXISTS idx_clipsei_start ON clip_sei (seg_start DESC);
CREATE INDEX IF NOT EXISTS idx_clipsei_event ON clip_sei (event_id);

CREATE TABLE IF NOT EXISTS sei_event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT NOT NULL,
    event_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    ts        REAL NOT NULL,
    t_offset  REAL NOT NULL,
    magnitude REAL,
    speed_kmh REAL,
    mode      TEXT,
    lat       REAL,
    lon       REAL
);
CREATE INDEX IF NOT EXISTS idx_seiev_ts ON sei_event (ts DESC);
CREATE INDEX IF NOT EXISTS idx_seiev_kind ON sei_event (kind, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_seiev_uniq ON sei_event (path, kind, t_offset);
"""


# ---------------------------------------------------------------- 第 3 版
# clip_sei 的身份从「文件」改成「分段」。
#
# 起因：_pick_camera 优先前视，而 scanner 会跳过 0 字节的文件。车正在写这一
# 分钟的 front.mp4 时扫描，这一段以 back 的路径入库；下一轮 front 完整了，
# 优先级更高被选中，于是同一个 60 秒分段插出第二行 —— 它的 FSD 时长、里程、
# 急刹次数在 fsd_summary 里被计两遍，(path,kind,t_offset) 那个唯一索引拦不住。
#
# 先按 (event_id, seg_start) 去重留最新一行，再建唯一索引。
V3_CLIP_SEI_SEGMENT_KEY = """
DELETE FROM clip_sei WHERE rowid NOT IN (
    SELECT MAX(rowid) FROM clip_sei GROUP BY event_id, seg_start
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clipsei_seg ON clip_sei (event_id, seg_start);
"""


# ---------------------------------------------------------------- 第 4 版
# 区分「这段录像读不出来」和「这段录像没有 SEI 遥测」。
#
# 两者在界面上原本都显示成"没有遥测"，但用户要做的事完全不同：
# 前者去查 U 盘/文件损坏，后者去查固件版本（SEI 只存在于 2025.44.25+ 的
# 行驶片段，停车哨兵片段永远没有）。指错方向比不说更浪费时间。
# 已有行默认 1（可读）：它们是严格模式之前入库的，无从追溯，
# 而假设"可读"至少不会平白冤枉用户的 U 盘。
V4_CLIP_READABLE = """
ALTER TABLE clip_sei ADD COLUMN readable INTEGER NOT NULL DEFAULT 1;
"""


# ---------------------------------------------------------------- 第 5 版
# 再分出一类：读不出来是因为**加密**，还是因为**损坏**。
# 固件 2026.20+ 默认加密，那有明确解法（车机里关掉，或用账号换密钥解开）；
# 文件损坏没有解法，只能重新录。把两者混在一起报，等于让一半用户白折腾。
V5_CLIP_ENCRYPTED = """
ALTER TABLE clip_sei ADD COLUMN encrypted INTEGER NOT NULL DEFAULT 0;
"""


# (版本号, 名称, SQL)。**只能往后追加，永远不要改已发布的条目。**
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "baseline", V1_BASELINE),
    (2, "sei_index", V2_SEI_INDEX),
    (3, "clip_sei_segment_key", V3_CLIP_SEI_SEGMENT_KEY),
    (4, "clip_readable", V4_CLIP_READABLE),
    (5, "clip_encrypted", V5_CLIP_ENCRYPTED),
]

SCHEMA_VERSION = max(v for v, _, _ in MIGRATIONS)

_TRACKER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at REAL NOT NULL
);
"""


def _checksum(sql: str) -> str:
    # 只对语义字符做哈希：改个缩进或注释不该被当成"历史被篡改"
    normalized = " ".join(sql.split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


async def current_version(db: aiosqlite.Connection) -> int:
    try:
        async with db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
    except aiosqlite.OperationalError:
        return 0          # 表还不存在 = 全新库


_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+\"?(\w+)\"?\s+ADD\s+(?:COLUMN\s+)?\"?(\w+)\"?[^;]*;?",
    re.IGNORECASE,
)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_]+$")


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    try:
        async with db.execute(f'PRAGMA table_info("{table}")') as cur:
            return {row[1] async for row in cur}
    except aiosqlite.OperationalError:
        return set()


async def _pending_sql(db: aiosqlite.Connection, sql: str) -> str:
    """去掉"列已经存在"的 ADD COLUMN 语句，其余原样保留。

    SQLite 的 `ALTER TABLE … ADD COLUMN` 没有 IF NOT EXISTS。第 4、5 版就是这种
    语句：进程在 ALTER 之后、记账之前被杀（或者磁盘满了），列已经加上、版本却没
    记上，以后每次启动都会报 duplicate column name，hub 起不来、restart: always
    无限重启（审计 F9）。这里在执行前对照实际表结构，跳过已经生效的那一句 ——
    已发布的 SQL 文本一个字都不改，checksum 不漂移。
    """
    out: list[str] = []
    pos = 0
    for m in _ADD_COLUMN.finditer(sql):
        out.append(sql[pos:m.start()])
        table, col = m.group(1), m.group(2)
        if col in await _columns(db, table):
            log.info("migration: %s.%s already exists, skipping its ADD COLUMN", table, col)
        else:
            out.append(m.group(0))
        pos = m.end()
    out.append(sql[pos:])
    return "".join(out)


async def migrate(db: aiosqlite.Connection) -> dict:
    """把库推进到最新版，返回这次做了什么。

    每一版连同它的记账行在**同一个事务**里提交（SQLite 的 DDL 是事务性的）：
    中途失败或进程被杀，这一版要么全有要么全无，下次启动从这一版重来。
    再加上 _pending_sql 对 ADD COLUMN 的幂等处理，就算是旧版本代码留下的
    "列加了、账没记"的库，也能自己走完。
    """
    import time

    await db.executescript(_TRACKER)
    await db.commit()

    applied: dict[int, tuple[str, str]] = {}
    async with db.execute("SELECT version, name, checksum FROM schema_migrations") as cur:
        async for row in cur:
            applied[row[0]] = (row[1], row[2])

    ran: list[str] = []
    drifted: list[str] = []

    for version, name, sql in MIGRATIONS:
        digest = _checksum(sql)
        if version in applied:
            if applied[version][1] != digest:
                # 不自动修。两台机器上同一个版本号对应不同 schema 是最难查的
                # 那类故障，宁可吵闹地报出来让人处理。
                drifted.append(f"v{version} {name}")
                log.error(
                    "migration v%s (%s) changed after it was applied — "
                    "recorded %s, now %s. Schema may differ across installs; "
                    "add a new migration instead of editing history.",
                    version, name, applied[version][1], digest,
                )
            continue

        if not (_SAFE_NAME.match(name) and _SAFE_NAME.match(digest)):
            raise ValueError(f"migration v{version}: unsafe name/checksum")
        body = (await _pending_sql(db, sql)).strip()
        # 记账值是本模块的常量（整数版本号、[A-Za-z0-9_] 的名字和十六进制摘要），
        # 直接写进脚本，才能和 DDL 放进同一个 BEGIN … COMMIT。
        script = (
            "BEGIN;\n"
            f"{body}\n;\n"
            "INSERT INTO schema_migrations (version, name, checksum, applied_at)"
            f" VALUES ({int(version)}, '{name}', '{digest}', {time.time():.3f});\n"
            "COMMIT;"
        )
        try:
            await db.executescript(script)
        except Exception:
            # 脚本在 COMMIT 之前失败：整版回滚，库停在上一版，下次启动重来
            if db.in_transaction:
                await db.execute("ROLLBACK")
            log.exception("migration v%s (%s) failed; rolled back", version, name)
            raise
        ran.append(f"v{version} {name}")
        log.info("applied migration v%s %s", version, name)

    return {"version": SCHEMA_VERSION, "applied": ran, "drifted": drifted}
