"""对着一个**真的** Postgres 跑数据层：写路径、超时、schema 守卫、没数据/进行中/归档的车。

只在显式给了 OMT_TEST_PG 时运行（例如 postgresql://teslamate:pw@127.0.0.1:25432/postgres）。
它会新建一个临时库 omt_data_test_<pid>，按 TeslaMate v4.4.0 的列定义建表、灌一份
小数据，跑完删库 —— 不碰同一服务器上的任何其它库。**不要**指向生产库所在的
服务器以外的东西以为它是只读的：电价回写测试会写这个临时库。

覆盖：
- 费用回写：只填空白、绝不覆盖手填、跳过进行中的场次、重复跑幂等（真 SQL）
- 查询超时 → DbTimeout，连接池保留（以前被当成断线，整池拆掉）
- SQL 错误 → 原样的 PostgresError，翻译成 500 db_query_failed（以前一律"尚未就绪"503）
- schema 守卫：ok / newer（比 20261014120000 新）/ incompatible（缺列）
- 分析接口对 没数据的车 / 进行中的行程与充电 / 归档的车 都不报错，数值合理
- 兼容层按声明的单位换算；币种显式选过后不被市场检测冲掉；F10 停充判定

跑法：OMT_TEST_PG=postgresql://... python3 services/hub/tests/test_data_pg.py
"""

import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

DSN = os.environ.get("OMT_TEST_PG", "")
if not DSN:
    print("⏭  skipped: set OMT_TEST_PG=postgresql://user:pw@host:port/postgres to run")
    sys.exit(0)

for _p in ("/opt/ohmytesla", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break

U = urlparse(DSN)
TEST_DB = f"omt_data_test_{os.getpid()}"
os.environ.update({
    "TM_DB_HOST": U.hostname or "127.0.0.1", "TM_DB_PORT": str(U.port or 5432),
    "TM_DB_USER": U.username or "postgres", "TM_DB_PASS": U.password or "",
    "TM_DB_NAME": TEST_DB, "DATA_DIR": tempfile.mkdtemp(), "TZ": "America/Chicago",
    "COST_WRITE_BACK": "false", "OHMYTESLA_PASSWORD": "x",
})
try:
    import asyncpg
    from app import db
    from app.alerts.store import store
    from app.compat import teslamateapi as compat
    from app.i18n import prefs
    from app.insights import analytics as A
    from app.insights import cars as registry
    from app.insights import charging_stops, guard, journey, queries
    from app.insights import costs as K
    from app.insights.period import parse_period
    from app.livestate import live
    from app.tariff import calc as C
except ImportError as e:
    print(f"⏭  skipped: needs the hub runtime ({e})")
    sys.exit(0)

FAILS: list[str] = []
CHI = ZoneInfo("America/Chicago")


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)[:300]) if extra else ''}")
    if not cond:
        FAILS.append(name)


