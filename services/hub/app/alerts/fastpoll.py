"""Fast sentry check — the gap between "something happened" and "TeslaMate noticed".

TeslaMate polls a parked car with Sentry on only every 60 s when streaming is
enabled (30 s × 2), so a short Sentry event can come and go unseen and the
rest arrive a minute late. A SentryPro-class alert needs seconds.

While a car is **awake with Sentry armed** (it cannot sleep then anyway, so
reading costs nothing in battery), this reads ``vehicle_state`` and
``location_data`` every ``SENTRY_FAST_POLL_SECONDS`` and feeds the values into
the same live-state path as TeslaMate's MQTT. Only changes produce
transitions, so TeslaMate's later copy of the same value is a no-op. Each
reading is one complete snapshot, so Sentry decides on it at once instead of
waiting out the quiet period meant for TeslaMate's MQTT bursts.

Ground rules:

* Never wake a car: only cars TeslaMate reports online with Sentry on.
* Use TeslaMate's access token read-only; never refresh it (that would rotate
  the refresh token TeslaMate depends on).
* Back off on 408 (asleep), 429 (rate-limited) and 401 (token rotating).
* Fleet Telemetry, when configured, is faster still; this stands down for a
  car that has a live telemetry stream.
* Cost nothing while idle: one kept-alive connection while Sentry is on (a
  new client per check re-read the CA bundle and redid the TLS handshake —
  about 13 ms of CPU, against 0.5 ms on a warm connection), closed when it
  goes off; and no ticking — the loop sleeps until the next car is due, or
  until Sentry or a car's state changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time

import httpx

from ..config import settings
from ..livestate import live
from ..tesla import tokens
from .sentry import tracker as sentry_tracker
from .store import store

log = logging.getLogger(__name__)

_UA = "OhmyTesla/2 (self-hosted; sentry check)"
_IDLE_S = 60            # longest sleep with nothing to poll; a change wakes it sooner


def _b(v) -> str | None:
    if v is None:
        return None
    return "true" if bool(v) else "false"


def parse_vehicle_state(vs: dict, loc: dict | None = None) -> dict[str, str]:
    """Owner-API vehicle_state (+ drive/location state) → TeslaMate's MQTT key names."""
    out: dict[str, str] = {}
    if "center_display_state" in vs and vs["center_display_state"] is not None:
        out["center_display_state"] = str(vs["center_display_state"])
    for k in ("sentry_mode", "locked", "is_user_present"):
        if vs.get(k) is not None:
            out[k] = _b(vs[k])
    doors = [vs.get(k) for k in ("df", "dr", "pf", "pr") if vs.get(k) is not None]
    if doors:
        out["doors_open"] = _b(any(d not in (0, None) for d in doors))
    if vs.get("ft") is not None:
        out["frunk_open"] = _b(vs["ft"] not in (0, None))
    if vs.get("rt") is not None:
        out["trunk_open"] = _b(vs["rt"] not in (0, None))
    wins = [vs.get(k) for k in ("fd_window", "fp_window", "rd_window", "rp_window") if vs.get(k) is not None]
    if wins:
        out["windows_open"] = _b(any(w not in (0, None) for w in wins))
    if loc:
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is not None and lon is not None:
            out["latitude"], out["longitude"] = f"{float(lat):.6f}", f"{float(lon):.6f}"
    return out


