"""`omt` 启动脚本的健壮性（审计 data-api F16 + 恢复流程）。

只用标准库 + bash，宿主机上就能跑：
1. 没有 .env、.env 里缺 *_PORT 行时不再静默退出（以前 exit 2 一个字都不说）；
2. .env 的读取规则与 docker compose 一致：值里的 `=`、`#` 保留，" #" 才是注释，
   引号去掉，`export ` 前缀、Windows 换行、重复键取最后一个；
3. （给了 OMT_TEST_PG 才跑）`omt restore` 在一台**新**服务器上能恢复：先建好
   omt_mcp_readonly 角色（dump 里有 GRANT 给它），psql 用 ON_ERROR_STOP=1 +
   --single-transaction —— 出错就停、整体回滚，不留半个库。docker 用一个小垫片
   代替：`docker compose exec -T database <cmd>` 直接在本机跑 <cmd>（连 OMT_TEST_PG）。
   OMT_TEST_PG_DISPOSABLE=1 时会先删掉服务器上的 omt_mcp_readonly 角色，模拟全新的库。

跑法：python3 services/hub/tests/test_data_omt.py
"""

import gzip
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
OMT = os.path.join(ROOT, "omt")
FAILS: list[str] = []


def chk(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + str(extra)[:300]) if extra else ''}")
    if not cond:
        FAILS.append(name)


def envget_script() -> str:
    src = open(OMT, encoding="utf-8").read()
    start = src.index("envget() {")
    end = src.index("port() {")
    return src[start:end]


def envget(env_text: str | None, key: str) -> tuple[int, str]:
    d = tempfile.mkdtemp()
    if env_text is not None:
        with open(os.path.join(d, ".env"), "w", encoding="utf-8", newline="") as f:
            f.write(env_text)
    script = "set -euo pipefail\n" + envget_script() + f'\nprintf "[%s]" "$(envget {key})"\n'
    p = subprocess.run(["bash", "-c", script], cwd=d, capture_output=True, text=True, check=False)
    shutil.rmtree(d, ignore_errors=True)
    return p.returncode, p.stdout


def omt_copy(env_text: str | None) -> str:
    d = tempfile.mkdtemp()
    shutil.copy(OMT, os.path.join(d, "omt"))
    if env_text is not None:
        with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
            f.write(env_text)
    return d


SHIM = """#!/bin/bash
# 假 docker：compose exec -T database <cmd> → 本机直接跑 <cmd>（连 PGHOST/PGPORT）
if [ "$1" = "compose" ] && [ "$2" = "exec" ]; then
  shift 2; [ "$1" = "-T" ] && shift; shift; exec "$@"
fi
case "$1" in info|compose) exit 0 ;; inspect) echo unknown; exit 0 ;; esac
exit 0
"""


def main() -> int:
    if not os.path.isfile(OMT):
        # Inside the hub image only services/hub exists; the script lives at the repo root.
        print("⏭  skipped: no ./omt here (run this one on the host)")
        return 0
    print("shell 语法")
    chk("bash -n omt", subprocess.run(["bash", "-n", OMT], check=False).returncode == 0)

    print(".env 读取规则（与 docker compose 一致）")
    cases = [
        ("OHMYTESLA_PASSWORD=pa=ss#word 1\n", "OHMYTESLA_PASSWORD", "[pa=ss#word 1]"),
        ("OHMYTESLA_PORT=8800 # the port\n", "OHMYTESLA_PORT", "[8800]"),
        ('OHMYTESLA_PASSWORD="quoted # kept"\n', "OHMYTESLA_PASSWORD", "[quoted # kept]"),
        ("OHMYTESLA_PASSWORD='single=quoted'\n", "OHMYTESLA_PASSWORD", "[single=quoted]"),
        ("export OHMYTESLA_PORT=9900\r\n", "OHMYTESLA_PORT", "[9900]"),
        ("OHMYTESLA_PORT=1\nOHMYTESLA_PORT=2\n", "OHMYTESLA_PORT", "[2]"),
        ("TM_DB_USER= spaced  \n", "TM_DB_USER", "[spaced]"),
        ("OTHER=1\n", "OHMYTESLA_PORT", "[]"),
        (None, "OHMYTESLA_PORT", "[]"),
    ]
    for text, key, want in cases:
        rc, out = envget(text, key)
        chk(f"{key} from {text!r}", rc == 0 and out == want, (rc, out))

    print("没有 .env / 缺 *_PORT 行时不再静默退出")
    d = omt_copy(None)
    p = subprocess.run(["bash", "./omt", "help"], cwd=d, capture_output=True, text=True, check=False)
    chk("没有 .env：./omt help 正常打印菜单", p.returncode == 0 and "start everything" in p.stdout, p.returncode)
    shim_dir = tempfile.mkdtemp()
    with open(os.path.join(shim_dir, "docker"), "w") as f:
        f.write(SHIM)
    os.chmod(os.path.join(shim_dir, "docker"), 0o755)
    env = {**os.environ, "PATH": f"{shim_dir}:{os.environ['PATH']}"}
    p = subprocess.run(["bash", "./omt", "up"], cwd=d, capture_output=True, text=True, env=env, check=False)
    chk("没有 .env：./omt up 说清楚 .env 缺了", p.returncode == 1 and ".env is missing" in p.stderr,
        (p.returncode, p.stderr[-200:]))
    d2 = omt_copy("TM_ENCRYPTION_KEY=abc\nTM_DB_PASS=x\n")
    p = subprocess.run(["bash", "./omt", "help"], cwd=d2, capture_output=True, text=True, check=False)
    chk("缺 *_PORT 行：./omt help 正常", p.returncode == 0 and "start everything" in p.stdout, p.returncode)

    print("恢复流程是'遇错即停'的")
    src = open(OMT, encoding="utf-8").read()
    body = src[src.index("cmd_restore() {"):src.index("cmd_grafana_password")]
    chk("恢复用 ON_ERROR_STOP=1 + --single-transaction",
        "ON_ERROR_STOP=1" in body and "--single-transaction" in body)
    chk("恢复前先建 omt_mcp_readonly（NOLOGIN）",
        body.index("omt_mcp_readonly") < body.index('gzip -dc "$dir/teslamate.sql.gz"')
        and "NOLOGIN" in body)
    chk("恢复前快照失败不会把整个恢复带走（子 shell）", "( cmd_backup )" in body)

    dsn = os.environ.get("OMT_TEST_PG", "")
    if dsn:
        restore_flow(dsn, shim_dir)
    else:
        print("  ⏭  真实恢复流程跳过：设 OMT_TEST_PG=postgresql://user:pw@host:port/postgres 才跑")

    print()
    print("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}")
    return 1 if FAILS else 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *a):
        pass


