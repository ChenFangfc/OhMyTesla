"""时间维度与地理维度的只读统计：时间线、日历、足迹、地点、周期汇总、状态占比。

与 queries.py 的分工：queries.py 回答"这辆车整体怎么样"（电池健康、能耗、充电、
纪录），这里回答"什么时候发生了什么"和"去过哪儿"。两者共用 db 层与 _iso 约定。

分桶的时区问题：TeslaMate 的时间列是 naive UTC（见 queries._iso）。日历热力图
按 UTC 分桶会让东八区用户的凌晨行程落到前一天，所以所有按天/周/月的聚合都在
SQL 里显式 `AT TIME ZONE 'UTC' AT TIME ZONE <本地时区>` 之后再 date_trunc。
"""

import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import db
from .queries import _iso

log = logging.getLogger(__name__)

# date_trunc 的粒度不能拼进 SQL 字符串就完事——白名单挡住注入，也挡住
# Postgres 不认识的粒度（那会是 500 而不是 400）。
_BUCKETS = {"day", "week", "month", "year"}


def _place_label(g: str, a: str) -> str:
    """地点显示名的取值优先级。

    直接用 addresses.display_name 会在界面上铺出一整行
    "12, Example Street, Springfield, Example County, Kansas, 66000, United States"。
    Nominatim 的 name 只在 POI 上有值（商店、机构），住宅地址是空串而不是
    NULL，所以必须 NULLIF 才能落到下一档；再没有就用「门牌 + 路名」，
    最后才退回完整串。
    """
    return (
        f"COALESCE({g}.name, NULLIF({a}.name, ''),"
        f" NULLIF(trim(concat_ws(' ', {a}.house_number, {a}.road)), ''),"
        f" NULLIF({a}.city, ''), {a}.display_name, '')"
    )


# 三处用到的地点标签表达式，按 JOIN 别名各生成一份。
_LBL_FROM = _place_label("sg", "sa")   # 行程起点
_LBL_TO = _place_label("eg", "ea")     # 行程终点
_LBL_PLACE = _place_label("g", "a")    # 充电地点 / 常去地点


def local_tz() -> str:
    """给 Postgres 用的 IANA 时区名。

    容器里通常只有 TZ 环境变量；拿不到或者名字不合法就退回 UTC——
    宁可全站统一偏移，也不要让 SQL 抛 invalid time zone。
    """
    from ..config import settings

    name = (getattr(settings, "tz", "") or os.environ.get("TZ") or "").strip()
    if not name:
        return "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r, falling back to UTC", name)
        return "UTC"
    return name


