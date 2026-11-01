import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..i18n.prefs import get as get_prefs
from ..i18n.router import t
from ..livestate import live
from . import notifier, webpush
from . import sentry as sentry_mod
from .engine import RULES, engine
from .sentry import tracker as sentry_tracker
from .store import store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("/history")
async def history(limit: int = 100):
    # 两头都夹：SQLite 的 LIMIT -1 表示"不限"，只夹上界的话 limit=-5
    # 会把整张表倒出来（和 /api/dashcam/events 是同一个坑）。
    return await store.recent_alerts(limit=max(1, min(limit, 500)))


@router.get("/rules")
async def rules():
    modes = await engine.rule_modes()
    return [
        {
            "id": rule.id,
            "level": rule.level,
            "title_zh": rule.title_zh,
            "title_en": rule.title_en,
            "mode": modes[rule.id],
            "default_mode": rule.mode,
            "enabled": modes[rule.id] != "off",
        }
        for rule in RULES.values()
    ]


class RuleUpdate(BaseModel):
    enabled: bool | None = None
    mode: str | None = None          # push | app | off


@router.post("/rules/{rule_id}")
async def update_rule(rule_id: str, body: RuleUpdate):
    if rule_id not in RULES:
        return {"ok": False, "error": "unknown rule"}
    try:
        if body.mode is not None:
            await engine.set_mode(rule_id, body.mode)
        elif body.enabled is not None:
            await engine.set_rule(rule_id, body.enabled)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True, "mode": (await engine.rule_modes())[rule_id]}


@router.get("/maintenance")
async def maintenance_status():
    """保养项目与到期情况。里程取实时状态里的 odometer。"""
    from . import maintenance

    car_id = next((k for k in live.cars if k.isdigit()), None)
    odo = None
    if car_id:
        try:
            odo = float(live.cars[car_id].get("odometer") or 0) or None
        except (TypeError, ValueError):
            odo = None
    return {"odometer_km": odo, "items": await maintenance.status(odo)}


class MaintenanceItems(BaseModel):
    items: list[dict]


@router.post("/maintenance")
async def set_maintenance(body: MaintenanceItems):
    from . import maintenance

    return {"items": await maintenance.set_items(body.items)}


@router.post("/maintenance/{item_id}/done")
async def maintenance_done(item_id: str):
    """做完了：把基准推到当前里程与日期。"""
    from . import maintenance

    car_id = next((k for k in live.cars if k.isdigit()), None)
    odo = None
    if car_id:
        try:
            odo = float(live.cars[car_id].get("odometer") or 0) or None
        except (TypeError, ValueError):
            odo = None
    items = await maintenance.mark_done(item_id, odo)
    # 允许下一轮到期时重新提醒
    notified = set(await store.get_setting("maintenance_notified", []) or [])
    notified.discard(item_id)
    await store.set_setting("maintenance_notified", sorted(notified))
    return {"items": items}


class SpeedLimit(BaseModel):
    kmh: float | None = None


@router.get("/speed-limit")
async def get_speed_limit():
    """当前阈值。界面要能读回来 —— 只写不读的话，用户刷新后看到空输入框，
    再点一次保存就把"空"当成关闭写回去了。"""
    return {"kmh": await store.get_setting("speed_limit_kmh")}


@router.post("/speed-limit")
async def set_speed_limit(body: SpeedLimit):
    """无人在车时的车速告警阈值。留空 = 关闭。"""
    await store.set_setting("speed_limit_kmh", body.kmh if body.kmh and body.kmh > 0 else None)
    return {"kmh": await store.get_setting("speed_limit_kmh")}


class SnoozeBody(BaseModel):
    minutes: int = 0
    scope: str = "noncritical"      # noncritical | all


@router.get("/snooze")
async def get_snooze():
    return await engine.snooze_state()


@router.post("/snooze")
async def set_snooze(body: SnoozeBody):
    """临时静音。默认只静音非严重级 —— 「今晚别烦我」不等于
    「今晚别管我的车被撬」。要全静音必须显式选 all。"""
    return await engine.set_snooze(body.minutes, body.scope)


@router.get("/channels")
async def channels():
    return {"channels": await notifier.configured_channels(),
            "email_problem": await notifier.email_config_problem()}


@router.get("/channels/config")
async def channels_config():
    """Every channel, whether it can send, and what's missing — never secrets."""
    from . import channels as chan

    return {"channels": await chan.describe(), "webpush_devices": await webpush.count()}