DDL = """
CREATE TYPE billing_type AS ENUM ('per_kwh','per_minute');
CREATE TYPE preferred_range AS ENUM ('ideal','rated');
CREATE TYPE states_status AS ENUM ('online','offline','asleep');
CREATE TYPE unit_of_length AS ENUM ('km','mi');
CREATE TYPE unit_of_pressure AS ENUM ('bar','psi');
CREATE TYPE unit_of_temperature AS ENUM ('C','F');
CREATE TABLE schema_migrations (version bigint PRIMARY KEY, inserted_at timestamp(0));
CREATE TABLE settings (id bigserial PRIMARY KEY, inserted_at timestamp(0) NOT NULL DEFAULT now(),
  updated_at timestamp(0) NOT NULL DEFAULT now(), unit_of_length unit_of_length NOT NULL DEFAULT 'km',
  unit_of_temperature unit_of_temperature NOT NULL DEFAULT 'C', preferred_range preferred_range NOT NULL DEFAULT 'rated',
  base_url varchar(255), grafana_url varchar(255), language text NOT NULL DEFAULT 'en',
  unit_of_pressure unit_of_pressure NOT NULL DEFAULT 'bar', theme_mode text NOT NULL DEFAULT 'system');
CREATE TABLE car_settings (id bigserial PRIMARY KEY, suspend_min integer NOT NULL DEFAULT 21,
  suspend_after_idle_min integer NOT NULL DEFAULT 15, req_not_unlocked boolean NOT NULL DEFAULT false,
  free_supercharging boolean NOT NULL DEFAULT false, use_streaming_api boolean NOT NULL DEFAULT true,
  enabled boolean NOT NULL DEFAULT true, lfp_battery boolean NOT NULL DEFAULT false);
CREATE TABLE cars (id smallserial PRIMARY KEY, eid bigint NOT NULL, vid bigint NOT NULL, model varchar(255),
  efficiency double precision, inserted_at timestamp(0) NOT NULL DEFAULT now(), updated_at timestamp(0) NOT NULL DEFAULT now(),
  vin text NOT NULL, name text, trim_badging text, settings_id bigint NOT NULL REFERENCES car_settings(id),
  exterior_color text, spoiler_type text, wheel_type text, display_priority smallint NOT NULL DEFAULT 1,
  marketing_name varchar(255));
CREATE TABLE addresses (id serial PRIMARY KEY, display_name varchar(512), latitude numeric(8,6), longitude numeric(9,6),
  name varchar(255), house_number varchar(255), road varchar(255), neighbourhood varchar(255), city varchar(255),
  county varchar(255), postcode varchar(255), state varchar(255), state_district varchar(255), country varchar(255),
  raw jsonb, inserted_at timestamp(0) NOT NULL DEFAULT now(), updated_at timestamp(0) NOT NULL DEFAULT now(),
  osm_id bigint, osm_type text);
CREATE TABLE geofences (id serial PRIMARY KEY, name varchar(255) NOT NULL, latitude numeric(8,6) NOT NULL,
  longitude numeric(9,6) NOT NULL, radius smallint NOT NULL DEFAULT 25, inserted_at timestamp(0) NOT NULL DEFAULT now(),
  updated_at timestamp(0) NOT NULL DEFAULT now(), cost_per_unit numeric(9,4), session_fee numeric(14,2),
  billing_type billing_type NOT NULL DEFAULT 'per_kwh');
CREATE TABLE positions (id serial PRIMARY KEY, date timestamp NOT NULL, latitude numeric(8,6) NOT NULL,
  longitude numeric(9,6) NOT NULL, speed smallint, power smallint, odometer double precision,
  ideal_battery_range_km numeric(6,2), battery_level smallint, outside_temp numeric(4,1), elevation smallint,
  fan_status integer, driver_temp_setting numeric(4,1), passenger_temp_setting numeric(4,1), is_climate_on boolean,
  is_rear_defroster_on boolean, is_front_defroster_on boolean, car_id smallint NOT NULL, drive_id integer,
  inside_temp numeric(4,1), battery_heater boolean, battery_heater_on boolean, battery_heater_no_power boolean,
  est_battery_range_km numeric(6,2), rated_battery_range_km numeric(6,2), usable_battery_level smallint,
  tpms_pressure_fl numeric(4,1), tpms_pressure_fr numeric(4,1), tpms_pressure_rl numeric(4,1), tpms_pressure_rr numeric(4,1));
CREATE INDEX ON positions (car_id);
CREATE INDEX positions_full ON positions (car_id, date, (ideal_battery_range_km IS NOT NULL)) WHERE ideal_battery_range_km IS NOT NULL;
CREATE INDEX positions_brin ON positions USING brin (drive_id, date);
CREATE TABLE drives (id serial PRIMARY KEY, start_date timestamp NOT NULL, end_date timestamp, outside_temp_avg numeric(4,1),
  speed_max smallint, power_max smallint, power_min smallint, start_ideal_range_km numeric(6,2), end_ideal_range_km numeric(6,2),
  start_km double precision, end_km double precision, distance double precision, duration_min smallint, car_id smallint NOT NULL,
  inside_temp_avg numeric(4,1), start_address_id integer, end_address_id integer, start_rated_range_km numeric(6,2),
  end_rated_range_km numeric(6,2), start_position_id integer, end_position_id integer, start_geofence_id integer,
  end_geofence_id integer, ascent smallint, descent smallint);
CREATE TABLE charging_processes (id serial PRIMARY KEY, start_date timestamp NOT NULL, end_date timestamp,
  charge_energy_added numeric(8,2), start_ideal_range_km numeric(6,2), end_ideal_range_km numeric(6,2),
  start_battery_level smallint, end_battery_level smallint, duration_min smallint, outside_temp_avg numeric(4,1),
  car_id smallint NOT NULL, position_id integer NOT NULL, address_id integer, start_rated_range_km numeric(6,2),
  end_rated_range_km numeric(6,2), geofence_id integer, charge_energy_used numeric(8,2), cost numeric(14,2));
CREATE TABLE charges (id serial PRIMARY KEY, date timestamp NOT NULL, battery_heater_on boolean, battery_level smallint,
  charge_energy_added numeric(8,2) NOT NULL, charger_actual_current smallint, charger_phases smallint CHECK (charger_phases > 0),
  charger_pilot_current smallint, charger_power smallint NOT NULL, charger_voltage smallint, fast_charger_present boolean,
  conn_charge_cable varchar(255), fast_charger_brand varchar(255), fast_charger_type varchar(255),
  ideal_battery_range_km numeric(6,2) NOT NULL, not_enough_power_to_heat boolean, outside_temp numeric(4,1),
  charging_process_id integer NOT NULL, battery_heater boolean, battery_heater_no_power boolean,
  rated_battery_range_km numeric(6,2), usable_battery_level smallint);
CREATE TABLE states (id serial PRIMARY KEY, state states_status NOT NULL, start_date timestamp NOT NULL, end_date timestamp,
  car_id smallint NOT NULL);
CREATE TABLE updates (id serial PRIMARY KEY, start_date timestamp NOT NULL, end_date timestamp, version varchar(255),
  car_id smallint NOT NULL);
"""