async def timeline(car_id: int, days: int | None = 30, limit: int = 300) -> dict:
    """统一时间线：行程 / 充电 / 软件更新按时间倒序排在一条轴上，
    相邻两件事之间的空档折算成"停放"。

    TeslaMate 的 states 表只有 online/offline/asleep 三种，拿它拼时间线会
    把一次行程切成好几段 online；所以这里以"事件"为单位（drives、
    charging_processes、updates），停放由相邻事件的空档反推。
    """
    window = ""
    args: list = [car_id]
    if days and days > 0:
        args.append(int(days))
        window = " AND start_date >= now() - ($2::int * INTERVAL '1 day')"

    rows = await db.fetch(
        f"""
        WITH ev AS (
            SELECT 'drive'::text AS kind, d.id, d.start_date, d.end_date,
                   d.distance AS distance, d.duration_min,
                   d.start_rated_range_km - d.end_rated_range_km AS range_used,
                   NULL::numeric AS energy, NULL::numeric AS cost,
                   d.start_battery, d.end_battery,
                   d.start_km AS odo_start, d.end_km AS odo_end,
                   {_LBL_FROM} AS from_label,
                   {_LBL_TO} AS to_label,
                   NULL::text AS version
            FROM (
                SELECT d.*,
                       COALESCE(ps.usable_battery_level, ps.battery_level) AS start_battery,
                       COALESCE(pe.usable_battery_level, pe.battery_level) AS end_battery
                FROM drives d
                LEFT JOIN positions ps ON ps.id = d.start_position_id
                LEFT JOIN positions pe ON pe.id = d.end_position_id
            ) d
            LEFT JOIN geofences sg ON sg.id = d.start_geofence_id
            LEFT JOIN geofences eg ON eg.id = d.end_geofence_id
            LEFT JOIN addresses sa ON sa.id = d.start_address_id
            LEFT JOIN addresses ea ON ea.id = d.end_address_id
            WHERE d.car_id = $1{window}

            UNION ALL

            SELECT 'charge', cp.id, cp.start_date, cp.end_date,
                   NULL, cp.duration_min,
                   cp.end_rated_range_km - cp.start_rated_range_km,
                   cp.charge_energy_added, cp.cost,
                   cp.start_battery_level, cp.end_battery_level,
                   p.odometer, p.odometer,
                   NULL, {_LBL_PLACE}, NULL
            FROM charging_processes cp
            LEFT JOIN geofences g ON g.id = cp.geofence_id
            LEFT JOIN addresses a ON a.id = cp.address_id
            LEFT JOIN positions p ON p.id = cp.position_id
            WHERE cp.car_id = $1{window}

            UNION ALL

            SELECT 'update', u.id, u.start_date, u.end_date,
                   NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, u.version
            FROM updates u
            WHERE u.car_id = $1{window}
        )
        SELECT * FROM ev ORDER BY start_date DESC LIMIT {int(limit)}
        """,  # noqa: S608 - 插值项均为模块常量 _LBL_* 与本函数构造的 window/limit；car_id/days 走 $1/$2
        *args,
    )

    def num(v):
        return float(v) if v is not None else None

    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "kind": r["kind"],
                "id": r["id"],
                "start": _iso(r["start_date"]),
                "end": _iso(r["end_date"]),
                "duration_min": r["duration_min"],
                "distance": num(r["distance"]),
                "range_used": num(r["range_used"]),
                "energy": num(r["energy"]),
                "cost": num(r["cost"]),
                "start_soc": r["start_battery"],
                "end_soc": r["end_battery"],
                "odo_start": num(r["odo_start"]),
                "odo_end": num(r["odo_end"]),
                "from": r["from_label"] or None,
                "to": r["to_label"] or None,
                "version": r["version"],
            }
        )

    # 停放是"两件事之间什么都没发生"，所以只能在排好序之后反推。倒序列表里
    # 后一个元素时间更早，空档 = 当前项的 start 减去下一项的 end。
    # 小于 10 分钟的空档不插——那多半是同一次停靠里 TeslaMate 分了两段，
    # 插出来只会让时间线满屏"停放 3 分钟"。
    merged: list[dict] = []
    for i, ev in enumerate(out):
        merged.append(ev)
        if i + 1 >= len(out):
            continue
        nxt = out[i + 1]
        if not ev["start"] or not nxt["end"]:
            continue
        gap_min = (rows[i]["start_date"] - rows[i + 1]["end_date"]).total_seconds() / 60
        if gap_min >= 10:
            # 空档里里程表动了，说明车开过但 TeslaMate 没记上 —— 那不是停放。
            # 把它如实标成 gap，附上跑掉的里程；否则界面上会出现
            # "停放 1 天掉 70%"这种把漏采集说成吸血鬼耗电的说法。
            moved = None
            if nxt["odo_end"] is not None and ev["odo_start"] is not None:
                delta = ev["odo_start"] - nxt["odo_end"]
                # 0.5 km 以内是 GPS/里程表取整噪声，不算移动
                if delta > 0.5:
                    moved = round(delta, 1)
            merged.append(
                {
                    "kind": "gap" if moved else "idle",
                    "unlogged_km": moved,
                    "id": None,
                    "start": nxt["end"],
                    "end": ev["start"],
                    "duration_min": round(gap_min),
                    "distance": None,
                    "range_used": None,
                    "energy": None,
                    "cost": None,
                    # 停放期间掉的电：上一件事结束时的 SOC 减去下一件事开始时的 SOC。
                    # 两端任一缺失就不给，宁可留空也不要编一个 0。
                    "start_soc": nxt["end_soc"],
                    "end_soc": ev["start_soc"],
                    "odo_start": nxt["odo_end"],
                    "odo_end": ev["odo_start"],
                    "from": nxt["to"],
                    "to": None,
                    "version": None,
                }
            )
    # 头条要显示"这段时间一共有多少件事"，而 events 是被 LIMIT 截断过的。
    # 拿 len(events) 当总数，一个每天通勤的用户选"一年"会看到恒定的 300，
    # 旁边的总里程却是全年真值，两个数字互相矛盾且无从察觉。
    totals = await db.fetchrow(
        f"""
        SELECT
          (SELECT COUNT(*) FROM drives
            WHERE car_id = $1{window.replace('start_date', 'drives.start_date')}) AS drives,
          (SELECT COUNT(*) FROM charging_processes
            WHERE car_id = $1{window.replace('start_date', 'charging_processes.start_date')}) AS charges,
          (SELECT COUNT(*) FROM updates
            WHERE car_id = $1{window.replace('start_date', 'updates.start_date')}) AS updates
        """,  # noqa: S608 - window 由本函数构造；car_id/days 走 $1/$2
        *args,
    )
    total = int(totals["drives"]) + int(totals["charges"]) + int(totals["updates"])
    return {
        "events": merged,
        "count": len(merged),
        "total": total,
        "truncated": total > len(out),
        "totals": {"drives": int(totals["drives"]),
                   "charges": int(totals["charges"]),
                   "updates": int(totals["updates"])},
    }


