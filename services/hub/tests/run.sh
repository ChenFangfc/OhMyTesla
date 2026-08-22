#!/usr/bin/env bash
# OhmyTesla hub 测试入口。
#
# 为什么不是 pytest：这个仓库里没有 pytest，也不想为了跑几个纯函数测试
# 往生产镜像里塞测试依赖。每个测试文件自己 `python3 xxx.py` 就能跑，
# 退出码就是结果。CI 和本地是同一条命令。
#
#   ./services/hub/tests/run.sh            # 宿主机（只跑不依赖运行时的）
#   ./services/hub/tests/run.sh --docker   # 容器内（能跑需要 aiosqlite 的）
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'
TESTS_DIR="services/hub/tests"
MODE="${1:-host}"

# 需要 app 包依赖（aiosqlite 等）的测试只能在容器里跑
NEEDS_RUNTIME="test_migrations.py test_sentry_escalation.py test_automation.py test_compose_layouts.py"
# 需要服务真的在跑的契约测试（只用标准库，宿主机上跑更方便）
NEEDS_SERVER="test_teslamateapi_contract.py"

# 契约测试要凭据。从 .env 读，读不到就跳过而不是判失败 ——
# 本地开发者可能根本没起服务。
export OMT_URL="${OMT_URL:-http://localhost:8800}"
if [ -z "${OMT_PW:-}" ] && [ -f .env ]; then
  OMT_PW=$(grep -E '^OHMYTESLA_PASSWORD=' .env | head -1 | cut -d= -f2- | tr -d ' ')
  export OMT_PW
fi

run_host() {
  local failed=0
  for f in "$TESTS_DIR"/test_*.py; do
    local base; base=$(basename "$f")
    if [[ " $NEEDS_RUNTIME " == *" $base "* ]]; then
      echo "${DIM}— $base (需要运行时依赖，用 --docker 跑)${OFF}"
      continue
    fi
    if [[ " $NEEDS_SERVER " == *" $base "* ]]; then
      if ! curl -fsS "$OMT_URL/healthz" >/dev/null 2>&1; then
        echo "${DIM}— $base (服务没在跑，跳过)${OFF}"
        continue
      fi
    fi
    echo "▸ $base"
    if python3 "$f"; then :; else failed=1; fi
  done
  return $failed
}

run_docker() {
  local c=ohmytesla-ohmytesla-1
  docker inspect "$c" >/dev/null 2>&1 || { echo "${RED}容器 $c 没在跑${OFF}"; return 1; }
  docker cp "$TESTS_DIR" "$c:/opt/ohmytesla/tests" >/dev/null || return 1
  local failed=0
  for f in "$TESTS_DIR"/test_*.py; do
    local base; base=$(basename "$f")
    if [[ " $NEEDS_SERVER " == *" $base "* ]]; then
      # 契约测试打的是宿主机映射出来的端口，在容器里跑不到
      echo "▸ $base"
      if python3 "$f"; then :; else failed=1; fi
      continue
    fi
    echo "▸ $base ${DIM}(容器)${OFF}"
    if docker exec "$c" python3 "/opt/ohmytesla/tests/$base"; then :; else failed=1; fi
  done
  return $failed
}

if [ "$MODE" = "--docker" ]; then run_docker; rc=$?; else run_host; rc=$?; fi

echo
if [ $rc -eq 0 ]; then echo "${GREEN}测试通过${OFF}"; else echo "${RED}有测试失败${OFF}"; fi
exit $rc