NOW = datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def local(days_ago: int, hh: int, mm: int = 0) -> datetime:
    """N 天前芝加哥本地 hh:mm → naive UTC。"""
    d = (datetime.now(CHI) - timedelta(days=days_ago)).date()
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=CHI).astimezone(UTC).replace(tzinfo=None)


async def seed(con):
    await con.execute(DDL)
    await con.execute("INSERT INTO schema_migrations (version) SELECT 20261014120000")
    await con.execute("INSERT INTO settings DEFAULT VALUES")
    for _ in range(3):
        await con.execute("INSERT INTO car_settings DEFAULT VALUES")
    await con.execute("""INSERT INTO cars (eid, vid, model, efficiency, vin, name, settings_id) VALUES
        (1, 1, 'Cybertruck', 0.234, '7G2CEHED0RA000001', NULL, 1),
        (2, 2, NULL, NULL, '7G2CEHEE0RA000002', NULL, 2),
        (3, 3, 'Y', NULL, '7SAYGDEE0TF000003', 'Empty', 3)""")
    await con.execute("""INSERT INTO addresses (display_name, latitude, longitude, name, road, house_number,
        neighbourhood, city, state, country, raw) VALUES
        ('12 Example Street, Plainville', 40.056, -98.248, NULL, 'Example Street', '12', 'Plainville Hamlet', NULL,
         'Kansas', 'United States', '{"address": {"country_code": "us"}}'),
        ('Office, Springfield', 40.115, -98.225, 'Office', 'Main Street', '100', NULL, 'Springfield', 'Kansas',
         'United States', '{"address": {"country_code": "us"}}')""")
    # 行程 1：昨天 17:00 本地，20 分钟，流式行 1 Hz（含负功率）+ 每 15 s 一个完整行
    t0 = local(1, 17)
    await con.execute("""INSERT INTO drives (start_date, end_date, start_km, end_km, distance, duration_min, car_id,
        start_rated_range_km, end_rated_range_km, start_ideal_range_km, end_ideal_range_km, speed_max, power_max,
        power_min, outside_temp_avg, start_address_id, end_address_id, ascent, descent)
        VALUES ($1, $2, 1000, 1015, 15, 20, 1, 300, 280, 300, 280, 90, 150, -60, 18.0, 1, 2, 30, 10)""",
                      t0, t0 + timedelta(minutes=20))
    rows = []
    for i in range(1200):
        t = t0 + timedelta(seconds=i)
        full = i % 15 == 0
        rows.append((t, 40.056 + i * 5e-5, -98.248 + i * 2e-5, 45 + (i % 40), -20 if 300 < i < 400 else 40,
                     1000 + i * 15 / 1200, 300 - i * 20 / 1200 if full else None, 80 - i // 120,
                     18.0 if full else None, 200 + (i // 60), 1, 1, 300 - i * 20 / 1200 if full else None,
                     80 - i // 120 if full else None))
    await con.executemany("""INSERT INTO positions (date, latitude, longitude, speed, power, odometer,
        ideal_battery_range_km, battery_level, outside_temp, elevation, car_id, drive_id, rated_battery_range_km,
        usable_battery_level) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""", rows)
    # 行程 2：进行中（end_date NULL），10 分钟前开始
    t2 = NOW - timedelta(minutes=10)
    d2 = await con.fetchval("""INSERT INTO drives (start_date, car_id, start_rated_range_km, start_ideal_range_km)
        VALUES ($1, 1, 270, 270) RETURNING id""", t2)
    await con.executemany("""INSERT INTO positions (date, latitude, longitude, speed, power, odometer,
        battery_level, car_id, drive_id, ideal_battery_range_km, rated_battery_range_km, usable_battery_level)
        VALUES ($1, 40.06, -98.25, 50, 30, $2, 70, 1, $3, $4, $4, 70)""",
                          [(t2 + timedelta(seconds=15 * k), 1100 + k * 0.2, d2, 270 - k * 0.2) for k in range(30)])
    # 充电：两场交流都在 06:00 本地停（谷电结束），一场直流超充，一场手填费用，一场进行中
    pos = await con.fetchval("""INSERT INTO positions (date, latitude, longitude, odometer, car_id,
        ideal_battery_range_km, rated_battery_range_km, battery_level, usable_battery_level)
        VALUES ($1, 40.056, -98.248, 1015, 1, 280, 280, 60, 60) RETURNING id""", local(3, 22))
    cps = [  # (start, end, added, used, dc, cost)
        (local(3, 23), local(2, 6), 20.0, 23.0, False, None),
        (local(2, 23), local(1, 6), 22.0, 25.5, False, None),
        (local(1, 12), local(1, 12, 30), 40.0, 41.0, True, None),
        (local(5, 23), local(4, 6), 18.0, 21.0, False, 9.99),
        (NOW - timedelta(minutes=30), None, None, None, False, None),
    ]
    for s, e, added, used, dc, cost in cps:
        cid = await con.fetchval("""INSERT INTO charging_processes (start_date, end_date, charge_energy_added,
            charge_energy_used, car_id, position_id, address_id, start_battery_level, end_battery_level,
            start_rated_range_km, end_rated_range_km, start_ideal_range_km, end_ideal_range_km, duration_min, cost)
            VALUES ($1,$2,$3,$4,1,$5,1,40,60,200,300,200,300,$6,$7) RETURNING id""",
                                 s, e, added, used, pos,
                                 None if e is None else int((e - s).total_seconds() // 60), cost)
        end = e or NOW
        n = max(int((end - s).total_seconds() // 60), 2)
        samples = []
        for k in range(n + 1):
            t = s + timedelta(minutes=k)
            if dc:
                samples.append((t, 40 + k // 2, k * (added or 1) / n, None, 150, None, None, True, "Tesla",
                                "Tesla", cid))
            else:
                samples.append((t, 40 + k // 20, k * (added or 1) / n, 1, 7, 32, 240, False, None, None, cid))
        await con.executemany("""INSERT INTO charges (date, battery_level, charge_energy_added, charger_phases,
            charger_power, charger_actual_current, charger_voltage, fast_charger_present, fast_charger_brand,
            fast_charger_type, charging_process_id, ideal_battery_range_km, rated_battery_range_km,
            usable_battery_level) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,250,250,$2)""", samples)
    await con.execute("""INSERT INTO states (state, start_date, end_date, car_id) VALUES
        ('online', $1, $2, 1), ('asleep', $2, NULL, 1)""", local(2, 0), NOW - timedelta(hours=2))
    await con.execute("""INSERT INTO updates (start_date, end_date, version, car_id) VALUES
        ($1, $2, '2026.26.6.5 abc', 1), ($3, $3, '2026.38.5.11 def', 1)""",
                      local(20, 1), local(20, 1, 40), local(2, 3))
    # 4 号车：两段行程隔了 38 天，里程表却走了 1060 km（TeslaMate 停了一个多月没记），
    # 中间夹着一次软件更新 —— 更新没有里程读数，不能把这段漏采集切成两截"停放"
    await con.execute("INSERT INTO car_settings DEFAULT VALUES")
    await con.execute("""INSERT INTO cars (eid, vid, model, efficiency, vin, name, settings_id)
        VALUES (4, 4, 'Cybertruck', 0.234, '7G2CEHED0RA000004', 'Gap', 4)""")
    await con.execute("""INSERT INTO drives (start_date, end_date, start_km, end_km, distance, duration_min, car_id)
        VALUES ($1, $2, 2000, 2010, 10, 20, 4), ($3, $4, 3070, 3080, 10, 20, 4)""",
                      local(40, 9), local(40, 9, 20), local(2, 9), local(2, 9, 20))
    await con.execute("INSERT INTO updates (start_date, end_date, version, car_id) VALUES ($1, $2, '2026.32.1 x', 4)",
                      local(20, 2), local(20, 2, 30))
    # 2 号车：60 天前最后一次出现，states 里挂着一条没关的 online
    old = NOW - timedelta(days=60)
    await con.execute("""INSERT INTO positions (date, latitude, longitude, odometer, car_id, ideal_battery_range_km,
        rated_battery_range_km, battery_level) VALUES ($1, 41.0, -98.0, 500, 2, 400, 400, 80)""", old)
    await con.execute("INSERT INTO states (state, start_date, end_date, car_id) VALUES ('online', $1, NULL, 2)", old)


async def main() -> int:
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
    await admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    try:
        con = await asyncpg.connect(DSN.rsplit("/", 1)[0] + f"/{TEST_DB}")
        await seed(con)
        await store.open()
        return await run(con)
    finally:
        await db.close()
        await store.close()
        try:
            await con.close()
        except Exception:  # noqa: BLE001
            pass
        await admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        await admin.close()


async def run(con) -> int:
    print("车辆名册：归档与默认车")
    live.last_update["1"] = __import__("time").time()        # 1 号车有 MQTT
    cars = {c["id"]: c for c in await registry.list_cars(fresh=True)}
    chk("1 号车活着", not cars[1]["archived"], cars[1])
    chk("2 号车 60 天没见、没有 MQTT → archived", cars[2]["archived"], cars[2])
    chk("VIN 第 4 位补出 Cybertruck（model 为 NULL 的老行）", cars[2]["model_line"] == "Cybertruck")
    chk("默认车 = 最近活跃且未归档的那辆", await registry.default_car_id() == 1)
    chk("Cybertruck 坡度修正用 3100 kg", registry.mass_kg("Cybertruck") == 3100)

    print("状态：归档的车不再'连续在线 60 天'")
    s2 = await journey.states_breakdown(2, 30)
    chk("2 号车最近 30 天 online = 0（截到 last_seen）", s2["online_min"] == 0 and s2["archived"], s2)
    s1 = await journey.states_breakdown(1, 3)
    chk("进行中的充电/行程按交集计时，不是 0", s1["charge_min"] > 0 and s1["drive_min"] > 0, s1)
    tl = await A.states_timeline(2, parse_period(30))
    chk("2 号车时间线 covered = 0", tl["covered_min"] == 0, tl["totals_min"])

    print("费用：唯一真源 + 自动定价")
    await store.set_setting("tariff_plan", {"currency": "USD", "default": {"type": "flat", "rate": 0.12},
                                            "supercharger": {"type": "flat", "rate": 0.36}, "locations": {}})
    costs = await K.session_costs(1)
    by = {cid: v for cid, v in costs.items()}
    srcs = sorted(v["source"] for v in by.values())
    chk("来源：3 场 computed、1 场 recorded（手填 9.99）、1 场 ongoing",
        srcs == ["computed", "computed", "computed", "ongoing", "recorded"], srcs)
    ac = [v["cost"] for v in by.values() if v["source"] == "computed"]
    chk("交流按墙端电量计费：23.0×0.12 / 25.5×0.12；直流 41×0.36",
        sorted(ac) == sorted([2.76, 3.06, 14.76]), ac)
    summ = await queries.summary(1)
    st = await A.statistics(1, parse_period(0), "total")
    chk("汇总页与统计页的费用一致", abs(summ["charge_cost"] - (st["rows"][0]["cost"] or 0)) < 0.01,
        (summ["charge_cost"], st["rows"][0]["cost"]))

    print("回写（真 SQL）：只填空白、绝不覆盖手填、跳过进行中")
    r = await C.recompute_all(1, write_back=True)
    rows = {x["id"]: x for x in await con.fetch("SELECT id, cost::float8 AS cost, end_date FROM charging_processes")}
    chk("手填的 9.99 原样保留", rows[4]["cost"] == 9.99, rows[4])
    chk("进行中的场次仍为空", rows[5]["cost"] is None)
    chk("其余三场写进去了", all(rows[i]["cost"] is not None for i in (1, 2, 3)) and r["written_back"] == 3, r)
    r2 = await C.recompute_all(1, write_back=True)
    chk("再跑一次：0 写入 / 3 不变 / 1 保护", (r2["written_back"], r2["unchanged"], r2["protected"]) == (0, 3, 1), r2)
    a, b = await asyncio.gather(C.recompute_all(1, write_back=True), C.recompute_all(1, write_back=True))
    reg = await C.written_registry()
    chk("并发两次之后登记表完好", sorted(reg) == [1, 2, 3], reg)

    print("超时 ≠ 断线；SQL 错误 ≠ 尚未就绪")
    pool = db._pool
    try:
        await db.fetch("SELECT pg_sleep(2)", timeout_s=0.3)
        chk("超时应抛 DbTimeout", False)
    except db.DbTimeout:
        chk("超时抛 DbTimeout", True)
    chk("连接池没有被拆", db._pool is pool and await db.fetchval("SELECT 1") == 1)
    try:
        await db.fetch("SELECT * FROM drives LIMIT -1")
        chk("负 LIMIT 应当报错", False)
    except asyncpg.PostgresError as e:
        err = guard.http_error(e)
        chk("SQL 错误 → 500 db_query_failed（不是'尚未就绪'503）",
            err.status_code == 500 and err.headers["X-Error-Code"] == "db_query_failed", err.detail)
    err = guard.http_error(db.DbTimeout("x"))
    chk("超时 → 504", err.status_code == 504)

    print("分析接口：没数据 / 进行中 / 归档")
    for cid in (1, 2, 3):
        p = parse_period(0)
        for name, coro in (("drives", A.drives_table(cid, p)), ("charges", A.charges_table(cid, p)),
                           ("stats", A.statistics(cid, p, "month")), ("eff", A.efficiency(cid, p)),
                           ("vampire", A.vampire(cid, p)), ("battery", A.battery(cid)),
                           ("mileage", A.mileage(cid, p)), ("locations", A.locations(cid, p)),
                           ("updates", A.updates(cid)), ("level", A.charge_level(cid, parse_period(30))),
                           ("charging", A.charging_stats(cid, p))):
            try:
                await coro
                ok = True
            except Exception as e:  # noqa: BLE001
                ok = repr(e)
            if ok is not True:
                chk(f"car {cid} {name}", False, ok)
    chk("三辆车的所有分析接口都不抛异常", not [f for f in FAILS if f.startswith("car ")])
    d = await A.drive_detail(2)
    chk("进行中的行程：open、时长按最后一个点算", d["drive"]["open"] and d["drive"]["duration_min"] >= 7, d["drive"])
    d = await A.drive_detail(1)
    chk("动能回收按流式负功率积分（~99 s × 20 kW）", 0.5 < (d["energy"]["regen_kwh"] or 0) < 0.6, d["energy"])
    chk("坡度修正用 Cybertruck 的 3100 kg", d["energy"]["mass_kg"] == 3100)
    chk("路线是 GeoJSON LineString", d["route"]["type"] == "LineString" and len(d["route"]["coordinates"]) > 10)
    c = await A.charge_detail(5)
    chk("进行中的充电：open、费用来源 ongoing", c["charge"]["open"] and c["charge"]["cost"]["source"] == "ongoing")
    c = await A.charge_detail(1)
    chk("充电详情：墙端 vs 电池端、效率", c["charge"]["energy"]["billed_kwh"] == 23.0
        and c["charge"]["energy"]["efficiency_pct"] == round(20 / 23 * 100, 1), c["charge"]["energy"])
    loc = await A.locations(1, parse_period(0))
    chk("城市取 COALESCE(city, neighbourhood)", any(x["city"] == "Plainville Hamlet" for x in loc["cities"]), loc["cities"])
    fp = await A.footprint(1, parse_period(0))
    cells = fp["cells"]
    chk("足迹：20 分钟的行程点亮了一串 ~1 km 格子", 3 <= len(cells) <= 60, len(cells))
    chk("足迹：格子按首次到达排序，时间戳是秒", all(cells[i][2] <= cells[i + 1][2] for i in range(len(cells) - 1))
        and cells[0][2] > 1_600_000_000, cells[:2])
    chk("足迹：面积约等于格子数 × ~0.94 km²（纬度 40°）", abs(fp["explored_km2"] / max(len(cells), 1) - 0.94) < 0.05,
        fp["explored_km2"])
    chk("足迹：城市与「第一次」时间线", fp["counts"]["cities"] >= 1 and fp["firsts"]
        and all(fp["firsts"][i]["at"] <= fp["firsts"][i + 1]["at"] for i in range(len(fp["firsts"]) - 1)), fp["firsts"])
    chk("足迹：边界框包住所有格子", fp["bounds"] and fp["bounds"][0] <= 40.056 <= fp["bounds"][2], fp["bounds"])
    tl4 = await journey.timeline(4, days=60)
    kinds = [e["kind"] for e in tl4["events"]]
    gaps = [e for e in tl4["events"] if e["kind"] == "gap"]
    chk("漏采集中间夹着软件更新：仍判成一段 gap，带上漏掉的里程",
        len(gaps) == 1 and abs((gaps[0]["unlogged_km"] or 0) - 1060) < 1 and "idle" not in kinds, kinds)
    fp3 = await A.footprint(3, parse_period(0))
    chk("没数据的车：足迹为空，不报错", fp3["cells"] == [] and fp3["bounds"] is None and fp3["explored_km2"] == 0)
    m = await A.mileage(3, parse_period(0))
    chk("没数据的车：mileage 空表", m["rows"] == [] and m["total_km"] == 0)

    print("F10：停充是不是中断")
    r = await charging_stops.classify_stop(1, local(0, 6, 1))
    chk("06:01 停 → 学到的谷电结束时刻（scheduled_stop）", r["reason"] == "scheduled_stop", r)
    r = await charging_stops.classify_stop(1, local(0, 14, 37))
    chk("14:37 停 → unexplained（报）", r["interrupted"] and r["reason"] == "unexplained", r)
    r = await charging_stops.classify_stop(1, is_dc=True)
    chk("直流快充结束 → 不报", r["reason"] == "dc_session")
    r = await charging_stops.classify_stop(1, end_soc=80, charge_limit_soc=80)
    chk("到上限 → 不报", r["reason"] == "limit_reached")
    r = await charging_stops.classify_stop(1, charging_state="NoPower")
    chk("NoPower → 报", r["interrupted"] and r["reason"] == "no_power")

    print("兼容层按声明的单位换算")
    await prefs.set_prefs({"length": "mi", "temp": "F", "pressure": "psi", "auto": False})
    live.cars["1"] = {"odometer": "44521.05", "inside_temp": "22.4", "tpms_pressure_fl": "3.45",
                      "rated_battery_range_km": "347.39", "speed": "100", "state": "online"}
    from starlette.requests import Request

    req = Request({"type": "http", "headers": [], "method": "GET", "path": "/"})
    s = (await compat.status_v1(1, req))["data"]
    st_ = s["status"]
    chk("里程 km → mi（44521.05 / 1.609344）", abs(st_["odometer"] - 27664.10) < 0.01, st_["odometer"])
    chk("车内温度 °C → °F", st_["climate_details"]["inside_temp"] == 72.3)
    chk("胎压 bar → psi", abs(st_["tpms_details"]["tpms_pressure_fl"] - 50.04) < 0.01)
    chk("续航 km → mi", abs(st_["battery_details"]["rated_battery_range"] - 215.86) < 0.01)
    chk("速度 km/h → mph（整数）", st_["driving_details"]["speed"] == 62)
    dv = (await compat.drives_v1(1, req, limit=-5))["data"]
    chk("limit=-5 夹到 1，不再 503", len(dv["drives"]) == 1 and dv["units"]["unit_of_length"] == "mi")
    chk("能耗 Wh/km → Wh/mi 是 ×1.609", dv["drives"][0]["consumption_net"] == round(20 * 0.234 * 1000 / 15 * 1.609344, 1),
        dv["drives"][0]["consumption_net"])

    print("币种显式选过后不被市场检测覆盖")
    await prefs.set_prefs({"currency": "EUR"})
    p = await prefs.apply_market("US")
    chk("下次启动的市场检测不再把 EUR 改回 USD", p["currency"] == "EUR" and p["currency_set"], p)

    print("schema 守卫")
    s = await db.check_teslamate_schema(force=True)
    chk("v4.4.0 的 schema → ok", s["status"] == "ok", s)
    await con.execute("INSERT INTO schema_migrations (version) VALUES (20270101000000)")
    s = await db.check_teslamate_schema(force=True)
    chk("比 20261014120000 新 → newer", s["status"] == "newer" and s["version"] == 20270101000000, s)
    await con.execute("ALTER TABLE drives DROP COLUMN ascent")
    s = await db.check_teslamate_schema(force=True)
    chk("缺列 → incompatible 并点名", s["status"] == "incompatible" and "drives.ascent" in s["missing_columns"], s)
    try:
        await A.drive_detail(1)
        chk("缺列时接口应失败", False)
    except asyncpg.PostgresError as e:
        err = guard.http_error(e)
        chk("缺列 → 503 tm_schema（说清楚是 schema 变了）",
            err.status_code == 503 and err.headers["X-Error-Code"] == "tm_schema", err.detail)

    print()
    print("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