class FastPoll:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._due: dict[str, float] = {}
        self._eid: dict[str, int] = {}
        self.stats: dict[str, dict] = {}
        self._every: int | None = None
        self._every_at = 0.0
        self._wake = asyncio.Event()
        self._cli: httpx.AsyncClient | None = None
        self.start_delay = 15.0     # let TeslaMate's retained state arrive first
        self.transport: httpx.AsyncBaseTransport | None = None   # tests swap in a mock

    async def interval(self) -> int:
        # Read at most once a minute, and again right after Settings changes it: an
        # SQLite round trip on every pass made the hub the busiest idle process of
        # the Mac app's whole stack.
        now = time.monotonic()
        if self._every is None or now - self._every_at > 60:
            override = await store.get_setting("sentry_fast_poll_seconds", None)
            v = settings.sentry_fast_poll_seconds if override is None else override
            try:
                self._every = max(0, int(v))
            except (TypeError, ValueError):
                self._every = 0
            self._every_at = now
        return self._every

    def invalidate(self) -> None:
        self._every = None
        self._wake.set()

    async def on_change(self, car_id: str, key: str, old: str | None, new: str) -> None:
        """Transition hook: Sentry or a car's state changed, so look again now."""
        if key in ("sentry_mode", "state"):
            self._wake.set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="sentry-fast-poll")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._close()

    def status(self) -> dict:
        return {"cars": self.stats}

    async def _vehicle_id(self, car_id: str) -> int | None:
        if car_id in self._eid:
            return self._eid[car_id]
        from .. import db

        try:
            row = await db.fetchrow("SELECT eid FROM cars WHERE id = $1", int(car_id))
        except Exception:  # noqa: BLE001
            return None
        if row and row["eid"]:
            self._eid[car_id] = int(row["eid"])
            return self._eid[car_id]
        return None

    def _eligible(self, car_id: str, car: dict) -> bool:
        return (car_id.isdigit() and live.is_active(car_id)
                and car.get("sentry_mode") == "true"
                and car.get("state") in ("online", "charging")
                and not sentry_tracker.has_fleet(car_id))

    async def _loop(self) -> None:
        await asyncio.sleep(self.start_delay)
        while True:
            try:
                # Cleared before the pass, so a change that lands during it still
                # cuts the sleep below short.
                self._wake.clear()
                every = await self.interval()
                wait: float | None = None       # None: no car to poll
                if every > 0:
                    for car_id, car in list(live.cars.items()):
                        if not self._eligible(car_id, car):
                            if car.get("sentry_mode") != "true":
                                self.stats.pop(car_id, None)
                            continue
                        due = self._due.get(car_id, 0)
                        if time.monotonic() >= due:
                            delay = await self._poll(car_id, every)
                            # a little jitter so several cars never fire in lockstep
                            due = time.monotonic() + delay + random.uniform(0, min(1.5, every / 10))  # noqa: S311 - scheduling jitter, not crypto
                            self._due[car_id] = due
                        left = due - time.monotonic()
                        wait = left if wait is None else min(wait, left)
                if wait is None:
                    await self._close()         # Sentry is off everywhere: let the connection go
                    wait = _IDLE_S
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=max(0.2, wait))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fast sentry poll loop error")
                await asyncio.sleep(10)

    def _client(self) -> httpx.AsyncClient:
        if self._cli is None:
            # Kept alive across checks; httpx's default 5 s expiry would drop the
            # connection just before every 5 s check.
            self._cli = httpx.AsyncClient(
                timeout=httpx.Timeout(12.0), transport=self.transport,
                limits=httpx.Limits(max_keepalive_connections=2, keepalive_expiry=60),
                headers={"User-Agent": _UA, "Accept": "application/json"})
        return self._cli

    async def _close(self) -> None:
        cli, self._cli = self._cli, None
        if cli is not None:
            with contextlib.suppress(Exception):
                await cli.aclose()

    async def _get(self, url: str, token: str) -> httpx.Response:
        kw = {"params": {"endpoints": "vehicle_state;location_data"},
              "headers": {"Authorization": f"Bearer {token}"}}
        try:
            return await self._client().get(url, **kw)
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):
            # The server dropped the kept-alive connection between checks: once
            # more on a fresh one, rather than lose a whole interval.
            await self._close()
            return await self._client().get(url, **kw)

    async def _poll(self, car_id: str, every: int) -> float:
        st = self.stats.setdefault(car_id, {"polls": 0, "errors": 0, "backoff": every})
        token = await tokens.access_token()
        eid = await self._vehicle_id(car_id)
        if not token or not eid:
            st["last_error"] = "no token" if not token else "no vehicle id"
            return 60
        base = (settings.tesla_api_host or "https://owner-api.teslamotors.com").rstrip("/")
        url = f"{base}/api/1/vehicles/{eid}/vehicle_data"
        t0 = time.monotonic()
        try:
            r = await self._get(url, token)
        except httpx.HTTPError as e:
            await self._close()
            st["errors"] += 1
            st["last_error"] = e.__class__.__name__
            return min(120, every * 3)
        st["polls"] += 1
        st["latency_ms"] = int((time.monotonic() - t0) * 1000)
        if r.status_code == 408:            # asleep after all — wait for TeslaMate
            st["last_error"] = "asleep"
            return 120
        if r.status_code == 429:
            st["errors"] += 1
            st["backoff"] = min(300, max(every, st.get("backoff", every)) * 2)
            st["last_error"] = "rate limited"
            log.warning("fast sentry poll rate-limited for car %s; backing off %ss", car_id, st["backoff"])
            return st["backoff"]
        if r.status_code == 401:
            tokens.invalidate_cache()       # TeslaMate is rotating it; re-read soon
            st["last_error"] = "token rotating"
            return 30
        if r.status_code >= 400:
            st["errors"] += 1
            st["last_error"] = f"HTTP {r.status_code}"
            return min(300, every * 6)
        resp = (r.json() or {}).get("response") or {}
        values = parse_vehicle_state(resp.get("vehicle_state") or {},
                                     resp.get("drive_state") or resp.get("location_data") or {})
        for k, v in values.items():
            live.ingest(car_id, k, v, source="fastpoll")
        live.end_snapshot(car_id)           # one complete reading: Sentry can decide now
        st.update({"last_ok": time.time(), "backoff": every, "last_error": None})
        return every


poller = FastPoll()
