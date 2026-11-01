"""Sentry: decide whether something is happening to the parked car, and say so once.

This is the product's reason to exist, so the two failure modes get equal
weight: a real event that never reaches the phone, and a phone that buzzes
"broken into!" every time the owner walks up with their key. The previous
design produced 29 critical "broken into" pushes from 18 owner returns
(audit F1): it pushed from inside the MQTT reader, on the first message of a
batch, before the same batch's ``is_user_present=true`` was even read.

How a decision is made now:

1. **Settle, then decide.** A sentry-relevant change schedules an evaluation
   of the car's *complete* state ~2.5 s after its message burst goes quiet
   (TeslaMate publishes one poll's keys concurrently, in arbitrary order).
2. **Owner first.** Any sign of the owner — someone in the seat, Sentry
   switched off, a gear selected, driving, a key unlock, a command sent from
   this hub — within the last two minutes explains the activity.
3. **Severity from physics, not from one flag:**

   =========  ===============================================================
   alarm      Fleet ``Panic``; Tesla's ``alarmTriggered`` alert; a door,
              trunk, frunk or window opening while the car still reports
              *locked*; the parked car moving with nobody aboard (towing)
   aware      Fleet ``Aware``; the Sentry screen lighting up
              (``center_display_state == 7``); a door handle pulled without
              an authorised key (``handlePullWithoutAuth``)
   info       unlocked while Sentry was on; a door opened on an unlocked car
              — recorded on the timeline, not pushed by default: a break-in
              does not unlock the car, a key does
   =========  ===============================================================

4. **One incident, few pushes.** Signals within an active incident merge into
   it. A push goes out when the incident opens, when it escalates to alarm,
   and once more if awareness persists past ``SENTRY_ESCALATE_SECONDS``.
   Where you park decides how loud *aware* is (Home defaults to "alarm
   only"); an alarm is always delivered.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field

from ..config import settings
from ..i18n.prefs import get as get_prefs
from ..i18n.router import t
from .store import store

log = logging.getLogger(__name__)

LEVEL_INFO, LEVEL_WARN, LEVEL_CRIT = "info", "warning", "critical"
SETTLE_S = 2.5          # quiet time after a burst before deciding
SETTLE_MAX_S = 8.0      # never wait longer than this from the first signal
OWNER_WINDOW_S = 120    # an owner sign this recent explains the activity
TOW_DISTANCE_M = 150    # parked car displaced this far with nobody aboard

# Keys whose change may mean something happened to a parked car.
WATCHED = frozenset({
    "center_display_state", "doors_open", "trunk_open", "frunk_open", "windows_open",
    "locked", "latitude", "longitude",
})
OWNER_KEYS = frozenset({"is_user_present", "sentry_mode", "shift_state", "state"})
_OPENINGS = {"doors_open": "door", "trunk_open": "trunk", "frunk_open": "frunk", "windows_open": "window"}
# Which of this hub's own commands explain which opening. Unlocking lets
# someone open anything; nothing this hub sends explains a tow.
_EXPLAINS = {
    "door": frozenset({"door_unlock", "remote_start"}),
    "trunk": frozenset({"door_unlock", "trunk"}),
    "frunk": frozenset({"door_unlock", "frunk"}),
    "window": frozenset({"door_unlock", "vent_windows"}),
}

_KEYISH = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")

POLICY_CHOICES = {"always": "sentry.policy.always", "escalated": "sentry.policy.escalated",
                  "log": "sentry.policy.log"}
DEFAULT_POLICY = "always"
HOME_DEFAULT_POLICY = "escalated"     # at home, only alarms push unless you choose otherwise


def _true(v) -> bool:
    return v == "true"


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


@dataclass
class Session:
    car_id: str
    car_name: str
    source: str
    started: float
    last_seen: float
    samples: int = 1
    peak_state: str = "aware"           # aware | panic (alarm)
    escalated_level: str = ""           # "" | warn | panic — what has been *delivered*
    geofence: str = ""
    soc: str = ""
    db_id: int | None = None
    extras: list[str] = field(default_factory=list)
    pushed: int = 0

    @property
    def duration(self) -> float:
        return self.last_seen - self.started

    @property
    def tag(self) -> str:
        return f"sentry-{self.car_id}-{self.db_id or int(self.started)}"


@dataclass
class _Pending:
    first: float
    timer: asyncio.TimerHandle | None = None
    signals: list[tuple[str, str | None, str, float]] = field(default_factory=list)


class SentryService:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._recent_end: dict[str, float] = {}
        self._fleet_seen: dict[str, float] = {}
        self._owner_seen: dict[str, float] = {}
        self._pending: dict[str, _Pending] = {}
        self._anchor: dict[str, tuple[float, float]] = {}   # where the car was parked
        self._tow_hits: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        self._fire = None

    # ── wiring ───────────────────────────────────────────────────────────

    def bind(self, fire_callable) -> None:
        self._fire = fire_callable

    def start(self) -> None:
        self._task = asyncio.create_task(self._reaper(), name="sentry-reaper")

    async def stop(self) -> None:
        for p in self._pending.values():
            if p.timer:
                p.timer.cancel()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # ── policies & home ──────────────────────────────────────────────────

    async def policies(self) -> dict:
        return await store.get_setting("sentry_policies", {}) or {}

    async def set_policy(self, geofence: str, policy: str) -> None:
        pols = await self.policies()
        default = HOME_DEFAULT_POLICY if geofence == "__home__" else DEFAULT_POLICY
        if policy == default:
            pols.pop(geofence, None)
        else:
            pols[geofence] = policy
        await store.set_setting("sentry_policies", pols)

    async def home(self) -> dict | None:
        return await store.get_setting("home_location", None)

    async def set_home(self, lat: float, lon: float, radius_m: float = 150, label: str = "") -> dict:
        h = {"lat": float(lat), "lon": float(lon), "radius_m": float(radius_m), "label": label or "Home"}
        await store.set_setting("home_location", h)
        return h

    async def place(self, car: dict) -> tuple[str, bool]:
        """(place label, is_home) for the car's current position."""
        geo = car.get("geofence") or ""
        home = await self.home()
        try:
            pos = (float(car["latitude"]), float(car["longitude"]))
        except (KeyError, TypeError, ValueError):
            pos = None
        if home and pos and _haversine_m(pos, (home["lat"], home["lon"])) <= home.get("radius_m", 150):
            return (geo or home.get("label") or "Home"), True
        return geo, False

    async def _policy(self, place: str, at_home: bool) -> str:
        pols = await self.policies()
        if place and place in pols:
            return pols[place]
        if at_home:
            return pols.get("__home__", HOME_DEFAULT_POLICY)
        return pols.get("__away__", DEFAULT_POLICY)

    # ── inputs ───────────────────────────────────────────────────────────

    def note_fleet(self, car_id: str) -> None:
        self._fleet_seen[car_id] = time.time()

    def has_fleet(self, car_id: str) -> bool:
        return time.time() - self._fleet_seen.get(car_id, 0) < 900

    def note_owner(self, car_id: str, why: str = "") -> None:
        """An unmistakable sign of the owner (seat occupied, Sentry off, a
        command sent from here). Explains activity for OWNER_WINDOW_S."""
        self._owner_seen[car_id] = time.time()
        sess = self._sessions.get(car_id)
        if sess and sess.peak_state != "panic" and why and why not in sess.extras:
            sess.extras.append(why)

    def owner_recent(self, car_id: str, car: dict) -> bool:
        if _true(car.get("is_user_present")):
            return True
        if car.get("state") == "driving" or (car.get("shift_state") or "P") in ("D", "R", "N"):
            return True
        # Sentry being off is not presence: a car parked with Sentry off can
        # still be towed or broken into. Switching it off is an owner sign and
        # is noted (sentry.owner.disarmed) for OWNER_WINDOW_S like the others.
        return time.time() - self._owner_seen.get(car_id, 0) < OWNER_WINDOW_S

    def on_change(self, car_id: str, key: str, old: str | None, new: str) -> None:
        """Called for every live transition; cheap. Owner signs are noted at
        once; watched keys are collected and evaluated after the burst settles."""
        from ..livestate import live

        car = live.cars.get(car_id, {})
        owner_sign = {
            ("is_user_present", "true"): "sentry.owner.seat",
            ("sentry_mode", "false"): "sentry.owner.disarmed",
            ("state", "driving"): "sentry.owner.drive",
        }.get((key, new))
        if key == "shift_state" and new in ("D", "R", "N"):
            owner_sign = "sentry.owner.drive"
        if owner_sign:
            self.note_owner(car_id, owner_sign)

        # Remember where the car was parked, for tow detection. The anchor is
        # where it was *before* this reading: anchoring on the new position
        # would make the first displacement its own reference.
        if key in ("latitude", "longitude") and car.get("state") != "driving":
            if car_id not in self._anchor:
                lat = old if key == "latitude" and old else car.get("latitude")
                lon = old if key == "longitude" and old else car.get("longitude")
                with contextlib.suppress(TypeError, ValueError):
                    self._anchor[car_id] = (float(lat), float(lon))
            elif self.owner_recent(car_id, car):
                with contextlib.suppress(KeyError, TypeError, ValueError):
                    self._anchor[car_id] = (float(car["latitude"]), float(car["longitude"]))
        if key == "state" and new == "driving":
            self._anchor.pop(car_id, None)
            self._tow_hits.pop(car_id, None)

        if key not in WATCHED:
            return
        p = self._pending.get(car_id)
        now = time.time()
        if p is None:
            p = self._pending[car_id] = _Pending(first=now)
        p.signals.append((key, old, new, now))
        if p.timer:
            p.timer.cancel()
        delay = min(SETTLE_S, max(0.0, p.first + SETTLE_MAX_S - now))
        loop = asyncio.get_running_loop()
        p.timer = loop.call_later(delay, lambda: self._spawn(self._evaluate(car_id)))

    async def fleet_sentry(self, car_id: str, state: str, car_name: str) -> None:
        """Authoritative Fleet Telemetry SentryMode edge: no settle needed."""
        self.note_fleet(car_id)
        if state == "Panic":
            await self._signal(car_id, car_name, "fleet", "panic", "sentry.reason.panic")
        elif state == "Aware":
            await self._signal(car_id, car_name, "fleet", "aware", "sentry.reason.aware")

    async def fleet_alert(self, car_id: str, name: str, car_name: str) -> None:
        """Tesla vehicle alerts streamed by Fleet Telemetry.

        `name` may carry "|late=<seconds>" when the car reported it after
        being out of signal; the push then says how long ago."""
        name, _, late = name.partition("|late=")
        mins = {"n": max(1, round(int(late) / 60))} if late.isdigit() else None
        if "alarmTriggered" in name:
            await self._signal(car_id, car_name, "fleet", "panic",
                               "sentry.reason.alarm_late" if mins else "sentry.reason.alarm", mins)
        elif "handlePullWithoutAuth" in name:
            await self._signal(car_id, car_name, "fleet", "aware",
                               "sentry.reason.handle_pull_late" if mins else "sentry.reason.handle_pull", mins)

    async def fleet_display_lock(self, car_id: str, car_name: str) -> None:
        """Fleet DisplayState=Lock: someone touched the locked car (works with Sentry off)."""
        from ..livestate import live

        if not self.owner_recent(car_id, live.cars.get(car_id, {})):
            await self._signal(car_id, car_name, "fleet", "aware", "body.touched_while_locked")

    # ── evaluation ───────────────────────────────────────────────────────

    async def _evaluate(self, car_id: str) -> None:
        from ..livestate import live

        p = self._pending.pop(car_id, None)
        if p is None:
            return
        car = live.cars.get(car_id, {})
        name = await car_label(car_id)
        armed = _true(car.get("sentry_mode"))
        owner = self.owner_recent(car_id, car)
        locked = _true(car.get("locked"))
        source = "polled"

        for key, _old, new, _at in p.signals:
            what = _OPENINGS.get(key)
            if what and new == "true":
                # Only a command that could have caused *this* opening explains
                # it: a honk or a remote lock must not hide a break-in.
                if owner or await _hub_command_recent(car_id, _EXPLAINS[what]):
                    continue
                if not locked:
                    if armed:
                        await self._record_info(car_id, name, f"rule.sentry_{what}_opened")
                    continue
                if what == "door":
                    # A door can't open on a locked car without force (Sentry
                    # on or off): every key unlocks first.
                    await self._signal(car_id, name, "state", "panic", "sentry.reason.forced_door")
                elif what in ("trunk", "frunk"):
                    # The Tesla app opens these remotely and leaves the car
                    # locked, and this hub never sees that command. Without the
                    # alarm it's worth a look, not a break-in call.
                    await self._signal(car_id, name, "state", "aware", f"sentry.reason.forced_{what}")
                else:
                    # Windows vent from the Tesla app on a locked car; broken
                    # glass doesn't move the window sensor. Recorded, not pushed.
                    await self._record_info(car_id, name, f"rule.sentry_{what}_opened")
            elif key == "locked" and new == "false" and armed and not _true(car.get("is_user_present")):
                # A key opened it. Note it so the next minute of activity is explained.
                self.note_owner(car_id, "sentry.owner.unlocked")
                await self._record_info(car_id, name, "rule.sentry_unlocked")
            elif key == "center_display_state" and new == "7":
                if owner or not armed or self.has_fleet(car_id) or not settings.sentry_polled_alerts:
                    continue
                await self._signal(car_id, name, source, "aware", "sentry.reason.display")
            elif key in ("latitude", "longitude"):
                await self._check_tow(car_id, name, car, owner)

    async def _check_tow(self, car_id: str, name: str, car: dict, owner: bool) -> None:
        anchor = self._anchor.get(car_id)
        if anchor is None or owner or car.get("state") == "driving":
            return
        try:
            pos = (float(car["latitude"]), float(car["longitude"]))
        except (KeyError, TypeError, ValueError):
            return
        moved = _haversine_m(anchor, pos)
        if moved < TOW_DISTANCE_M or not (_true(car.get("locked")) or _true(car.get("sentry_mode"))):
            self._tow_hits.pop(car_id, None)
            return
        # One bad GPS fix (a parking garage) can jump 150 m; a tow keeps going.
        # Two readings in a row beyond the radius, or one far beyond it.
        hits = self._tow_hits[car_id] = self._tow_hits.get(car_id, 0) + 1
        if hits >= 2 or moved >= TOW_DISTANCE_M * 3.3:
            await self._signal(car_id, name, "state", "panic", "sentry.reason.towed",
                               reason_vars={"m": int(moved)})
            self._anchor[car_id] = pos     # one alarm per displacement
            self._tow_hits.pop(car_id, None)

    async def _record_info(self, car_id: str, name: str, what: str) -> None:
        """Owner-explained activity: kept on the timeline of the open incident
        (or a short info incident), pushed only if that rule is switched on."""
        sess = self._sessions.get(car_id)
        if sess is not None:
            if what not in sess.extras:
                sess.extras.append(what)
            await self._persist(sess, closed=False)
            return
        if self._fire is not None:
            rule = what.removeprefix("rule.")
            # Same shape as every other push, should the owner switch this rule to Push.
            what = re.sub(r"^[^\w\s]+\s*", "", t(f"rule.{rule}", (await get_prefs())["locale"]))
            await self._fire(car_id, LEVEL_INFO, f"👀 {name} — {what}", "", rule=rule, gate=rule)

    async def _signal(self, car_id: str, car_name: str, source: str, state: str, reason: str,
                      reason_vars: dict | None = None) -> None:
        from ..livestate import live

        now = time.time()
        car = live.cars.get(car_id, {})
        place, at_home = await self.place(car)
        sess = self._sessions.get(car_id)
        if sess is None:
            sess = Session(car_id=car_id, car_name=car_name, source=source, started=now,
                           last_seen=now, geofence=place, soc=car.get("battery_level") or "",
                           peak_state=state)
            sess.extras.append(reason)
            self._sessions[car_id] = sess
            sess.db_id = await self._persist(sess, closed=False)
            repeat = now - self._recent_end.get(car_id, 0) < settings.sentry_escalate_seconds
            await self._notify(sess, at_home, reason, reason_vars, escalated=repeat or state == "panic")
            return

        sess.last_seen = now
        sess.samples += 1
        if source == "fleet":
            sess.source = "fleet"
        if reason not in sess.extras:
            sess.extras.append(reason)
        rising = state == "panic" and sess.peak_state != "panic"
        if state == "panic":
            sess.peak_state = "panic"
        await self._persist(sess, closed=False)

        if rising or (state == "panic" and sess.escalated_level != "panic"):
            await self._notify(sess, at_home, reason, reason_vars, escalated=True)
        elif not sess.escalated_level and sess.duration >= settings.sentry_escalate_seconds:
            await self._notify(sess, at_home, "sentry.reason.sustained", {"n": int(sess.duration)},
                               escalated=True)

    # ── delivery ─────────────────────────────────────────────────────────

    async def _notify(self, sess: Session, at_home: bool, reason: str, reason_vars: dict | None,
                      escalated: bool) -> None:
        """One short push: the title says what happened, the body where and when.

        "🚨 Cybertruck — door opened while locked" / "Home · 10:41 PM". Detail
        (source, every signal in the incident) lives on the Security page,
        which the push opens."""
        alarm = sess.peak_state == "panic"
        level = LEVEL_CRIT if alarm else (LEVEL_WARN if escalated else LEVEL_INFO)
        policy = await self._policy(sess.geofence, at_home)
        if not alarm:                       # an alarm is never silenced by location
            if policy == "log":
                log.info("sentry activity at %s logged only (policy)", sess.geofence or "?")
                return
            if policy == "escalated" and not escalated:
                return                      # at home: repeats and lingering push, a glance doesn't
        if self._fire is None:
            return
        loc = (await get_prefs())["locale"]
        what = await self._short_reason(sess, reason, reason_vars or {}, loc)
        icon = "🚨" if alarm else ("⚠️" if escalated else "👀")
        title = f"{icon} {sess.car_name} — {what}"
        from datetime import datetime

        from .engine import _tz  # local import: engine imports this module

        now = datetime.now(_tz())
        when = now.strftime("%H:%M") if loc == "zh" else now.strftime("%-I:%M %p")
        body = " · ".join(x for x in (sess.geofence, when) if x)
        delivered = await self._fire(
            sess.car_id, level, title, body,
            rule="sentry_activity", gate="sentry_panic" if alarm else "sentry_aware",
            url=f"/security?incident={sess.db_id}" if sess.db_id else "/security",
            tag=sess.tag,
        )
        if delivered:
            sess.pushed += 1
            if escalated or alarm:
                sess.escalated_level = "panic" if alarm else "warn"

    async def _short_reason(self, sess: Session, reason: str, vars_: dict, loc: str) -> str:
        key = reason.rsplit(".", 1)[-1]
        if key == "touched_while_locked":
            key = "touched"
        if key == "forced_trunk" and await _is_cybertruck(sess.car_id):
            key = "forced_tailgate"
        if key == "sustained":
            vars_ = {"n": max(1, round(int(vars_.get("n", 60)) / 60))}
        if key == "towed":
            vars_ = {"m": vars_.get("m", "")}
        return t(f"sentry.short.{key}", loc, **vars_)

    # ── persistence ──────────────────────────────────────────────────────

    async def _persist(self, sess: Session, closed: bool) -> int | None:
        try:
            if sess.db_id is None:
                cur = await store.db.execute(
                    "INSERT INTO sentry_sessions"
                    " (car_id, car_name, source, started, last_seen, samples, peak_state,"
                    "  escalated, geofence, soc, extras, closed)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sess.car_id, sess.car_name, sess.source, sess.started, sess.last_seen,
                     sess.samples, sess.peak_state, int(bool(sess.escalated_level)), sess.geofence,
                     sess.soc, json.dumps(sess.extras, ensure_ascii=False), int(closed)),
                )
                await store.db.commit()
                return cur.lastrowid
            await store.db.execute(
                "UPDATE sentry_sessions SET source=?, last_seen=?, samples=?, peak_state=?,"
                " escalated=?, extras=?, closed=? WHERE id=?",
                (sess.source, sess.last_seen, sess.samples, sess.peak_state,
                 int(bool(sess.escalated_level)), json.dumps(sess.extras, ensure_ascii=False),
                 int(closed), sess.db_id),
            )
            await store.db.commit()
            return sess.db_id
        except Exception:
            log.exception("failed to persist sentry session")
            return sess.db_id

    def _close_after(self, sess: Session) -> float:
        # Fleet signals are continuous; polled ones arrive every 10–60 s, so a
        # polled incident must outlive the gap between two polls.
        return max(settings.sentry_dedup_seconds, 30 if sess.source == "fleet" else 120)

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for car_id, sess in list(self._sessions.items()):
                if now - sess.last_seen < self._close_after(sess):
                    continue
                self._sessions.pop(car_id, None)
                self._recent_end[car_id] = sess.last_seen
                await self._persist(sess, closed=True)
                log.info("sentry incident closed: car=%s dur=%.0fs samples=%d peak=%s pushed=%d",
                         car_id, sess.duration, sess.samples, sess.peak_state, sess.pushed)

    async def close_stale_sessions(self) -> int:
        """At startup: incidents left open by a restart can't still be open."""
        cur = await store.db.execute("UPDATE sentry_sessions SET closed=1 WHERE closed=0")
        await store.db.commit()
        return cur.rowcount or 0

    def active(self) -> dict[str, dict]:
        return {cid: {"started": s.started, "last_seen": s.last_seen, "peak": s.peak_state,
                      "source": s.source, "id": s.db_id}
                for cid, s in self._sessions.items()}

    async def recent(self, limit: int = 100, before: float | None = None,
                     car_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM sentry_sessions WHERE 1=1"
        args: list = []
        if before:
            q += " AND started < ?"
            args.append(before)
        if car_id:
            q += " AND car_id = ?"
            args.append(car_id)
        q += " ORDER BY started DESC LIMIT ?"
        args.append(limit)
        rows = await store.db.execute_fetchall(q, tuple(args))
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


async def car_label(car_id: str) -> str:
    """What to call this car in a notification: its name, else its model, never 'Car 1'
    unless there is truly nothing else."""
    from ..livestate import live

    name = (live.cars.get(car_id, {}) or {}).get("display_name") or ""
    if name.strip():
        return name.strip()
    try:
        from .. import db
        from ..vehicle import catalog

        row = await db.fetchrow("SELECT name, vin, model FROM cars WHERE id = $1", int(car_id))
        if row:
            if row["name"]:
                return row["name"]
            ident = catalog.identify(vin=row["vin"], model=row["model"])
            if ident.key:
                return ident.name
    except Exception:
        log.debug("car_label lookup failed", exc_info=True)
    return f"Car {car_id}"


async def _is_cybertruck(car_id: str) -> bool:
    from ..livestate import live

    if (live.cars.get(car_id, {}) or {}).get("model") == "Cybertruck":
        return True
    try:
        from .. import db

        row = await db.fetchrow("SELECT vin FROM cars WHERE id = $1", int(car_id))
        return bool(row and row["vin"] and len(row["vin"]) > 3 and row["vin"][3].upper() == "C")
    except Exception:  # noqa: BLE001 — DB down or odd id: just not known to be a Cybertruck
        return False


async def _hub_command_recent(car_id: str, commands: frozenset[str],
                              window: float = OWNER_WINDOW_S) -> bool:
    """Did this hub just send one of `commands` to this car?

    A timed-out command counts: the car may well have done it (the API says
    so), and calling the owner's own frunk a break-in is the worse mistake."""
    if not commands:
        return False
    marks = ",".join("?" * len(commands))
    try:
        cur = await store.db.execute(
            f"SELECT 1 FROM command_log WHERE car_id = ? AND ts > ? AND command IN ({marks})"  # noqa: S608 — only placeholders
            " AND (ok = 1 OR error LIKE 'timeout%') LIMIT 1",
            (str(car_id), time.time() - window, *sorted(commands)))
        return await cur.fetchone() is not None
    except Exception:
        log.debug("command_log lookup failed", exc_info=True)
        return False


tracker = SentryService()