class ChannelBody(BaseModel):
    values: dict


@router.put("/channels/{name}")
async def channel_save(name: str, body: ChannelBody):
    from . import channels as chan

    try:
        await chan.save(name, body.values)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"channels": await chan.describe()}


class EnabledBody(BaseModel):
    enabled: bool


@router.post("/channels/{name}/enabled")
async def channel_enabled(name: str, body: EnabledBody):
    from . import channels as chan

    if name not in chan.SCHEMA:
        raise HTTPException(404, "unknown channel")
    await chan.set_enabled(name, body.enabled)
    return {"channels": await chan.describe()}


@router.delete("/channels/{name}")
async def channel_reset(name: str):
    from . import channels as chan

    await chan.reset(name)
    return {"channels": await chan.describe()}


@router.post("/channels/ntfy/new-topic")
async def ntfy_new_topic():
    """Create an unguessable private topic and switch ntfy to it."""
    from . import channels as chan

    topic = chan.new_ntfy_topic()
    cur = (await chan.config())["ntfy"]
    await chan.save("ntfy", {"url": cur.get("url") or "https://ntfy.sh", "topic": topic})
    base = (cur.get("url") or "https://ntfy.sh").rstrip("/")
    return {"topic": topic, "subscribe_url": f"{base}/{topic}",
            "app_url": f"ntfy://{base.split('://', 1)[-1]}/{topic}"}


@router.post("/channels/{name}/test")
async def channel_test(name: str):
    """Send a test to one channel. For ntfy, read the topic back to prove the
    server really has it — `200 OK` from a send only proves we sent it."""
    import secrets as _secrets

    from . import channels as chan

    if name != "webpush" and name not in chan.SCHEMA:
        raise HTTPException(404, "unknown channel")
    loc = (await get_prefs())["locale"]
    marker = _secrets.token_hex(4)
    title = t("push.test", loc)
    body = t("push.test_body", loc, car="OhmyTesla") + f" · #{marker}"
    ok, delivered = await notifier.send(title, body, payload={"rule": "test", "level": "info"},
                                        only=[name])
    verified = None
    if ok and name == "ntfy":
        verified = await notifier.verify_ntfy(marker)
    return {"ok": ok, "delivered": delivered, "verified": verified}


@router.post("/history/clear")
async def clear_history():
    return {"ok": True, "removed": await store.clear_alerts()}


# ---------------- 哨兵活动会话 ----------------

@router.get("/sentry/sessions")
async def sentry_sessions(limit: int = 100, car_id: str | None = None, before: float | None = None):
    """哨兵会话，附带对应的行车记录仪片段。

    「有人靠近你的车」和「看那段录像」本来就是同一件事，之前拆在两个页面
    上，用户得自己对时间戳。这里在服务端接起来。`before` pages backwards
    (pass the oldest `started` you have).
    """
    rows = await sentry_tracker.recent(max(1, min(limit, 500)), before=before, car_id=car_id)
    return await _attach_clips(rows)


@router.get("/sentry/active")
async def sentry_active():
    """Incidents happening right now (for the nav badge and the live banner)."""
    return {"active": sentry_tracker.active()}


@router.get("/sentry/config")
async def sentry_config():
    from .fastpoll import poller

    return {"home": await sentry_tracker.home(), "fast_poll_seconds": await poller.interval(),
            "fast_poll": poller.status(), "policies": await sentry_tracker.policies(),
            "fleet": {cid: sentry_tracker.has_fleet(cid) for cid in live.cars if cid.isdigit()}}


class HomeBody(BaseModel):
    lat: float | None = None
    lon: float | None = None
    radius_m: float = 150
    label: str = ""
    from_car: str | None = None     # use this car's current position