async def calendar(car_id: int, days: int = 365) -> dict:
    """按本地日分桶的活动强度，供月历热力图使用。"""
    tz = local_tz()
    drive_rows = await db.fetch(
        """
        SELECT (start_date AT TIME ZONE 'UTC' AT TIME ZONE $3)::date AS d,
               COUNT(*) AS n,
               COALESCE(SUM(distance), 0) AS km,
               COALESCE(SUM(duration_min), 0) AS minutes,
               COALESCE(SUM(GREATEST(start_rated_range_km - end_rated_range_km, 0)), 0) AS range_used
        FROM drives
        WHERE car_id = $1 AND start_date >= now() - ($2::int * INTERVAL '1 day')
        GROUP BY 1
        """,
        car_id, int(days), tz,
    )
    charge_rows = await db.fetch(
        """
        SELECT (start_date AT TIME ZONE 'UTC' AT TIME ZONE $3)::date AS d,
               COUNT(*) AS n,
               COALESCE(SUM(charge_energy_added), 0) AS kwh,
               COALESCE(SUM(cost), 0) AS cost
        FROM charging_processes
        WHERE car_id = $1 AND start_date >= now() - ($2::int * INTERVAL '1 day')
        GROUP BY 1
        """,
        car_id, int(days), tz,
    )

    byday: dict[str, dict] = {}
    for r in drive_rows:
        byday[r["d"].isoformat()] = {
            "date": r["d"].isoformat(),
            "drives": r["n"],
            "km": round(float(r["km"]), 1),
            "minutes": int(r["minutes"]),
            "range_used": round(float(r["range_used"]), 1),
            "charges": 0,
            "kwh": 0.0,
            "cost": 0.0,
        }
    for r in charge_rows:
        k = r["d"].isoformat()
        cell = byday.setdefault(
            k,
            {"date": k, "drives": 0, "km": 0.0, "minutes": 0, "range_used": 0.0,
             "charges": 0, "kwh": 0.0, "cost": 0.0},
        )
        cell["charges"] = r["n"]
        cell["kwh"] = round(float(r["kwh"]), 2)
        cell["cost"] = round(float(r["cost"]), 2)

    cells = sorted(byday.values(), key=lambda c: c["date"])
    return {
        "tz": tz,
        "days": cells,
        "max_km": max((c["km"] for c in cells), default=0),
        "max_kwh": max((c["kwh"] for c in cells), default=0),
        "total_km": round(sum(c["km"] for c in cells), 1),
        "total_kwh": round(sum(c["kwh"] for c in cells), 2),
        "active_days": len(cells),
    }


