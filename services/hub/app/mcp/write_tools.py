"""MCP 写操作工具：让模型不只是查数据，还能做事。

调研里这是全开源生态唯一的空白：13 个 Tesla/TeslaMate 的 MCP 仓库全是只读，
商业侧只有 mytesla.io 做了 AI 控车（按 credit 计费，唤醒车贵 20 倍作隐性刹车）。

但"能控车的 AI"必须先解决一个问题：**模型会犯错，而车门是真的会开的。**
所以这里的护栏不是加分项，是准入条件：

1. **两段式确认。** 高风险动作第一次调用只返回一个确认票据和一句人话描述，
   什么都不做；模型必须把票据原样送回来才真的执行。票据 90 秒过期、一次性。
   这一步把"模型幻觉出一个 door_unlock"和"用户真的想开门"分开了。
2. **默认全关。** MCP_WRITE=false 时所有写工具连列表都不出现 —— 模型看不见
   就不会去试，比看得见然后被拒更省事，也不会诱导它反复重试。
3. **动作白名单 + 全量流水。** 能做什么是枚举出来的，不是"除了危险的都行"。
   每一次调用都进 command_log，事后可回溯。
4. **不碰 TeslaMate 的库。** 写只发生在 OhmyTesla 自己的 SQLite 和车辆指令
   通道上。改地理围栏、改车辆设置这类要写 TeslaMate 表的操作不在这里开口子。
"""

import json
import logging
import secrets
import time

from ..alerts.store import store
from ..config import settings

log = logging.getLogger(__name__)

# 确认票据的有效期。够模型答一轮，不够留到下一次对话。
TICKET_TTL_S = 90

# 需要二次确认的动作。判据是"做错了要付出真实代价"：
# 开锁开厢是物理安全，改充电限值和哨兵会影响电池与看护。
# 由指令表的风险等级决定，和界面、自动化同一个来源（tesla/catalog.py）。
# 以前是手写的列表，漏了 boombox 之类后来补进来的高危指令。
from ..tesla import catalog as _catalog  # noqa: E402

NEEDS_CONFIRM = frozenset(c.id for c in _catalog.COMMANDS.values() if c.risk >= 1)

# 完全不允许模型碰的。wake_up 单独列出来不是因为危险，是因为它按次计费
# 且会真的把车叫醒 —— 模型"顺手唤醒看一眼"的成本用户承担不起。
FORBIDDEN = frozenset({"wake_up"} | {c.id for c in _catalog.COMMANDS.values() if c.risk >= 2})

_tickets: dict[str, tuple[float, str, dict]] = {}


def enabled() -> bool:
    return bool(getattr(settings, "mcp_write", False))


def _prune() -> None:
    now = time.time()
    for k in [k for k, (exp, _, _) in _tickets.items() if exp < now]:
        _tickets.pop(k, None)


def _describe(command: str, params: dict) -> str:
    """给模型（最终给用户）看的一句人话。确认票据的意义全在这句话上。"""
    p = params or {}
    table = {
        "door_unlock": "解锁车门",
        "door_lock": "锁车",
        "trunk": "打开/关闭后备箱",
        "frunk": "打开前备箱",
        "sentry_on": "开启哨兵模式",
        "sentry_off": "**关闭哨兵模式**（关闭期间无人看护）",
        "climate_on": "打开空调",
        "climate_off": "关闭空调",
        "charge_start": "开始充电",
        "charge_stop": "停止充电",
        "set_charge_limit": f"把充电上限改成 {p.get('percent', '?')}%",
        "set_temps": f"把温度设为 {p.get('driver_temp', '?')}°C",
        "vent_windows": "通风打开车窗",
        "close_windows": "关闭车窗",
        "flash_lights": "闪灯",
        "honk_horn": "鸣笛",
        "charge_port_open": "打开充电口",
        "charge_port_close": "关闭充电口",
    }
    return table.get(command, command)


# ---------------------------------------------------------------- 工具实现