@router.post("/sentry/home")
async def sentry_set_home(body: HomeBody):
    lat, lon = body.lat, body.lon
    if body.from_car:
        car = live.cars.get(body.from_car, {})
        try:
            lat, lon = float(car["latitude"]), float(car["longitude"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(409, "that car's position isn't known yet") from None
    if lat is None or lon is None:
        raise HTTPException(400, "lat/lon or from_car required")
    return await sentry_tracker.set_home(lat, lon, max(50, min(body.radius_m, 2000)), body.label)


@router.delete("/sentry/home")
async def sentry_clear_home():
    await store.set_setting("home_location", None)
    return {"ok": True}


class FastPollBody(BaseModel):
    seconds: int


@router.post("/sentry/fast-poll")
async def sentry_fast_poll(body: FastPollBody):
    """0 = off. Never below 5 s (Tesla rate limits), never above 120 s."""
    v = 0 if body.seconds <= 0 else max(5, min(body.seconds, 120))
    await store.set_setting("sentry_fast_poll_seconds", v)
    from .fastpoll import poller

    poller.invalidate()
    return {"fast_poll_seconds": v}


async def _attach_clips(rows: list[dict]) -> list[dict]:
    from datetime import datetime, timedelta, timezone

    try:
        from ..dashcam import telemetry
        from ..dashcam.scanner import scanner

        events = [e for e in await scanner.ascan() if e.type == "sentry"]
        if not events:
            return rows
        tz = telemetry.local_tz()
    except Exception:  # noqa: BLE001 - 没有素材目录时照常返回会话
        return rows

    for r in rows:
        try:
            offset = await telemetry.clock_offset(int(r["car_id"]))
        except Exception:  # noqa: BLE001
            offset = 0.0
        # 会话时间是 UTC 秒；录像文件名走车机本地时区，还要扣掉时钟漂移
        started = (
            datetime.fromtimestamp(r["started"], timezone.utc)
            .astimezone(tz).replace(tzinfo=None) - timedelta(seconds=offset)
        )
        best, best_gap = None, None
        for ev in events:
            # 事件区间内直接命中；否则取最近的一个，但不超过 5 分钟
            if ev.start <= started <= ev.end:
                gap = 0.0
            else:
                gap = min(abs((started - ev.start).total_seconds()),
                          abs((started - ev.end).total_seconds()))
                if gap > 300:
                    continue
            if best_gap is None or gap < best_gap:
                best, best_gap = ev, gap
        if best is None:
            continue
        seek = max(0.0, (started - best.start).total_seconds())
        at = min(max(started, best.start), best.end)
        r["clip"] = {
            "event_id": best.id,
            "thumb": best.thumb,
            # `at` (the car's local wall clock, as in the file names) is what the
            # player maps onto its timeline, gaps and short segments included.
            "at": at.isoformat(timespec="seconds"),
            "seek": round(min(seek, max(0.0, (best.end - best.start).total_seconds()))),
            "exact": best_gap == 0.0,
        }
    return rows


@router.get("/sentry/policies")
async def sentry_policies():
    geofences: list[str] = []
    try:
        from .. import db

        rows = await db.fetch("SELECT name FROM geofences ORDER BY name")
        geofences = [r["name"] for r in rows]
    except Exception:
        log.debug("suppressed", exc_info=True)
    return {
        "policies": await sentry_tracker.policies(),
        "geofences": geofences,
        "choices": sentry_mod.POLICY_CHOICES,
        "default": sentry_mod.DEFAULT_POLICY,
        "dedup_seconds": settings.sentry_dedup_seconds,
        "escalate_seconds": settings.sentry_escalate_seconds,
        "polled_enabled": settings.sentry_polled_alerts,
    }


class PolicyBody(BaseModel):
    geofence: str
    policy: str


@router.post("/sentry/policies")
async def set_sentry_policy(body: PolicyBody):
    if body.policy not in sentry_mod.POLICY_CHOICES:
        raise HTTPException(400, "unknown policy")
    await sentry_tracker.set_policy(body.geofence, body.policy)
    return {"ok": True}


# ---------------- Web Push（PWA 推送）----------------

@router.get("/push/key")
async def push_key():
    return {"public_key": await webpush.public_key(), "devices": await webpush.count()}


@router.post("/push/subscribe")
async def push_subscribe(sub: dict):
    try:
        await webpush.subscribe(sub)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "devices": await webpush.count()}


class Endpoint(BaseModel):
    endpoint: str


@router.post("/push/unsubscribe")
async def push_unsubscribe(body: Endpoint):
    await webpush.unsubscribe(body.endpoint)
    return {"ok": True, "devices": await webpush.count()}


@router.post("/test")
async def test_notification():
    cars = live.snapshot()
    car_name = next(
        (v.get("display_name") for v in cars.values() if v.get("display_name")),
        "OhmyTesla",
    )
    loc = (await get_prefs())["locale"]
    ok, chans = await notifier.send(
        t("push.test", loc), t("push.test_body", loc, car=car_name),
        payload={"rule": "test", "level": "info"}, url="/security",
    )
    return {"ok": ok, "channels": chans}