async def visited(car_id: int) -> dict:
    """生涯足迹：去过的国家 / 省州 / 城市。

    "去过"取两个来源：行程的终点地址，以及充电的地址。只取终点是因为
    起点几乎总是上一段的终点，算两次会让常去的地方权重翻倍。
    """
    rows = await db.fetch(
        """
        WITH v AS (
            SELECT end_address_id AS aid, end_date AS at
            FROM drives WHERE car_id = $1 AND end_address_id IS NOT NULL
            UNION ALL
            SELECT address_id, start_date
            FROM charging_processes WHERE car_id = $1 AND address_id IS NOT NULL
        )
        SELECT COALESCE(a.country, '') AS country,
               COALESCE(a.state, '')   AS state,
               COALESCE(a.city, '')    AS city,
               COUNT(*)                AS visits,
               MIN(v.at)               AS first_at,
               MAX(v.at)               AS last_at,
               AVG(a.latitude)::float  AS lat,
               AVG(a.longitude)::float AS lon
        FROM v JOIN addresses a ON a.id = v.aid
        GROUP BY 1, 2, 3
        ORDER BY visits DESC
        """,
        car_id,
    )

    cities = [
        {
            "country": r["country"] or None,
            "state": r["state"] or None,
            "city": r["city"] or None,
            "visits": r["visits"],
            "first": _iso(r["first_at"]),
            "last": _iso(r["last_at"]),
            "lat": r["lat"],
            "lon": r["lon"],
        }
        for r in rows
    ]

    countries: dict[str, dict] = {}
    states: set[tuple[str, str]] = set()
    for c in cities:
        if not c["country"]:
            continue
        entry = countries.setdefault(
            c["country"], {"country": c["country"], "visits": 0, "cities": 0}
        )
        entry["visits"] += c["visits"]
        if c["city"]:
            entry["cities"] += 1
        if c["state"]:
            states.add((c["country"], c["state"]))

    return {
        "cities": cities,
        "countries": sorted(countries.values(), key=lambda x: -x["visits"]),
        "n_countries": len(countries),
        "n_states": len(states),
        "n_cities": len({(c["country"], c["city"]) for c in cities if c["city"]}),
    }


