"""哨兵活动会话模型 —— 把零散的状态跳变收敛成"一次事件"。

为什么需要它：哨兵被触发时，无论是 Fleet Telemetry 的 Aware 状态还是
轮询看到的 center_display_state=7，都会在几十秒内反复出现。逐条推送
= 告警疲劳（有车主报告一晚上 800 多次哨兵录像），这是同类产品的头号差评。

会话规则：
- 首次活动 → 开启会话，推送一条（L1）
- 15 秒内的后续活动并入同一会话，不再推送
- 持续超过 90 秒，或 90 秒内再次触发 → 升级推送（L2，高优先级）
- Panic / 开门解锁等 → 严重（L3，穿透静默时段）
- 静默 15 秒后关闭会话

来源分级（同时可用时以可靠的为准）：
  fleet  —— Fleet Telemetry 实时流，真实状态边沿，秒级
  polled —— TeslaMate 轮询推断（30 秒一次），可能漏报
"""

import asyncio
import contextlib
import re
import json
import logging
import time
from dataclasses import dataclass, field

from ..config import settings
from ..i18n.prefs import get as get_prefs
from ..i18n.router import t
from .store import store

log = logging.getLogger(__name__)

LEVEL_INFO, LEVEL_WARN, LEVEL_CRIT = "info", "warning", "critical"

# 看起来像文案目录 key 的字符串（a.b / a.b.c），老数据里也可能是成品文案
_KEYISH = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")


@dataclass
class Session:
    car_id: str
    car_name: str
    source: str
    started: float
    last_seen: float
    samples: int = 1
    peak_state: str = "aware"
    escalated: bool = False
    geofence: str = ""
    soc: str = ""
    db_id: int | None = None
    extras: list[str] = field(default_factory=list)
    # 上一次因"入侵"推送的时刻（仅内存，不落库）。用来把同一批并发到达的
    # 解锁/开门/开后备箱收敛成一条，见 note_intrusion。
    last_intrusion_notify: float = 0.0

    @property
    def duration(self) -> float:
        return self.last_seen - self.started


# 默认地点策略；用户可在告警页按地理围栏覆盖
POLICY_CHOICES = {
    "always": "sentry.policy.always",
    "escalated": "sentry.policy.escalated",
    "log": "sentry.policy.log",
}
DEFAULT_POLICY = "always"