async def t_send_command(command: str, car_id: int | None = None,
                         params: dict | None = None, confirm_token: str = "") -> dict:
    """下发车辆指令。高风险动作要两段式确认。"""
    from ..automation.engine import RISKY_COMMANDS, SAFE_COMMANDS
    from ..livestate import live
    from ..tesla import commands as cmd

    if not enabled():
        return {"error": "写操作未开启。在 .env 里设 MCP_WRITE=true 并重启。"}
    if command in FORBIDDEN:
        return {"error": f"{command} 不开放给 AI：唤醒和最高风险的指令（如远程启动）只能在网页上手动操作"}
    if command not in SAFE_COMMANDS and command not in RISKY_COMMANDS:
        return {"error": f"未知或不允许的指令：{command}"}

    # 默认车 = 最近活跃且没归档的那辆（live.cars 的第一个键在多车账号上可能是
    # 一辆已经不在账号里的车）。确认语和票据里都带上车名，同意不能挪到另一辆车上用。
    from ..insights import cars as registry

    try:
        if car_id is None:
            car_id = await registry.default_car_id()
        meta = await registry.get_car(int(car_id)) if car_id is not None else None
    except Exception:  # noqa: BLE001 — DB down: fall back to what's streaming live
        if car_id is None:
            car_id = next((int(k) for k in live.cars if k.isdigit()), None)
        meta = {"id": car_id, "label": f"car {car_id}", "archived": False} if car_id is not None else None
    if car_id is None:
        return {"error": "没有可用的车辆"}
    if meta is None:
        return {"error": f"没有 id 为 {car_id} 的车"}
    if meta.get("archived"):
        return {"error": f"{meta['label']} 已归档（不在账号里或很久没有数据），不能下发指令"}
    label = meta.get("label") or f"car {car_id}"

    params = params or {}
    _prune()

    if command in NEEDS_CONFIRM:
        if not confirm_token:
            token = secrets.token_urlsafe(9)
            _tickets[token] = (time.time() + TICKET_TTL_S, command,
                               {"car_id": car_id, "car": label, **params})
            return {
                "needs_confirmation": True,
                "confirm_token": token,
                "expires_in_s": TICKET_TTL_S,
                "car": label,
                "what_will_happen": f"{label}：{_describe(command, params)}",
                "next_step": "把 confirm_token 原样传回来才会真的执行。"
                             "先向用户复述 what_will_happen 并得到同意。",
            }
        got = _tickets.pop(confirm_token, None)
        if got is None:
            return {"error": "确认票据无效或已过期，请重新发起（票据一次性、90 秒有效）"}
        exp, want_cmd, want_args = got
        if time.time() > exp:
            return {"error": "确认票据已过期"}
        # 票据和这次调用必须完全一致，否则等于拿"开空调"的同意去开门
        if want_cmd != command or want_args != {"car_id": car_id, "car": label, **params}:
            return {"error": "确认票据与本次请求不匹配"}

    t0 = time.time()
    try:
        # The AI never wakes the car (FORBIDDEN bans wake_up; an implicit
        # wake inside any command would undo that).
        res = await cmd.send(int(car_id), command, params, allow_wake=False)
        ok = bool(res.get("ok"))
        err = "" if ok else str(res.get("error") or res)[:300]
    except Exception as e:  # noqa: BLE001
        ok, err, res = False, str(e)[:300], {}
    await store.log_command(str(car_id), command, params, "mcp",
                            ok, err, int((time.time() - t0) * 1000))
    log.info("mcp command %s on car %s -> %s", command, car_id, ok)
    return {"ok": ok, "command": command, "car_id": car_id, "car": label,
            "did": f"{label}：{_describe(command, params)}", "error": err or None}


async def t_tag_drive(drive_id: int, category: str = "", note: str = "") -> dict:
    """给一段行程打分类/备注。写在 OhmyTesla 自己的库里，不碰 TeslaMate。"""
    if not enabled():
        return {"error": "写操作未开启。在 .env 里设 MCP_WRITE=true 并重启。"}
    from ..trips.router import CATEGORIES

    cat = (category or "").strip().lower()
    if cat and cat not in CATEGORIES:
        return {"error": f"category 必须是 {sorted(CATEGORIES)} 之一"}

    key = str(drive_id)

    def apply(tags):
        tags = dict(tags or {})
        if not cat and not note.strip():
            tags.pop(key, None)
            return tags
        entry = dict(tags.get(key) or {})
        if category:
            entry["category"] = cat
        if note:
            entry["note"] = note.strip()[:200]
        tags[key] = entry
        return tags

    tags = await store.mutate_setting("drive_tags", apply, {})
    return {"ok": True, "drive_id": drive_id, "tag": tags.get(key)}