async def places(car_id: int, limit: int = 30) -> list[dict]:
    """常去地点排行：到访次数 + 累计停留时长。

    停留时长的算法：一段行程结束到下一段行程开始之间的时间，算在这段行程的
    终点头上。两道过滤：

    - 超过 7 天的空档不计——那不是"停在这儿"，那是这段时间没开车
      （出差、车放在别处、TeslaMate 停机），计进去会让家的停留时长虚高。
    - **里程表在这段空档里走了的，一分钟都不计**。实测本机数据：drive#15 到
      drive#16 之间隔了 97.9 小时，原来全算成"停在家"，但同期里程表跑了
      493.6 公里 —— 那是一趟没被记录的长途。「家」的总停留
      15342 分钟里有 38% 是这么来的。阈值 0.5 km 与 timeline 的空档判定一致。
    """
    rows = await db.fetch(
        f"""
        SELECT {_LBL_PLACE} AS label,
               g.id   AS geofence_id,
               a.city AS city,
               AVG(COALESCE(g.latitude,  a.latitude))::float  AS lat,
               AVG(COALESCE(g.longitude, a.longitude))::float AS lon,
               COUNT(*) AS visits,
               COALESCE(SUM(GREATEST(LEAST(EXTRACT(EPOCH FROM (nxt - d.end_date)) / 60,
                                           7 * 24 * 60), 0))
                        FILTER (WHERE nxt IS NOT NULL
                                  AND COALESCE(nxt_km - d.end_km, 0) <= 0.5), 0) AS dwell_min,
               MAX(d.end_date) AS last_at
        FROM (
            SELECT d.*,
                   LEAD(d.start_date) OVER w AS nxt,
                   LEAD(d.start_km)   OVER w AS nxt_km
            FROM drives d WHERE d.car_id = $1
            WINDOW w AS (ORDER BY d.start_date)
        ) d
        LEFT JOIN geofences g ON g.id = d.end_geofence_id
        LEFT JOIN addresses a ON a.id = d.end_address_id
        WHERE COALESCE(g.name, a.display_name) IS NOT NULL
        GROUP BY 1, 2, 3
        ORDER BY visits DESC
        LIMIT $2
        """,  # noqa: S608 - 仅插入模块常量 _LBL_PLACE；car_id/limit 走绑定参数
        car_id, int(limit),
    )
    return [
        {
            "label": r["label"],
            "geofence_id": r["geofence_id"],
            "city": r["city"],
            "lat": r["lat"],
            "lon": r["lon"],
            "visits": r["visits"],
            "dwell_min": round(float(r["dwell_min"] or 0)),
            "last": _iso(r["last_at"]),
        }
        for r in rows
    ]


async def statistics(car_id: int, bucket: str = "month", limit: int = 36) -> dict:
    """周期汇总：每周/每月/每年的里程、时长、能耗、充电量、费用。

    行程和充电必须在**同一条查询里**分桶。之前是两条查询各自 ORDER BY … LIMIT，
    于是两边取到的周期集合不一样：实测 bucket=day、limit=5 时，drives 拿到
    08-21/19/16/14/10，charges 拿到 08-22/20/19/15/11，合并出 8 个周期，
    而 08-10 那行显示「0 次充电」——那天库里其实有两次充电，只是被 charges
    那条的 LIMIT 切掉了。用户看到的是一个凭空捏造的零。
    """
    if bucket not in _BUCKETS:
        bucket = "month"
    tz = local_tz()

    rows = await db.fetch(
        f"""
        WITH ev AS (
            SELECT date_trunc('{bucket}', start_date AT TIME ZONE 'UTC' AT TIME ZONE $2) AS b,
                   1 AS n_drive, 0 AS n_charge,
                   COALESCE(distance, 0)::numeric AS km,
                   COALESCE(duration_min, 0) AS minutes,
                   GREATEST(start_rated_range_km - end_rated_range_km, 0) AS range_used,
                   speed_max,
                   0::numeric AS kwh, 0::numeric AS cost
            FROM drives WHERE car_id = $1
            UNION ALL
            SELECT date_trunc('{bucket}', start_date AT TIME ZONE 'UTC' AT TIME ZONE $2),
                   0, 1, 0, 0, 0, NULL,
                   COALESCE(charge_energy_added, 0), COALESCE(cost, 0)
            FROM charging_processes WHERE car_id = $1
        )
        SELECT b,
               SUM(n_drive)  AS drives,
               SUM(n_charge) AS charges,
               SUM(km)       AS km,
               SUM(minutes)  AS minutes,
               SUM(range_used) AS range_used,
               MAX(speed_max)  AS speed_max,
               SUM(kwh)  AS kwh,
               SUM(cost) AS cost
        FROM ev GROUP BY b ORDER BY b DESC LIMIT {int(limit)}
        """,  # noqa: S608 - bucket 已过 _BUCKETS 白名单；limit 经 int() 强转
        car_id, tz,
    )

    eff = await db.fetchval("SELECT efficiency FROM cars WHERE id = $1", car_id)
    eff = float(eff) if eff else None

    periods = []
    for r in rows:
        km = round(float(r["km"]), 1)
        range_used = float(r["range_used"])
        periods.append({
            "period": r["b"].date().isoformat(),
            "drives": int(r["drives"]),
            "km": km,
            "minutes": int(r["minutes"]),
            "speed_max": r["speed_max"],
            # 消耗的电量用 TeslaMate 自己的口径：额定续航差 × 车辆效率因子
            "kwh_used": round(range_used * eff, 2) if eff else None,
            "wh_per_km": round(range_used * eff * 1000 / km, 1) if eff and km > 0 else None,
            "charges": int(r["charges"]),
            "kwh_added": round(float(r["kwh"]), 2),
            "cost": round(float(r["cost"]), 2),
        })

    return {"bucket": bucket, "tz": tz, "periods": periods}


