"""迁移框架的行为验证：幂等、漂移检测、断点续跑。"""
import asyncio, sys, tempfile, os
# 容器里代码在 /app，宿主机上在 services/hub。两边都能跑。
for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p); break
import aiosqlite
from app import migrations as M

async def main():
    fails = []
    def chk(name, cond, extra=""):
        print(f"  {'✅' if cond else '❌'} {name}{(' — ' + extra) if extra else ''}")
        if not cond: fails.append(name)

    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.db")

    # ① 全新库：全部迁移跑一遍
    db = await aiosqlite.connect(path); db.row_factory = aiosqlite.Row
    r1 = await M.migrate(db)
    chk("全新库应用了全部迁移", len(r1["applied"]) == len(M.MIGRATIONS), str(r1["applied"]))
    chk("版本号正确", r1["version"] == M.SCHEMA_VERSION)
    tables = {r[0] for r in await db.execute_fetchall("select name from sqlite_master where type='table'")}
    chk("关键表都建出来了", {"alerts","settings","clip_sei","sei_event","schema_migrations"} <= tables)

    # ② 再跑一次：一条都不该重跑
    r2 = await M.migrate(db)
    chk("重复运行是空操作", r2["applied"] == [] and r2["drifted"] == [])

    # ③ 有数据的库上再迁移，数据不能丢
    await db.execute("insert into alerts (ts,car_id,rule,level,title) values (1,'1','r','info','t')")
    await db.commit()
    await M.migrate(db)
    n = (await db.execute_fetchall("select count(*) from alerts"))[0][0]
    chk("已有数据在迁移后仍在", n == 1, f"{n} 行")

    # ④ 漂移检测：把已应用迁移的 checksum 改掉，应当被报出来而不是静默重跑
    await db.execute("update schema_migrations set checksum='deadbeef' where version=2")
    await db.commit()
    r4 = await M.migrate(db)
    chk("检测到历史被篡改", len(r4["drifted"]) == 1, str(r4["drifted"]))
    chk("漂移时不重跑迁移", r4["applied"] == [])

    # ⑤ checksum 只认语义：改缩进/注释不算漂移
    a = M._checksum("CREATE TABLE x(a int);")
    b = M._checksum("CREATE  TABLE   x(a int);\n")
    chk("空白差异不算漂移", a == b)
    c = M._checksum("CREATE TABLE x(a text);")
    chk("真实改动能被认出来", a != c)

    # ⑥ 版本号必须连续且唯一 —— 漏号或重号会让记账对不上
    vs = [v for v, _, _ in M.MIGRATIONS]
    chk("版本号唯一", len(vs) == len(set(vs)))
    chk("版本号从 1 起连续", vs == list(range(1, len(vs) + 1)), str(vs))

    await db.close()
    print()
    print("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}")
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