def restore_flow(dsn: str, shim_dir: str) -> None:
    print("真实恢复：新服务器（没有 omt_mcp_readonly 角色）上恢复一份带 GRANT 的备份")
    u = urlparse(dsn)
    dbname = f"omt_restore_test_{os.getpid()}"
    port = _free_port()
    srv = http.server.HTTPServer(("127.0.0.1", port), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {**os.environ, "PATH": f"{shim_dir}:{os.environ['PATH']}",
           "PGHOST": u.hostname or "127.0.0.1", "PGPORT": str(u.port or 5432),
           "PGPASSWORD": u.password or ""}

    def psql(sql: str, db: str = "postgres") -> subprocess.CompletedProcess:
        return subprocess.run(["psql", "-U", u.username or "postgres", "-d", db, "-Atc", sql],
                              capture_output=True, text=True, env=env, check=False)

    if os.environ.get("OMT_TEST_PG_DISPOSABLE") == "1":
        psql("DROP ROLE IF EXISTS omt_mcp_readonly")
    fresh = psql("SELECT count(*) FROM pg_roles WHERE rolname = 'omt_mcp_readonly'").stdout.strip() == "0"
    d = omt_copy(f"OHMYTESLA_PORT={port}\nTM_DB_USER={u.username}\nTM_DB_NAME={dbname}\n"
                 "TM_ENCRYPTION_KEY=abc\nTM_DB_PASS=x\nOHMYTESLA_PASSWORD=x\n")

    def backup(sql: str) -> str:
        b = os.path.join(d, "backups", "20260101-000000")
        os.makedirs(b, exist_ok=True)
        with gzip.open(os.path.join(b, "teslamate.sql.gz"), "wt") as f:
            f.write(sql + "\n--\n-- PostgreSQL database dump complete\n--\n")
        return b

    good = ("CREATE TABLE public.cars (id smallint PRIMARY KEY, vin text NOT NULL);\n"
            "INSERT INTO public.cars VALUES (1, '7G2CEHED0RA000000');\n"
            "GRANT SELECT ON TABLE public.cars TO omt_mcp_readonly;\n")
    b = backup(good)
    p = subprocess.run(["bash", "./omt", "restore", b], cwd=d, input="restore\n",
                       capture_output=True, text=True, env=env, check=False)
    chk(f"恢复成功（{'全新服务器' if fresh else '角色已存在'}）",
        p.returncode == 0 and "Restored from" in p.stdout, (p.returncode, p.stdout[-300:], p.stderr[-300:]))
    chk("数据到位", psql("SELECT vin FROM cars", dbname).stdout.strip() == "7G2CEHED0RA000000")
    chk("只读角色存在", psql("SELECT count(*) FROM pg_roles WHERE rolname = 'omt_mcp_readonly'").stdout.strip() == "1")

    bad = ("CREATE TABLE public.cars (id smallint PRIMARY KEY, vin text NOT NULL);\n"
           "INSERT INTO public.cars VALUES (1, 'X');\n"
           "THIS IS NOT SQL;\n"
           "CREATE TABLE public.never (id int);\n")
    b = backup(bad)
    p = subprocess.run(["bash", "./omt", "restore", b], cwd=d, input="restore\n",
                       capture_output=True, text=True, env=env, check=False)
    chk("坏备份：恢复以非零退出、并说明已回滚", p.returncode != 0 and "rolled back" in p.stderr,
        (p.returncode, p.stderr[-300:]))
    chk("坏备份：没有留下半个库（单事务回滚）",
        psql("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'", dbname).stdout.strip() == "0")
    psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    srv.shutdown()
    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