async def states_breakdown(car_id: int, days: int = 30) -> dict:
    """在线 / 睡眠 / 离线的时间占比，以及睡眠质量。

    TeslaMate 的 states 表只有这三种状态，driving/charging 都归在 online 里，
    所以这里额外扣掉行程与充电的时长，得到"醒着但没在干活"的时间——
    那才是真正该睡而没睡的部分。
    """
    # 与窗口有交集的状态段全都要算，并且**按窗口边界截断**。
    # 原来的 `start_date >= 窗口起点` 会把跨越起点的那一条整条丢掉：
    # 实测 days=10 时 asleep 少了 363 分钟，睡眠评分从 27% 掉到 23%。
    # 极端情形更离谱 —— 车连续休眠 40 天、查最近 30 天，那一条 asleep 的起点
    # 在窗口外，于是 asleep_min 为 0，界面告诉用户"这个月车一次都没睡"。
    rows = await db.fetch(
        """
        SELECT state,
               SUM(EXTRACT(EPOCH FROM (
                     LEAST(COALESCE(end_date, now()), now())
                   - GREATEST(start_date, now() - ($2::int * INTERVAL '1 day'))
               )) / 60) AS minutes,
               COUNT(*) AS n
        FROM states
        WHERE car_id = $1
          AND COALESCE(end_date, now()) >= now() - ($2::int * INTERVAL '1 day')
        GROUP BY state
        """,
        car_id, int(days),
    )
    busy = await db.fetchrow(
        """
        SELECT
          (SELECT COALESCE(SUM(duration_min), 0) FROM drives
           WHERE car_id = $1 AND start_date >= now() - ($2::int * INTERVAL '1 day')) AS drive_min,
          (SELECT COALESCE(SUM(duration_min), 0) FROM charging_processes
           WHERE car_id = $1 AND start_date >= now() - ($2::int * INTERVAL '1 day')) AS charge_min
        """,
        car_id, int(days),
    )

    by = {r["state"]: {"minutes": round(float(r["minutes"] or 0)), "sessions": r["n"]} for r in rows}
    online = by.get("online", {}).get("minutes", 0)
    asleep = by.get("asleep", {}).get("minutes", 0)
    offline = by.get("offline", {}).get("minutes", 0)
    drive_min = int(busy["drive_min"] or 0)
    charge_min = int(busy["charge_min"] or 0)
    idle_awake = max(online - drive_min - charge_min, 0)

    # 睡眠评分 = 真正睡着的时间 ÷ 本来就该睡的时间（睡着 + 醒着但没事干）。
    # 离线不计入分母：车在没信号的地库里，既不是 TeslaMate 的功劳也不是它的锅。
    sleepable = asleep + idle_awake
    return {
        "days": days,
        "online_min": online,
        "asleep_min": asleep,
        "offline_min": offline,
        "drive_min": drive_min,
        "charge_min": charge_min,
        "idle_awake_min": idle_awake,
        "sleep_score": round(asleep / sleepable * 100) if sleepable > 0 else None,
        "sessions": {k: v["sessions"] for k, v in by.items()},
    }