class SentryTracker:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._recent_end: dict[str, float] = {}   # 用于"90 秒内再次触发"判定
        self._fleet_seen: dict[str, float] = {}   # 有实时流时让轮询层闭嘴
        self._task: asyncio.Task | None = None
        self._fire = None  # 由 engine 注入，避免循环导入

    def bind(self, fire_callable) -> None:
        self._fire = fire_callable

    def start(self) -> None:
        # 会话至少静默 dedup 秒才关闭，所以"刚结束又触发"的判定窗口
        # 必须大于 dedup，否则这条升级路径永远走不到。
        if settings.sentry_escalate_seconds <= settings.sentry_dedup_seconds:
            log.warning(
                "SENTRY_ESCALATE_SECONDS (%s) 应大于 SENTRY_DEDUP_SECONDS (%s)，"
                "否则「短时间内再次触发」不会升级",
                settings.sentry_escalate_seconds, settings.sentry_dedup_seconds,
            )
        self._task = asyncio.create_task(self._reaper(), name="sentry-reaper")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ---------- 策略 ----------

    async def policies(self) -> dict:
        return await store.get_setting("sentry_policies", {}) or {}

    async def set_policy(self, geofence: str, policy: str) -> None:
        pols = await self.policies()
        if policy == DEFAULT_POLICY:
            pols.pop(geofence, None)
        else:
            pols[geofence] = policy
        await store.set_setting("sentry_policies", pols)

    async def _policy_for(self, geofence: str) -> str:
        return (await self.policies()).get(geofence or "", DEFAULT_POLICY)

    # ---------- 主入口 ----------

    def note_fleet(self, car_id: str) -> None:
        """记下该车有实时流，轮询层随后不再重复告警。"""
        self._fleet_seen[car_id] = time.time()

    def has_fleet(self, car_id: str) -> bool:
        return time.time() - self._fleet_seen.get(car_id, 0) < 900

    async def observe(
        self, car_id: str, car_name: str, source: str, state: str = "aware",
        geofence: str = "", soc: str = "", extra: str = "",
    ) -> None:
        """收到一次哨兵活动信号。state: aware / panic"""
        now = time.time()
        sess = self._sessions.get(car_id)

        if sess is None:
            repeat = now - self._recent_end.get(car_id, 0) < settings.sentry_escalate_seconds
            sess = Session(
                car_id=car_id, car_name=car_name, source=source,
                started=now, last_seen=now, geofence=geofence, soc=soc,
                peak_state=state,
            )
            if extra:
                sess.extras.append(extra)
            self._sessions[car_id] = sess
            sess.db_id = await self._persist(sess, closed=False)
            await self._notify(sess, escalated=repeat or state == "panic",
                               reason="sentry.reason.repeat" if repeat else "")
            return

        # 并入现有会话
        sess.last_seen = now
        sess.samples += 1
        if state == "panic":
            sess.peak_state = "panic"
        if geofence:
            sess.geofence = geofence
        if soc:
            sess.soc = soc
        if extra and extra not in sess.extras:
            sess.extras.append(extra)
        await self._persist(sess, closed=False)

        should_escalate = (
            not sess.escalated
            and (state == "panic" or sess.duration >= settings.sentry_escalate_seconds)
        )
        if should_escalate:
            await self._notify(sess, escalated=True,
                               reason="sentry.reason.sustained",
                               reason_vars={"n": int(sess.duration)})

    async def note_intrusion(self, car_id: str, car_name: str, what: str,
                             geofence: str = "", soc: str = "") -> None:
        """开门/解锁等强信号：并入当前会话（如果有），并强制严重级。"""
        now = time.time()
        sess = self._sessions.get(car_id)

        if sess is None:
            # 以前这里走 observe(...)，那条路**不带 force**，于是设成「只记录」
            # 的地点会把入侵告警一并吞掉 —— 而入侵是全系统最强的信号。
            # 现在两条路径一致：入侵永远推。
            sess = Session(
                car_id=car_id, car_name=car_name, source="state",
                started=now, last_seen=now, geofence=geofence, soc=soc,
                peak_state="panic",
            )
            sess.extras.append(what)
            self._sessions[car_id] = sess
            sess.db_id = await self._persist(sess, closed=False)
            sess.last_intrusion_notify = now
            await self._notify(sess, escalated=True, reason=what, force=True)
            return

        sess.peak_state = "panic"
        sess.last_seen = now
        is_new = what not in sess.extras
        if is_new:
            sess.extras.append(what)
        if geofence:
            sess.geofence = geofence
        if soc:
            sess.soc = soc
        await self._persist(sess, closed=False)

        # TeslaMate 把同一批 Summary 的各键并发发出来，车被撬开时
        # 「解锁」「开门」「开后备箱」会在同一秒内接连到达。每个都推一条的话，
        # 手机上就是几条一模一样的"车辆被闯入"（库里 130/131 两条相隔 158ms
        # 就是这么来的）。首条已经把事说清楚了，后续原因并进会话记录，
        # 界面和后续升级推送里都看得到。
        if not is_new or now - sess.last_intrusion_notify < settings.sentry_dedup_seconds:
            return
        sess.last_intrusion_notify = now
        await self._notify(sess, escalated=True, reason=what, force=True)

    # ---------- 推送 ----------

    def _level(self, sess: Session, escalated: bool) -> str:
        if sess.peak_state == "panic":
            return LEVEL_CRIT
        return LEVEL_WARN if escalated else LEVEL_INFO

    async def _notify(self, sess: Session, escalated: bool, reason: str = "",
                      reason_vars: dict | None = None, force: bool = False) -> None:
        level = self._level(sess, escalated)

        policy = await self._policy_for(sess.geofence)
        if not force and policy == "log":
            log.info("sentry activity at %s suppressed by policy", sess.geofence or "?")
            return
        if not force and policy == "escalated" and level == LEVEL_INFO:
            return

        loc = (await get_prefs())["locale"]
        if sess.peak_state == "panic":
            title = t("sentry.notify.panic", loc)
        elif escalated:
            title = t("sentry.notify.escalated", loc)
        else:
            title = t("sentry.notify.triggered", loc)

        lines = [t("sentry.notify.source", loc,
                   source=t(f"sentry.source.{sess.source}", loc))]
        if reason:
            lines.append(t(reason, loc, **(reason_vars or {})))
        if sess.samples > 1:
            lines.append(t("sentry.notify.samples", loc, n=sess.samples))
        # extras 存的是入侵原因的目录 key（engine.py 传 f"rule.{id}" 进来）。
        # 直接 extend 会把 "rule.sentry_unlocked" 原样推到用户手机上。
        for x in sess.extras:
            lines.append(t(x, loc) if _KEYISH.match(x) else x)

        if self._fire is None:
            return
        # 入库统一记 sentry_activity（历史与界面分组按它），但开关按当次的
        # 严重程度分别查：警报拉响走 sentry_panic，普通活动走 sentry_aware。
        delivered = await self._fire(
            sess.car_id, level, title, "\n".join(lines),
            rule="sentry_activity",
            gate="sentry_panic" if sess.peak_state == "panic" else "sentry_aware",
        )

        # **只有真的推出去了才算"已升级"。** 这个闩之前是在调 _fire 之前就置上的，
        # 而 _fire 里还有三道闸门（规则开关 / 静默时段 / 临时静音）能把消息丢掉。
        # 后果很具体：用户按了「静音 2 小时（不含入侵）」，一次 Aware 被静音丢弃，
        # 却把会话标记成已升级；接下来真的 Panic 时，observe() 里的
        # `not sess.escalated` 直接挡掉 —— 承诺过"入侵仍然会推"，结果没推。
        if escalated and delivered:
            sess.escalated = True

    # ---------- 落库与收尾 ----------

    async def _persist(self, sess: Session, closed: bool) -> int | None:
        try:
            if sess.db_id is None:
                cur = await store.db.execute(
                    "INSERT INTO sentry_sessions"
                    " (car_id, car_name, source, started, last_seen, samples, peak_state,"
                    "  escalated, geofence, soc, extras, closed)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sess.car_id, sess.car_name, sess.source, sess.started, sess.last_seen,
                     sess.samples, sess.peak_state, int(sess.escalated), sess.geofence,
                     sess.soc, json.dumps(sess.extras, ensure_ascii=False), int(closed)),
                )
                await store.db.commit()
                return cur.lastrowid
            await store.db.execute(
                "UPDATE sentry_sessions SET last_seen=?, samples=?, peak_state=?,"
                " escalated=?, extras=?, closed=? WHERE id=?",
                (sess.last_seen, sess.samples, sess.peak_state, int(sess.escalated),
                 json.dumps(sess.extras, ensure_ascii=False), int(closed), sess.db_id),
            )
            await store.db.commit()
            return sess.db_id
        except Exception:  # noqa: BLE001
            log.exception("failed to persist sentry session")
            return sess.db_id

    async def _reaper(self) -> None:
        """静默超过 dedup 窗口就关闭会话。"""
        while True:
            # 粒度取窗口的一半，保证会话关闭时刻与配置的窗口基本吻合
            await asyncio.sleep(max(1.0, min(5.0, settings.sentry_dedup_seconds / 2)))
            now = time.time()
            for car_id, sess in list(self._sessions.items()):
                if now - sess.last_seen < settings.sentry_dedup_seconds:
                    continue
                self._sessions.pop(car_id, None)
                self._recent_end[car_id] = sess.last_seen
                await self._persist(sess, closed=True)
                log.info(
                    "sentry session closed: car=%s dur=%.0fs samples=%d peak=%s",
                    car_id, sess.duration, sess.samples, sess.peak_state,
                )

    async def recent(self, limit: int = 100) -> list[dict]:
        rows = await store.db.execute_fetchall(
            "SELECT * FROM sentry_sessions ORDER BY started DESC LIMIT ?", (limit,)
        )
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["extras"] = json.loads(d.get("extras") or "[]")
            except ValueError:
                d["extras"] = []
            d["duration"] = round(d["last_seen"] - d["started"], 1)
            out.append(d)
        return out


tracker = SentryTracker()
