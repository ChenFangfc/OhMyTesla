"""迁移中断后能自己走完（审计 data-api F9）。

旧代码：第 4/5 版是裸的 `ALTER TABLE … ADD COLUMN`，executescript 会先把 ALTER
提交掉，再去写记账行。容器在两者之间被杀（或磁盘满了），以后每次启动都报
`duplicate column name: readable`，store.open() 失败，restart: always 无限重启。

现在：每一版连同记账行在同一个事务里；ADD COLUMN 执行前对照实际表结构跳过
已经生效的那一句。已发布的 SQL 文本不变，checksum 不漂移。

跑法：python3 services/hub/tests/test_data_migrations.py（需要 aiosqlite）
"""

import asyncio
import os
import sys
import tempfile

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break

try:
    import aiosqlite
    from app import migrations as M
except ImportError as e:
    print(f"⏭  skipped: needs the hub runtime ({e}); run with tests/run.sh --docker")
    sys.exit(0)

FAILS: list[str] = []


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


async def cols(db, table):
    return {r[1] for r in await db.execute_fetchall(f"PRAGMA table_info({table})")}


async def versions(db):
    return [r[0] for r in await db.execute_fetchall("SELECT version FROM schema_migrations ORDER BY 1")]


async def main() -> int:
    d = tempfile.mkdtemp()

    print("旧代码留下的半截状态：v4 的列加上了，记账行没有")
    path = os.path.join(d, "legacy.db")
    db = await aiosqlite.connect(path)
    await db.executescript(M._TRACKER)
    for v, name, sql in M.MIGRATIONS[:3]:
        await db.executescript(sql)
        await db.execute("INSERT INTO schema_migrations VALUES (?,?,?,0)", (v, name, M._checksum(sql)))
    await db.commit()
    await db.executescript(M.V4_CLIP_READABLE)      # 只跑了 ALTER，没记账
    await db.commit()
    try:
        r = await M.migrate(db)
        chk("启动不再失败", True)
        chk("v4、v5 都记上了", r["applied"] == ["v4 clip_readable", "v5 clip_encrypted"], r["applied"])
    except Exception as e:  # noqa: BLE001
        chk("启动不再失败", False, repr(e))
    chk("两列都在且只有一份", {"readable", "encrypted"} <= await cols(db, "clip_sei"))
    r = await M.migrate(db)
    chk("再启动一次是空操作", r["applied"] == [] and r["drifted"] == [], r)
    await db.close()

    print("某一版在中途失败：整版回滚，下次从这一版重来")
    path = os.path.join(d, "fail.db")
    db = await aiosqlite.connect(path)
    saved = list(M.MIGRATIONS)
    bad = (6, "boom", "ALTER TABLE clip_sei ADD COLUMN half_done INTEGER;\nTHIS IS NOT SQL;")
    M.MIGRATIONS.append(bad)
    try:
        await M.migrate(db)
        chk("坏迁移应当抛错", False)
    except Exception:  # noqa: BLE001
        chk("坏迁移抛错", True)
    chk("前 5 版照常记账", await versions(db) == [1, 2, 3, 4, 5], await versions(db))
    chk("失败那一版的 ALTER 一并回滚（没有半截列）", "half_done" not in await cols(db, "clip_sei"))
    chk("连接没有卡在事务里", not db.in_transaction)
    M.MIGRATIONS[-1] = (6, "boom", "ALTER TABLE clip_sei ADD COLUMN half_done INTEGER;")
    r = await M.migrate(db)
    chk("修好后重跑这一版成功", r["applied"] == ["v6 boom"], r)
    M.MIGRATIONS[:] = saved
    await db.close()

    print("已发布 SQL 不变 → checksum 不漂移")
    path = os.path.join(d, "fresh.db")
    db = await aiosqlite.connect(path)
    await M.migrate(db)
    rows = dict(await db.execute_fetchall("SELECT version, checksum FROM schema_migrations"))
    chk("记账的 checksum 与源码一致", all(rows[v] == M._checksum(sql) for v, _, sql in M.MIGRATIONS))
    await db.execute("UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 4")
    await db.commit()
    r = await M.migrate(db)
    chk("篡改历史仍然被报出来", r["drifted"] == ["v4 clip_readable"], r)
    await db.close()

    print("ADD COLUMN 的识别")
    db = await aiosqlite.connect(os.path.join(d, "re.db"))
    await db.execute("CREATE TABLE t (a INTEGER)")
    sql = 'ALTER TABLE t ADD COLUMN a INTEGER;\nALTER TABLE "t" ADD b TEXT NOT NULL DEFAULT \'x;y\';'
    out = await M._pending_sql(db, sql)
    chk("已存在的列被跳过、其余原样保留", "COLUMN a" not in out and "ADD b TEXT" in out, out)
    await db.close()

    print()
    print("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