async def drive_detail(drive_id: int) -> dict:
    """单次行程的完整档案：元数据 + 起终点 + 逐点序列由 drive_track 提供。"""
    r = await db.fetchrow(
        f"""
        SELECT d.id, d.car_id, d.start_date, d.end_date, d.distance, d.duration_min,
               d.speed_max, d.power_max, d.power_min, d.ascent, d.descent,
               d.outside_temp_avg, d.inside_temp_avg,
               d.start_km, d.end_km,
               d.start_rated_range_km, d.end_rated_range_km,
               COALESCE(ps.usable_battery_level, ps.battery_level) AS start_soc,
               COALESCE(pe.usable_battery_level, pe.battery_level) AS end_soc,
               {_LBL_FROM} AS from_label,
               {_LBL_TO} AS to_label,
               c.efficiency
        FROM drives d
        LEFT JOIN positions ps ON ps.id = d.start_position_id
        LEFT JOIN positions pe ON pe.id = d.end_position_id
        LEFT JOIN geofences sg ON sg.id = d.start_geofence_id
        LEFT JOIN geofences eg ON eg.id = d.end_geofence_id
        LEFT JOIN addresses sa ON sa.id = d.start_address_id
        LEFT JOIN addresses ea ON ea.id = d.end_address_id
        LEFT JOIN cars c ON c.id = d.car_id
        WHERE d.id = $1
        """,  # noqa: S608 - 仅插入模块常量 _LBL_FROM/_LBL_TO；drive_id 走 $1
        drive_id,
    )
    if r is None:
        return {}

    def num(v):
        return float(v) if v is not None else None

    dist = num(r["distance"]) or 0
    range_used = max((num(r["start_rated_range_km"]) or 0) - (num(r["end_rated_range_km"]) or 0), 0)
    eff = num(r["efficiency"])
    kwh = round(range_used * eff, 2) if eff else None

    return {
        "id": r["id"],
        "car_id": r["car_id"],
        "start": _iso(r["start_date"]),
        "end": _iso(r["end_date"]),
        "duration_min": r["duration_min"],
        "distance": round(dist, 2),
        "speed_max": r["speed_max"],
        "speed_avg": round(dist / (r["duration_min"] / 60), 1)
        if r["duration_min"] and r["duration_min"] > 0 else None,
        "power_max": r["power_max"],
        "power_min": r["power_min"],
        "ascent": r["ascent"],
        "descent": r["descent"],
        "outside_temp": num(r["outside_temp_avg"]),
        "inside_temp": num(r["inside_temp_avg"]),
        "odometer_start": num(r["start_km"]),
        "odometer_end": num(r["end_km"]),
        "start_soc": r["start_soc"],
        "end_soc": r["end_soc"],
        "range_used": round(range_used, 1),
        "kwh_used": kwh,
        "wh_per_km": round(kwh * 1000 / dist, 1) if kwh and dist > 0 else None,
        "from": r["from_label"] or None,
        "to": r["to_label"] or None,
    }


async def unlogged_km(car_id: int) -> dict:
    """未记录里程：里程表总增量减去所有行程距离之和。

    差值来自 TeslaMate 没跑起来、或者车在没网的地方开过。这个数字是数据
    完整性的直接体现，也是"我的统计准不准"这个问题的答案。
    """
    r = await db.fetchrow(
        """
        SELECT MIN(start_km) AS first_km, MAX(end_km) AS last_km,
               COALESCE(SUM(distance), 0) AS logged
        FROM drives WHERE car_id = $1 AND start_km IS NOT NULL AND end_km IS NOT NULL
        """,
        car_id,
    )
    if r is None or r["first_km"] is None:
        return {"span_km": None, "logged_km": None, "unlogged_km": None, "coverage": None}
    span = float(r["last_km"]) - float(r["first_km"])
    logged = float(r["logged"])
    return {
        "span_km": round(span, 1),
        "logged_km": round(logged, 1),
        "unlogged_km": round(max(span - logged, 0), 1),
        "coverage": round(logged / span * 100, 1) if span > 0 else None,
    }