async def t_set_automation(rule: dict, enable: bool | None = None) -> dict:
    """新建或更新一条自动化规则。

    **新规则一律停用**，哪怕调用方传了 enabled=true —— 让模型直接创建一条
    生效的控车规则，风险和让它直接控车是一样的，而且更隐蔽（用户不在场时才
    触发）。用户要在界面上看过一遍再自己打开。
    """
    if not enabled():
        return {"error": "写操作未开启。在 .env 里设 MCP_WRITE=true 并重启。"}
    from ..automation.engine import engine as auto

    r = dict(rule or {})
    if enable is True:
        return {"error": "MCP 不能直接启用规则。先创建（默认停用），"
                         "让用户在设置页确认后自己打开。"}
    r["enabled"] = False
    try:
        rules = await auto.upsert(r)
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": True, "created": r.get("id"), "enabled": False,
            "note": "规则已保存但处于停用状态，需要用户在设置页手动启用。",
            "total_rules": len(rules)}


async def t_snooze_alerts(minutes: int, scope: str = "noncritical") -> dict:
    """临时静音告警。上限 24 小时——静音一周和关掉告警没区别。"""
    if not enabled():
        return {"error": "写操作未开启。在 .env 里设 MCP_WRITE=true 并重启。"}
    from ..alerts.engine import engine as alert_engine

    m = max(0, min(int(minutes), 24 * 60))
    if scope not in ("noncritical", "all"):
        return {"error": "scope 必须是 noncritical 或 all"}
    # 连同报警一起静音，效果等于替车主关掉哨兵推送 —— 那件事在 AI 这边要两段式确认，
    # 这件事更不该一句话就做成。只在网页上由车主自己操作。
    if scope == "all" and m > 0:
        return {"error": "AI 只能静音非紧急告警（scope=noncritical）。连警报一起静音请在网页「安全」页里操作。"}
    state = await alert_engine.set_snooze(m, scope)
    return {"ok": True, "minutes": m, "scope": scope, "state": state}


# ---------------------------------------------------------------- 注册

def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


WRITE_TOOLS: dict[str, tuple] = {
    "send_vehicle_command": (
        t_send_command,
        "给车辆下发指令（空调、充电、锁车、哨兵等）。"
        "解锁车门、开后备箱、关哨兵这类高风险动作会先返回 confirm_token —— "
        "必须先把 what_will_happen 复述给用户、得到明确同意，再带着 token 调一次。",
        _schema({
            "command": {"type": "string", "description": "指令名，见 list_commands"},
            "car_id": {"type": "integer"},
            "params": {"type": "object", "description": "指令参数，如 {\"percent\": 80}"},
            "confirm_token": {"type": "string", "description": "二次确认票据"},
        }, ["command"]),
    ),
    "tag_drive": (
        t_tag_drive,
        "给一段行程打分类（business / personal / commute…）或写备注。"
        "分类留空且备注留空 = 取消标记。",
        _schema({"drive_id": {"type": "integer"},
                 "category": {"type": "string"},
                 "note": {"type": "string"}}, ["drive_id"]),
    ),
    "create_automation": (
        t_set_automation,
        "创建或更新一条自动化规则（触发 → 动作）。创建出来一律是停用状态，"
        "需要用户在设置页手动启用。",
        _schema({"rule": {"type": "object"}}, ["rule"]),
    ),
    "snooze_alerts": (
        t_snooze_alerts,
        "临时静音非紧急告警，最多 24 小时；警报、入侵这类严重告警照常推送。"
        "连严重告警一起静音只能车主在网页上操作。minutes=0 取消静音。",
        _schema({"minutes": {"type": "integer"},
                 "scope": {"type": "string", "enum": ["noncritical"]}}, ["minutes"]),
    ),
}


def available() -> dict:
    """写工具只在开关打开时出现在工具列表里。

    看得见然后被拒，会让模型反复重试、把失败当成参数问题去猜；
    看不见就不会去试。
    """
    return WRITE_TOOLS if enabled() else {}


def describe_guardrails() -> str:
    return json.dumps({
        "write_enabled": enabled(),
        "needs_confirmation": sorted(NEEDS_CONFIRM),
        "forbidden": sorted(FORBIDDEN),
        "ticket_ttl_seconds": TICKET_TTL_S,
    }, ensure_ascii=False)
