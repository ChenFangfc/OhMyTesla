#!/usr/bin/env python3
"""OhmyTesla background supervisor.

Runs with the CPython bundled inside OhmyTesla.app and keeps the embedded
stack alive, in dependency order:

    PostgreSQL 18 -> Mosquitto -> TeslaMate (runs its own migrations) -> hub

It is started either by the per-user LaunchAgent (OhmyTesla.app/Contents/
Library/LaunchAgents, registered through SMAppService) or, as a fallback, as a
child process of the app. Only one instance runs per user (flock).

Files (all under ~/Library/Application Support/OhmyTesla unless overridden
with OHMYTESLA_HOME / OHMYTESLA_LOGS / OHMYTESLA_CACHES):

    config.env     0600  generated secrets + optional user overrides (KEY=VALUE)
    state.json           what is running, on which ports, how healthy — read by the app
    control.json         desired state written by the app: {"desired": "running"|"stopped",
                         "keep_awake_sentry": bool, "restart_seq": n}
    pgdata/              PostgreSQL cluster (Unix socket only, no TCP by default)
    teslamate/ hub/ teslacam/ mosquitto/ fontconfig/ run/

Logs: ~/Library/Logs/OhmyTesla/<service>.log, rotated (10 MB x 5).

Every listener binds 127.0.0.1 only. Ports default to 47800 (hub), 47801
(TeslaMate) and 47883 (MQTT); a busy port moves to the next free one and the
choice is written to state.json. PostgreSQL listens on a Unix socket only.

Light at idle — it runs all day on a laptop: the loop sleeps 5 s once
everything is up (1 s while something starts or stops) and is woken at once by
SIGCHLD (a service exited) and SIGUSR1 (the app wrote control.json; state.json
version >= 2 advertises that). Health checks run every 30 s, state.json is
rewritten on every change and at least every 30 s as a heartbeat.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import logging
import logging.handlers
import os
import resource
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

STATE_VERSION = 2          # 2: SIGUSR1 wakes the loop; heartbeat every HEARTBEAT_S
HEARTBEAT_S = 30
STEADY_NAP_S = 5.0
BUSY_NAP_S = 1.0
DEFAULT_PORTS = {"hub": 47800, "teslamate": 47801, "mqtt": 47883, "postgres": 47432}
PORT_OVERRIDES = {"hub": "OHMYTESLA_PORT", "teslamate": "TESLAMATE_PORT", "mqtt": "MQTT_PORT",
                  "postgres": "POSTGRES_PORT"}
DB_USER = "teslamate"
DB_NAME = "teslamate"
PORTAL_USER = "ohmytesla"
# Keys the supervisor owns; everything else in config.env is passed to the hub
# verbatim (SMTP_*, TELEGRAM_*, NTFY_*, TESLACAM_ROOT, TESLA_API_HOST, ...).
RESERVED = {"ENCRYPTION_KEY", "TM_DB_PASS", "OHMYTESLA_PASSWORD", "SECRET_KEY_BASE", "SIGNING_SALT",
            "TM_DB_HOST", "TM_DB_PORT", "TM_DB_USER", "TM_DB_NAME", "MQTT_HOST", "DATA_DIR",
            "TESLAMATE_URL", "TESLAMATE_PUBLIC_PORT", "HUB_BIND", "POSTGRES_TCP", "POSTGRES_SHARED_BUFFERS",
            *PORT_OVERRIDES.values()}
# A desktop-sized TeslaMate: two schedulers that sleep instead of spinning when
# idle, a smaller process table, and code loaded when first used rather than all
# at boot (about 120 MB less memory). Overridable with TESLAMATE_ERL_FLAGS /
# TESLAMATE_RELEASE_MODE / TESLAMATE_DATABASE_POOL_SIZE in config.env.
TESLAMATE_ERL_FLAGS = "+S 2:2 +SDcpu 2:2 +SDio 2 +sbwt none +sbwtdcpu none +sbwtdio none +P 65536"
TESLAMATE_POOL_SIZE = "5"
# PostgreSQL 18 for one user and a handful of connections: synchronous I/O (no
# I/O worker processes), no logical replication launcher, autovacuum that looks
# every 5 min instead of every minute, and a smaller buffer cache.
POSTGRES_SETTINGS = (
    "max_connections=30",            # TeslaMate 5 + hub 5 + hub read-only 2 + psql, with room to spare
    "io_method=sync",
    "max_worker_processes=4",
    "max_parallel_workers=2",
    "max_logical_replication_workers=0",
    "autovacuum_max_workers=2",
    "autovacuum_naptime=5min",
)
_PW_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
log = logging.getLogger("supervisor")


# --------------------------------------------------------------------------- paths

class Paths:
    def __init__(self, runtime: Path) -> None:
        home = Path.home()
        self.runtime = runtime
        self.data = Path(os.environ.get("OHMYTESLA_HOME") or home / "Library/Application Support/OhmyTesla")
        self.logs = Path(os.environ.get("OHMYTESLA_LOGS") or home / "Library/Logs/OhmyTesla")
        self.caches = Path(os.environ.get("OHMYTESLA_CACHES") or home / "Library/Caches/OhmyTesla")
        d = self.data
        self.config = d / "config.env"
        self.state = d / "state.json"
        self.control = d / "control.json"
        self.run = d / "run"
        self.pgdata = d / "pgdata"
        self.tm_home = d / "teslamate"
        self.hub_data = d / "hub"
        self.teslacam = d / "teslacam"
        self.mqtt_home = d / "mosquitto"
        self.fc_conf = d / "fontconfig" / "fonts.conf"
        # bundled runtime
        self.python = Path(sys.executable)
        self.pg_bin = runtime / "postgresql" / "bin"
        self.mosquitto = runtime / "mosquitto" / "sbin" / "mosquitto"
        self.teslamate = runtime / "teslamate" / "bin" / "teslamate"
        self.hub = runtime / "hub"
        self.ffmpeg_bin = runtime / "ffmpeg" / "bin"
        self.fonts = runtime / "fonts"
        self.openssl_cnf = runtime / "etc" / "openssl.cnf"

    def socket_dir(self) -> Path:
        """Postgres listens on a Unix socket in its data directory. sun_path is
        104 bytes on macOS; fall back to the per-user temp dir for long homes."""
        name = f".s.PGSQL.{DEFAULT_PORTS['postgres']}"
        if len(str(self.pgdata / name)) <= 100:
            return self.pgdata
        alt = Path(os.environ.get("TMPDIR", "/tmp")) / f"ohmytesla-{os.getuid()}"
        alt.mkdir(mode=0o700, exist_ok=True)
        return alt


# --------------------------------------------------------------------------- config

def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def portal_password() -> str:
    import secrets   # here, not at the top: it pulls in OpenSSL, needed once per install

    return "-".join("".join(secrets.choice(_PW_ALPHABET) for _ in range(5)) for _ in range(4))


def ensure_config(path: Path) -> tuple[dict[str, str], bool]:
    """Create config.env with fresh secrets on first run; add any missing ones later."""
    import secrets

    generators = {
        "ENCRYPTION_KEY": lambda: secrets.token_urlsafe(48),
        "TM_DB_PASS": lambda: secrets.token_urlsafe(24),
        "OHMYTESLA_PASSWORD": portal_password,
        "SECRET_KEY_BASE": lambda: secrets.token_urlsafe(64),
        "SIGNING_SALT": lambda: secrets.token_urlsafe(12),
    }
    first = not path.exists()
    cfg = load_env_file(path)
    missing = {k: g() for k, g in generators.items() if not cfg.get(k)}
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = "" if not first else (
            "# OhmyTesla configuration — generated on first launch. Keep this file private (0600).\n"
            "# ENCRYPTION_KEY encrypts your Tesla tokens in the database: losing it means signing in again.\n"
            "# OHMYTESLA_PASSWORD is the portal password (user name: ohmytesla).\n"
            "# Add hub settings below (SMTP_HOST=..., TELEGRAM_BOT_TOKEN=..., TESLACAM_ROOT=/Volumes/TESLADRIVE, ...)\n"
            "# and choose Restart Services in the menu bar. See README for port and bind options.\n"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(header)
            for k, v in missing.items():
                f.write(f"{k}={v}\n")
        cfg.update(missing)
    os.chmod(path, 0o600)
    return cfg, first


def system_timezone() -> str:
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


# --------------------------------------------------------------------------- probes

def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c:
        c.settimeout(0.3)
        if c.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_port(preferred: int, taken: set[int]) -> int:
    for p in [preferred, *range(preferred + 1, preferred + 200)]:
        if p not in taken and port_free(p):
            return p
    raise RuntimeError(f"no free port near {preferred}")


def tcp_ok(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c:
        c.settimeout(1.0)
        return c.connect_ex(("127.0.0.1", port)) == 0


def http_status(port: int, path: str, auth: str | None = None, timeout: float = 4.0) -> tuple[int, bytes]:
    """GET http://127.0.0.1:<port><path> -> (status, body); (0, b"") when nothing
    answers. HTTP/1.0 on a plain socket (loopback only: no TLS, no redirects), so
    a process that runs all day never imports urllib, http.client or ssl."""
    request = f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n"
    if auth:
        request += "Authorization: Basic " + base64.b64encode(auth.encode()).decode() + "\r\n"
    chunks: list[bytes] = []
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall((request + "\r\n").encode())
            size = 0
            while size < 1 << 20:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
                size += len(b)
    except OSError:      # refused, reset, timeout
        return 0, b""
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    try:
        return int(head.split(b" ", 2)[1]), body
    except (IndexError, ValueError):
        return 0, b""


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_command(pid: int) -> str:
    r = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip()


# --------------------------------------------------------------------------- logging

def service_logger(paths: Paths, name: str, stamp: bool = True) -> logging.Logger:
    """Rotating per-service log. stamp=False for programs that timestamp their own lines."""
    lg = logging.getLogger(f"svc.{name}")
    if not lg.handlers:
        h = logging.handlers.RotatingFileHandler(paths.logs / f"{name}.log", maxBytes=10 * 1024 * 1024,
                                                 backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s" if stamp else "%(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return lg


# --------------------------------------------------------------------------- services

class Service:
    """One supervised child process (optionally preceded by a one-shot 'prepare'
    command, used for TeslaMate's migrations)."""

    def __init__(self, sup: Supervisor, name: str, title: str, deps: list[str],
                 start_timeout: float, health_interval: float = 30.0, stamp: bool = True) -> None:
        self.sup = sup
        self.name = name
        self.title = title
        self.deps = deps
        self.start_timeout = start_timeout
        self.health_interval = health_interval
        self.enabled = True
        self.proc: subprocess.Popen | None = None
        self.prep: subprocess.Popen | None = None
        self.state = "stopped"
        self.healthy = False
        self.restarts = 0
        self.crash_times: list[float] = []
        self.next_start = 0.0
        self.phase_started = 0.0
        self.last_health = 0.0
        self.fail_streak = 0
        self.last_error = ""
        self.logger = service_logger(sup.paths, name, stamp)

    # subclasses fill these in
    def argv(self) -> list[str]: raise NotImplementedError
    def prepare_argv(self) -> list[str] | None: return None
    def env(self) -> dict[str, str]: return self.sup.base_env()
    def cwd(self) -> Path: return self.sup.paths.data
    def check(self) -> bool: raise NotImplementedError
    def stop_proc(self, proc: subprocess.Popen) -> None:
        self._terminate(proc, signal.SIGTERM, 20)

    # ---- process plumbing
    def _spawn(self, argv: list[str]) -> subprocess.Popen:
        self.logger.info("---- %s starting: %s", time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(argv))
        # Own session: a Ctrl-C in a terminal or launchd's SIGTERM reaches only the
        # supervisor, which then stops everything in reverse dependency order.
        proc = subprocess.Popen(argv, cwd=self.cwd(), env=self.env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True,
                                start_new_session=True)
        threading.Thread(target=self._pump, args=(proc,), name=f"log-{self.name}", daemon=True).start()
        self.sup.record_child(self.name, proc.pid)
        return proc

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            self.logger.info("%s", raw.decode("utf-8", "replace").rstrip())
        proc.stdout.close()

    def _terminate(self, proc: subprocess.Popen, sig: int, timeout: float) -> None:
        if proc.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.logger.info("---- did not exit in %ss; killing", timeout)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            proc.wait(10)

    # ---- state machine, driven from the supervisor loop
    def deps_ready(self) -> bool:
        return all(self.sup.services[d].state == "running" or not self.sup.services[d].enabled
                   for d in self.deps)

    def start(self) -> None:
        self.healthy = False
        self.fail_streak = 0
        self.phase_started = time.time()
        try:
            pre = self.prepare_argv()
            if pre:
                self.prep = self._spawn(pre)
                self.state = "preparing"
            else:
                self.proc = self._spawn(self.argv())
                self.state = "starting"
        except Exception as e:  # noqa: BLE001
            self.crashed(f"could not start: {e}")

    def crashed(self, why: str) -> None:
        now = time.time()
        self.healthy = False
        self.last_error = why
        self.restarts += 1
        self.crash_times = [t for t in self.crash_times if now - t < 600] + [now]
        delay = min(300.0, 2.0 ** min(len(self.crash_times), 8))
        self.next_start = now + delay
        self.state = "backoff"
        log.warning("%s: %s — restarting in %.0fs", self.name, why, delay)
        self.logger.info("---- %s; restart in %.0fs", why, delay)
        for p in (self.prep, self.proc):
            if p is not None and p.poll() is None:
                self._terminate(p, signal.SIGTERM, 15)
        self.prep = self.proc = None

    def tick(self, now: float) -> None:
        if not self.enabled:
            self.state = "disabled"
            return
        if self.state in ("stopped", "waiting", "backoff"):
            if now < self.next_start:
                return
            if not self.deps_ready():
                self.state = "waiting"
                return
            self.start()
            return
        if self.state == "preparing":
            assert self.prep is not None
            rc = self.prep.poll()
            if rc is None:
                if now - self.phase_started > self.start_timeout:
                    self.crashed(f"preparation took longer than {self.start_timeout:.0f}s")
                return
            self.prep = None
            if rc != 0:
                self.crashed(f"preparation exited with code {rc}")
                return
            self.proc = self._spawn(self.argv())
            self.state = "starting"
            self.phase_started = now
            return
        assert self.proc is not None
        rc = self.proc.poll()
        if rc is not None:
            self.proc = None
            self.crashed(f"exited with code {rc}")
            return
        if self.state == "starting":
            if self.check():
                self.state = "running"
                self.healthy = True
                self.last_health = now
                self.last_error = ""
                log.info("%s: up (pid %s)", self.name, self.proc.pid)
            elif now - self.phase_started > self.start_timeout:
                self.crashed(f"not healthy after {self.start_timeout:.0f}s")
            return
        if now - self.last_health >= self.health_interval:
            self.last_health = now
            ok = self.check()
            self.healthy = ok
            if ok:
                self.fail_streak = 0
                if self.state == "unhealthy":
                    self.state = "running"
                # a long healthy run forgives old crashes
                if self.crash_times and now - self.crash_times[-1] > 900:
                    self.crash_times.clear()
            else:
                self.fail_streak += 1
                self.state = "unhealthy"
                if self.fail_streak >= 6:
                    self.crashed("health check failed 6 times in a row")

    def stop(self) -> None:
        if self.prep is not None and self.prep.poll() is None:
            self.logger.info("---- stopping (during preparation)")
            self._terminate(self.prep, signal.SIGTERM, 20)
        if self.proc is not None and self.proc.poll() is None:
            self.logger.info("---- stopping")
            self.stop_proc(self.proc)
        self.prep = self.proc = None
        self.state = "stopped" if self.enabled else "disabled"
        self.healthy = False
        self.next_start = 0.0

    def snapshot(self) -> dict:
        pid = self.proc.pid if self.proc else (self.prep.pid if self.prep else None)
        return {"title": self.title, "state": self.state, "healthy": self.healthy, "pid": pid,
                "restarts": self.restarts, "last_error": self.last_error,
                "since": self.phase_started or None}


class Postgres(Service):
    def __init__(self, sup: Supervisor) -> None:
        super().__init__(sup, "postgres", "Database (PostgreSQL 18)", [], start_timeout=90, stamp=False)

    def argv(self) -> list[str]:
        s = self.sup
        listen = "127.0.0.1" if s.cfg.get("POSTGRES_TCP", "") in ("1", "true", "yes") else ""
        argv = [str(s.paths.pg_bin / "postgres"), "-D", str(s.paths.pgdata),
                "-c", f"listen_addresses={listen}",
                "-c", f"port={s.ports['postgres']}",
                "-c", f"unix_socket_directories={s.sockdir}",
                "-c", "unix_socket_permissions=0700",
                "-c", "jit=off",
                "-c", "timezone=UTC", "-c", "log_timezone=UTC",
                "-c", f"shared_buffers={s.cfg.get('POSTGRES_SHARED_BUFFERS') or '64MB'}",
                "-c", "logging_collector=off",
                "-c", "log_line_prefix=%m [%p] "]
        for setting in POSTGRES_SETTINGS:
            argv += ["-c", setting]
        return argv

    def prepare_argv(self) -> list[str] | None:
        return None

    def check(self) -> bool:
        s = self.sup
        r = subprocess.run([str(s.paths.pg_bin / "pg_isready"), "-q", "-h", str(s.sockdir),
                            "-p", str(s.ports["postgres"]), "-U", DB_USER, "-d", "postgres", "-t", "3"],
                           env=s.base_env(), capture_output=True)
        if r.returncode != 0:
            return False
        if not s.db_ready:
            s.db_ready = s.ensure_database()
        return s.db_ready

    def stop_proc(self, proc: subprocess.Popen) -> None:
        s = self.sup
        r = subprocess.run([str(s.paths.pg_bin / "pg_ctl"), "stop", "-D", str(s.paths.pgdata),
                            "-m", "fast", "-w", "-t", "60"], env=s.base_env(), capture_output=True, text=True)
        self.logger.info("---- pg_ctl stop -m fast: %s", (r.stdout + r.stderr).strip())
        self._terminate(proc, signal.SIGINT, 30)


class Mosquitto(Service):
    def __init__(self, sup: Supervisor) -> None:
        super().__init__(sup, "mosquitto", "MQTT broker (Mosquitto)", [], start_timeout=20)

    def argv(self) -> list[str]:
        return [str(self.sup.paths.mosquitto), "-c", str(self.sup.paths.mqtt_home / "mosquitto.conf")]

    def cwd(self) -> Path:
        return self.sup.paths.mqtt_home

    def check(self) -> bool:
        return tcp_ok(self.sup.ports["mqtt"])


class TeslaMate(Service):
    def __init__(self, sup: Supervisor) -> None:
        super().__init__(sup, "teslamate", "TeslaMate", ["postgres", "mosquitto"], start_timeout=900,
                         stamp=False)

    def prepare_argv(self) -> list[str] | None:
        # Same as the upstream container entrypoint.
        return [str(self.sup.paths.teslamate), "eval", "TeslaMate.Release.wait_for_database_and_migrate()"]

    def argv(self) -> list[str]:
        return [str(self.sup.paths.teslamate), "start"]

    def cwd(self) -> Path:
        return self.sup.paths.tm_home

    def env(self) -> dict[str, str]:
        s = self.sup
        p = s.paths
        port = s.ports["teslamate"]
        e = s.base_env()
        e.update({
            "ENCRYPTION_KEY": s.cfg["ENCRYPTION_KEY"],
            "DATABASE_USER": DB_USER, "DATABASE_PASS": s.cfg["TM_DB_PASS"], "DATABASE_NAME": DB_NAME,
            "DATABASE_SOCKET_DIR": str(s.sockdir), "DATABASE_PORT": str(s.ports["postgres"]),
            "MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(s.ports["mqtt"]),
            "PORT": str(port), "HTTP_BINDING_ADDRESS": "127.0.0.1",
            "CHECK_ORIGIN": f"//127.0.0.1:{port},//localhost:{port}",
            "VIRTUAL_HOST": "127.0.0.1",
            "SECRET_KEY_BASE": s.cfg["SECRET_KEY_BASE"], "SIGNING_SALT": s.cfg["SIGNING_SALT"],
            "SRTM_CACHE": str(p.tm_home / "srtm"), "TZDATA_DIR": str(p.tm_home / "tzdata"),
            "IMPORT_DIR": str(p.tm_home / "import"),
            # A mix release writes its runtime sys.config under RELEASE_TMP, which
            # defaults to inside the (read-only, signed) bundle.
            "RELEASE_TMP": str(p.tm_home / "tmp"),
            # No Erlang distribution: no epmd daemon, no extra listening port.
            "RELEASE_DISTRIBUTION": "none",
            "RELEASE_MODE": "interactive",
            "ERL_FLAGS": TESLAMATE_ERL_FLAGS,
            "DATABASE_POOL_SIZE": TESLAMATE_POOL_SIZE,
            "ERL_CRASH_DUMP": str(p.logs / "teslamate_erl_crash.dump"),
            "OPENSSL_CONF": str(p.openssl_cnf),
        })
        for k in ("TESLA_API_HOST", "TESLA_AUTH_HOST", "TESLA_AUTH_PATH", "TESLA_WSS_HOST",
                  "TESLA_WSS_TLS_ACCEPT_INVALID_CERTS", "TESLA_WSS_USE_VIN", "TOKEN",
                  "DEFAULT_GEOFENCE", "MQTT_NAMESPACE", "MQTT_HOME_ASSISTANT_DISCOVERY",
                  "DATABASE_POOL_SIZE", "DATABASE_TIMEOUT", "ERL_FLAGS", "RELEASE_MODE"):
            if s.cfg.get(f"TESLAMATE_{k}"):
                e[k] = s.cfg[f"TESLAMATE_{k}"]
        return e

    def check(self) -> bool:
        # TeslaMate's own probe: answered by the endpoint itself, no page render
        # and no database queries (GET / renders the whole car page).
        status, _ = http_status(self.sup.ports["teslamate"], "/health_check")
        return status == 200

    def stop_proc(self, proc: subprocess.Popen) -> None:
        # The BEAM treats SIGTERM as init:stop() — a clean shutdown.
        self._terminate(proc, signal.SIGTERM, 30)


class Hub(Service):
    def __init__(self, sup: Supervisor) -> None:
        super().__init__(sup, "hub", "OhmyTesla hub", ["postgres", "mosquitto", "teslamate"],
                         start_timeout=120, stamp=False)

    def argv(self) -> list[str]:
        s = self.sup
        bind = s.cfg.get("HUB_BIND", "127.0.0.1") or "127.0.0.1"
        return [str(s.paths.python), "-s", "-m", "uvicorn", "app.main:app",
                "--host", bind, "--port", str(s.ports["hub"]),
                "--no-access-log", "--timeout-graceful-shutdown", "10"]

    def cwd(self) -> Path:
        return self.sup.paths.hub

    def env(self) -> dict[str, str]:
        s = self.sup
        p = s.paths
        e = s.base_env()
        # user settings first, so the wiring below always wins
        for k, v in s.cfg.items():
            if k not in RESERVED:
                e[k] = v
        e.update({
            "PATH": f"{p.ffmpeg_bin}:{e['PATH']}",
            "TM_DB_HOST": str(s.sockdir), "TM_DB_PORT": str(s.ports["postgres"]),
            "TM_DB_USER": DB_USER, "TM_DB_PASS": s.cfg["TM_DB_PASS"], "TM_DB_NAME": DB_NAME,
            "MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(s.ports["mqtt"]),
            "DATA_DIR": str(p.hub_data),
            "ENCRYPTION_KEY": s.cfg["ENCRYPTION_KEY"],
            "OHMYTESLA_PASSWORD": s.cfg["OHMYTESLA_PASSWORD"],
            "TESLAMATE_URL": f"http://127.0.0.1:{s.ports['teslamate']}",
            "TESLAMATE_PUBLIC_PORT": str(s.ports["teslamate"]),
            "FONTCONFIG_FILE": str(p.fc_conf),
            "OPENSSL_CONF": str(p.openssl_cnf),
            "PYTHONPYCACHEPREFIX": str(p.caches / "pycache"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
        })
        e.setdefault("TESLACAM_ROOT", str(p.teslacam))
        # No Grafana in the app; an empty URL lets the hub hide its links (HUB_CHANGES.md #3).
        e.setdefault("GRAFANA_URL", "")
        return e

    def check(self) -> bool:
        status, body = http_status(self.sup.ports["hub"], "/healthz")
        if status != 200:
            return False
        try:
            return bool(json.loads(body or b"{}").get("ok"))
        except ValueError:
            return False

    def stop_proc(self, proc: subprocess.Popen) -> None:
        self._terminate(proc, signal.SIGTERM, 20)


# --------------------------------------------------------------------------- supervisor

class Supervisor:
    def __init__(self, paths: Paths, parent_pid: int | None = None) -> None:
        self.paths = paths
        self.parent_pid = parent_pid
        self.cfg: dict[str, str] = {}
        self.first_run = False
        self.ports: dict[str, int] = {}
        self.sockdir = paths.pgdata
        self.db_ready = False
        self.desired = "running"
        self.keep_awake = False
        self.restart_seq = 0
        self.control_mtime = 0.0
        self.stopping = False
        self.wake = threading.Event()
        self.error = ""
        self.caffeinate: subprocess.Popen | None = None
        self.sentry_on = False
        self.last_sentry_check = 0.0
        self.last_state = ""
        self.last_write = 0.0
        self.hup = False
        self.lock_fd: int | None = None
        self.children: dict[str, int] = {}
        self.services: dict[str, Service] = {}
        for svc in (Postgres(self), Mosquitto(self), TeslaMate(self), Hub(self)):
            self.services[svc.name] = svc

    # ---- environment shared by every child
    def base_env(self) -> dict[str, str]:
        e = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()),
            "USER": os.environ.get("USER", ""),
            "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "")),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "TZ": self.cfg.get("TZ") or system_timezone(),
            # Every bundled libcrypto (Postgres, the Erlang crypto NIF, Python
            # wheels) would otherwise read its build machine's openssl.cnf.
            "OPENSSL_CONF": str(self.paths.openssl_cnf),
        }
        return {k: v for k, v in e.items() if v}

    def record_child(self, name: str, pid: int) -> None:
        self.children[name] = pid
        with contextlib.suppress(OSError):
            (self.paths.run / "children.json").write_text(json.dumps(self.children))

    # ---- one-time setup
    def acquire_lock(self) -> bool:
        self.paths.run.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.paths.run / "supervisor.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self.lock_fd = fd
        return True

    def prepare(self) -> None:
        p = self.paths
        for d in (p.data, p.run, p.tm_home, p.tm_home / "tmp", p.tm_home / "srtm", p.tm_home / "tzdata",
                  p.hub_data, p.teslacam, p.mqtt_home, p.fc_conf.parent, p.logs,
                  p.caches / "pycache", p.caches / "fontconfig"):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(p.data, 0o700)
        for name in ("python", "pg_bin", "mosquitto", "teslamate", "hub", "ffmpeg_bin", "openssl_cnf"):
            if not getattr(p, name).exists():
                raise RuntimeError(f"bundled runtime is incomplete: {getattr(p, name)} is missing")
        self.cfg, self.first_run = ensure_config(p.config)
        with contextlib.suppress(ValueError, OSError):
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            want = 10240 if hard == resource.RLIM_INFINITY else min(10240, hard)
            if soft < want:
                resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        self.reap_leftovers()
        self.read_control(force=True)

    def reap_leftovers(self) -> None:
        """Children of a supervisor that died uncleanly (SIGKILL, crash) keep
        running in their own sessions. Stop them before starting new ones, or
        the new ones would find their ports and the database lock taken."""
        f = self.paths.run / "children.json"
        pending: list[int] = []
        with contextlib.suppress(OSError, ValueError):
            old = json.loads(f.read_text())
            for name, pid in old.items():
                if pid_alive(pid) and str(self.paths.runtime) in pid_command(pid):
                    log.warning("stopping leftover %s (pid %s) from a previous run", name, pid)
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGINT if name == "postgres" else signal.SIGTERM)
                    pending.append(pid)
        f.unlink(missing_ok=True)
        deadline = time.time() + 45
        while pending and time.time() < deadline:
            time.sleep(0.5)
            pending = [p for p in pending if pid_alive(p)]
        for pid in pending:
            log.warning("leftover pid %s ignored SIGTERM; killing it", pid)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        pidfile = self.paths.pgdata / "postmaster.pid"
        if pidfile.exists():
            with contextlib.suppress(OSError, ValueError, IndexError):
                pid = int(pidfile.read_text().splitlines()[0])
                if pid_alive(pid) and "postgres" in pid_command(pid):
                    log.warning("stopping leftover postgres (pid %s)", pid)
                    subprocess.run([str(self.paths.pg_bin / "pg_ctl"), "stop", "-D", str(self.paths.pgdata),
                                    "-m", "fast", "-w", "-t", "60"], env=self.base_env(), capture_output=True)

    def select_ports(self) -> None:
        taken: set[int] = set()
        for name, default in DEFAULT_PORTS.items():
            pref = int(self.cfg.get(PORT_OVERRIDES[name]) or default)
            if name == "postgres" and self.cfg.get("POSTGRES_TCP", "") not in ("1", "true", "yes"):
                self.ports[name] = pref        # Unix socket only: the number just names the socket
                continue
            self.ports[name] = choose_port(pref, taken)
            taken.add(self.ports[name])
            if self.ports[name] != pref:
                log.warning("port %s for %s is busy; using %s", pref, name, self.ports[name])
        self.sockdir = self.paths.socket_dir()

    def write_runtime_files(self) -> None:
        p = self.paths

        def esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # libass (inside the bundled ffmpeg) finds "Noto Sans CJK SC" through this.
        p.fc_conf.write_text(
            '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n<fontconfig>\n'
            f"  <dir>{esc(str(p.fonts))}</dir>\n"
            "  <dir>/System/Library/Fonts</dir>\n  <dir>/Library/Fonts</dir>\n  <dir>~/Library/Fonts</dir>\n"
            f"  <cachedir>{esc(str(p.caches / 'fontconfig'))}</cachedir>\n"
            "</fontconfig>\n", encoding="utf-8")
        # Same as the eclipse-mosquitto image's mosquitto-no-auth.conf, but loopback only.
        (p.mqtt_home / "mosquitto.conf").write_text(
            "# Generated by the OhmyTesla supervisor on every start.\n"
            f"listener {self.ports['mqtt']} 127.0.0.1\n"
            "allow_anonymous true\n"
            "persistence false\n"
            "log_dest stderr\n"
            "log_type error\nlog_type warning\nlog_type notice\n"
            "connection_messages false\n", encoding="utf-8")

    # ---- database bootstrap
    def init_cluster(self) -> None:
        p = self.paths
        version_file = p.pgdata / "PG_VERSION"
        if version_file.exists():
            major = version_file.read_text().strip()
            if major != "18":
                raise RuntimeError(f"the database in {p.pgdata} was created by PostgreSQL {major}; "
                                   "this app bundles PostgreSQL 18 and cannot upgrade it automatically")
            return
        if p.pgdata.exists() and any(p.pgdata.iterdir()):
            raise RuntimeError(f"{p.pgdata} exists but is not a PostgreSQL data directory")
        log.info("first run: initialising the PostgreSQL cluster in %s", p.pgdata)
        pwfile = p.run / "initdb.pw"
        fd = os.open(pwfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.cfg["TM_DB_PASS"] + "\n")
        try:
            r = subprocess.run([str(p.pg_bin / "initdb"), "-D", str(p.pgdata), "-U", DB_USER,
                                f"--pwfile={pwfile}", "-A", "scram-sha-256", "-E", "UTF8", "--locale=C",
                                "--locale-provider=builtin", "--builtin-locale=C.UTF-8", "--no-instructions"],
                               env=self.base_env(), capture_output=True, text=True)
            self.services["postgres"].logger.info("%s", (r.stdout + r.stderr).strip())
            if r.returncode != 0:
                raise RuntimeError(f"initdb failed: {r.stderr.strip()[-400:]}")
        finally:
            pwfile.unlink(missing_ok=True)

    def psql(self, db: str, sql: str) -> subprocess.CompletedProcess:
        env = self.base_env()
        env["PGPASSWORD"] = self.cfg["TM_DB_PASS"]
        return subprocess.run([str(self.paths.pg_bin / "psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1",
                               "-h", str(self.sockdir), "-p", str(self.ports["postgres"]),
                               "-U", DB_USER, "-d", db, "-tAc", sql], env=env, capture_output=True, text=True)

    def ensure_database(self) -> bool:
        """Create the teslamate database and the contrib extensions it needs."""
        r = self.psql("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}'")
        if r.returncode != 0:
            log.warning("database check failed: %s", r.stderr.strip())
            return False
        if r.stdout.strip() != "1":
            log.info("creating database %s", DB_NAME)
            r = self.psql("postgres", f'CREATE DATABASE "{DB_NAME}" OWNER "{DB_USER}" ENCODING \'UTF8\'')
            if r.returncode != 0:
                log.error("CREATE DATABASE failed: %s", r.stderr.strip())
                return False
        r = self.psql(DB_NAME, "CREATE EXTENSION IF NOT EXISTS cube; CREATE EXTENSION IF NOT EXISTS earthdistance;")
        if r.returncode != 0:
            log.error("creating extensions failed: %s", r.stderr.strip())
            return False
        return True

    # ---- control from the app
    def read_control(self, force: bool = False) -> bool:
        """Returns True when a restart was requested."""
        try:
            mtime = self.paths.control.stat().st_mtime
        except FileNotFoundError:
            return False
        if not force and mtime == self.control_mtime:
            return False
        self.control_mtime = mtime
        try:
            c = json.loads(self.paths.control.read_text())
        except (OSError, ValueError):
            return False
        self.desired = "stopped" if c.get("desired") == "stopped" else "running"
        self.keep_awake = bool(c.get("keep_awake_sentry", False))
        seq = int(c.get("restart_seq", 0) or 0)
        restart = (not force) and seq != self.restart_seq
        self.restart_seq = seq
        return restart

    # ---- keep-awake while Sentry Mode is on
    def keep_awake_tick(self, now: float) -> None:
        want = False
        if self.keep_awake and self.desired == "running" and self.services["hub"].state == "running":
            if now - self.last_sentry_check >= 30:
                self.last_sentry_check = now
                status, body = http_status(self.ports["hub"], "/api/live/state",
                                           auth=f"{PORTAL_USER}:{self.cfg['OHMYTESLA_PASSWORD']}")
                if status == 200:
                    try:
                        cars = json.loads(body or b"{}")
                        self.sentry_on = any(str(v.get("sentry_mode", "")).lower() == "true"
                                             for v in cars.values() if isinstance(v, dict))
                    except ValueError:
                        pass
            want = self.sentry_on
        running = self.caffeinate is not None and self.caffeinate.poll() is None
        if want and not running:
            # -i: an IOPMAssertion PreventUserIdleSystemSleep; -w: ends with us.
            self.caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL)
            log.info("Sentry Mode is on: keeping the Mac awake (caffeinate pid %s)", self.caffeinate.pid)
        elif not want and running:
            assert self.caffeinate is not None
            self.caffeinate.terminate()
            self.caffeinate.wait(5)
            self.caffeinate = None
            log.info("released the keep-awake assertion")

    # ---- state for the app
    def overall(self) -> str:
        if self.error:
            return "error"
        live = [x for x in self.services.values() if x.enabled]
        if self.desired == "stopped":
            return "stopped" if all(x.proc is None and x.prep is None for x in live) else "stopping"
        if all(x.state == "running" for x in live):
            return "running"
        if any(x.state in ("backoff", "unhealthy") for x in live):
            return "degraded"
        return "starting"

    def write_state(self, force: bool = False, final: bool = False) -> None:
        state = {
            "version": STATE_VERSION, "pid": None if final else os.getpid(),
            "status": "stopped" if final else self.overall(), "desired": self.desired,
            "error": self.error, "first_run": self.first_run,
            "ports": self.ports, "postgres_socket_dir": str(self.sockdir),
            "urls": {"hub": f"http://127.0.0.1:{self.ports.get('hub', 0)}/",
                     "teslamate": f"http://127.0.0.1:{self.ports.get('teslamate', 0)}/"},
            "services": {n: x.snapshot() for n, x in self.services.items()},
            "keep_awake": {"enabled": self.keep_awake, "sentry_on": self.sentry_on,
                           "active": self.caffeinate is not None and self.caffeinate.poll() is None},
            "paths": {"data": str(self.paths.data), "logs": str(self.paths.logs),
                      "config": str(self.paths.config), "runtime": str(self.paths.runtime)},
            "mode": "child" if self.parent_pid else "agent",
        }
        body = json.dumps(state, sort_keys=True)
        # rewrite on change, and at least every HEARTBEAT_S so the app can tell we're alive
        if not force and body == self.last_state and time.time() - self.last_write < HEARTBEAT_S:
            return
        self.last_state = body
        self.last_write = time.time()
        state["updated"] = self.last_write
        tmp = self.paths.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
        os.replace(tmp, self.paths.state)

    # ---- lifecycle
    def start_all(self) -> None:
        self.error = ""
        self.db_ready = False
        self.select_ports()
        self.write_runtime_files()
        self.init_cluster()
        for s in self.services.values():
            s.next_start = 0.0
            s.crash_times.clear()
            s.state = "stopped" if s.enabled else "disabled"
        log.info("starting services (hub :%s, TeslaMate :%s, MQTT :%s, Postgres socket %s)",
                 self.ports["hub"], self.ports["teslamate"], self.ports["mqtt"], self.sockdir)

    def stop_all(self) -> None:
        for name in ("hub", "teslamate", "mosquitto", "postgres"):
            self.services[name].stop()
        self.db_ready = False
        if self.caffeinate is not None and self.caffeinate.poll() is None:
            self.caffeinate.terminate()
        self.caffeinate = None
        self.children.clear()
        (self.paths.run / "children.json").unlink(missing_ok=True)

    def request_stop(self, *_: object) -> None:
        self.stopping = True
        self.wake.set()

    def nap(self) -> float:
        """How long the loop may sleep: briefly while anything starts, stops or
        retries; longer once everything has settled. A signal cuts it short."""
        settled = ("running", "stopped", "disabled")
        if self.error or any(x.state not in settled for x in self.services.values()):
            return BUSY_NAP_S
        return STEADY_NAP_S

    def poke(self, *_: object) -> None:
        """SIGCHLD (a service exited) or SIGUSR1 (the app changed control.json)."""
        self.wake.set()

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGHUP, self.request_restart)
        signal.signal(signal.SIGUSR1, self.poke)
        signal.signal(signal.SIGCHLD, self.poke)
        prepared = False
        active = False
        while not self.stopping:
            now = time.time()
            if not prepared:
                try:
                    self.prepare()
                    prepared = True
                    self.error = ""
                except Exception as e:  # noqa: BLE001 — stay up and report it; retry later
                    log.exception("cannot prepare the data directory")
                    self.error = str(e)
                    self.write_state(force=True)
                    self.wake.wait(30)
                    self.wake.clear()
                    continue
            if self.parent_pid and os.getppid() != self.parent_pid:
                log.info("parent app exited; shutting down")
                break
            restart = self.read_control() or self.hup
            self.hup = False
            if restart and active:
                log.info("restart requested")
                self.stop_all()
                active = False
            if self.desired == "running" and not active:
                try:
                    self.cfg, _ = ensure_config(self.paths.config)
                    self.start_all()
                    active = True
                except Exception as e:  # noqa: BLE001
                    log.exception("cannot start services")
                    self.error = str(e)
                    self.write_state(force=True)
                    self.wake.wait(30)
                    self.wake.clear()
                    continue
            elif self.desired == "stopped" and active:
                log.info("stopping services (requested by the app)")
                self.stop_all()
                active = False
            if active:
                for name in ("postgres", "mosquitto", "teslamate", "hub"):
                    if self.stopping:
                        break
                    self.services[name].tick(time.time())
                self.keep_awake_tick(now)
            self.write_state()
            self.wake.wait(self.nap())
            self.wake.clear()
        log.info("shutting down")
        self.stop_all()
        for x in self.services.values():
            x.state = "stopped" if x.enabled else "disabled"
        with contextlib.suppress(Exception):
            self.write_state(force=True, final=True)
        return 0

    def request_restart(self, *_: object) -> None:
        self.hup = True
        self.wake.set()


