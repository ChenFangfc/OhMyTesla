from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..i18n.prefs import get as get_prefs
from ..i18n.router import t
from ..livestate import live
from . import notifier
from . import sentry as sentry_mod
from . import webpush
from .engine import RULES, engine
from .sentry import tracker as sentry_tracker
from .store import store

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("/history")
async def history(limit: int = 100):
    return await store.recent_alerts(limit=min(limit, 500))


@router.get("/rules")
async def rules():
    states = await engine.rule_states()
    return [
        {
            "id": rule.id,
            "level": rule.level,
            "title_zh": rule.title_zh,
            "title_en": rule.title_en,
            "enabled": states.get(rule.id, rule.default_enabled),
        }
        for rule in RULES.values()
    ]


class RuleUpdate(BaseModel):
    enabled: bool


@router.post("/rules/{rule_id}")
async def update_rule(rule_id: str, body: RuleUpdate):
    if rule_id not in RULES:
        return {"ok": False, "error": "unknown rule"}
    await engine.set_rule(rule_id, body.enabled)
    return {"ok": True}


@router.get("/channels")
async def channels():
    return {"channels": await notifier.configured_channels(),
            "email_problem": notifier.email_config_problem()}


@router.post("/history/clear")
async def clear_history():
    return {"ok": True, "removed": await store.clear_alerts()}


# ---------------- 哨兵活动会话 ----------------

@router.get("/sentry/sessions")
async def sentry_sessions(limit: int = 100):
    """哨兵会话，附带对应的行车记录仪片段。

    「有人靠近你的车」和「看那段录像」本来就是同一件事，之前拆在两个页面
    上，用户得自己对时间戳。这里在服务端接起来。
    """
    rows = await sentry_tracker.recent(min(limit, 500))
    return await _attach_clips(rows)


async def _attach_clips(rows: list[dict]) -> list[dict]:
    from datetime import datetime, timedelta, timezone

    try:
        from ..dashcam import telemetry
        from ..dashcam.scanner import scanner

        events = [e for e in scanner.scan() if e.type == "sentry"]
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
        r["clip"] = {
            "event_id": best.id,
            "thumb": best.thumb,
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
    except Exception:  # noqa: BLE001
        pass
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
        t("notify.test_title", loc),
        t("notify.test_body", loc, car=car_name),
        payload={"rule": "test", "level": "info"},
    )
    return {"ok": ok, "channels": chans}