# --------------------------------------------------------------------------- entry

def main() -> int:
    ap = argparse.ArgumentParser(description="OhmyTesla service supervisor")
    ap.add_argument("--runtime", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="the bundle's Contents/Resources/runtime directory")
    ap.add_argument("--exit-with-parent", type=int, default=None, metavar="PID",
                    help="shut down when this process exits (used when the app runs us as a child)")
    ap.add_argument("--print-config", action="store_true", help="show resolved paths and exit")
    a = ap.parse_args()

    paths = Paths(a.runtime.resolve())
    paths.logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(paths.logs / "supervisor.log", maxBytes=5 * 1024 * 1024,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.addHandler(fh)
    if sys.stderr.isatty() or a.exit_with_parent:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    root.setLevel(logging.INFO)

    if a.print_config:
        for k, v in vars(paths).items():
            print(f"{k:12} {v}")
        return 0

    sup = Supervisor(paths, parent_pid=a.exit_with_parent)
    if not sup.acquire_lock():
        log.info("another supervisor is already running for %s; exiting", paths.data)
        return 0
    log.info("OhmyTesla supervisor %s starting (runtime %s, data %s)", os.getpid(), paths.runtime, paths.data)
    try:
        return sup.run()
    finally:
        log.info("supervisor exited")


if __name__ == "__main__":
    sys.exit(main())
