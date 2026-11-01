#!/bin/bash
# Build OhmyTesla.app and OhmyTesla-<version>-arm64.dmg for Apple Silicon Macs.
#
#   ./build.sh            build (components are cached in build/; re-runs are quick)
#   ./build.sh --relock   re-resolve services/hub/requirements.txt into
#                         python-requirements.lock first (after hub dependency changes)
#   ./build.sh --no-dmg   stop after OhmyTesla.app
#   ./build.sh --clean    delete build/ and dist/ first (downloads included)
#
# Everything lands in packaging/macos/build/ (scratch) and packaging/macos/dist/
# (the DMG). Every download is pinned by version + SHA-256 in versions.sh.
# Toolchain (build time only): Xcode, and from Homebrew:
#   brew install elixir cmake meson ninja pkgconf node
set -euo pipefail
cd "$(dirname "$0")"

RELOCK=0 DMG=1
for arg in "$@"; do
  case $arg in
    --relock) RELOCK=1 ;;
    --no-dmg) DMG=0 ;;
    --clean) rm -rf build dist ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

source scripts/lib.sh

[[ $(uname -m) == arm64 ]] || die "build on an Apple Silicon Mac (the bundle is arm64-only)"
xcode-select -p >/dev/null 2>&1 || die "Xcode (or the Command Line Tools) is required"
missing=()
for tool in swift git curl erl elixir mix node npm cmake meson ninja pkg-config; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if ((${#missing[@]})); then
  die "missing build tools: ${missing[*]} — brew install elixir cmake meson ninja pkgconf node"
fi

mkdir -p "$BUILD/logs"
step() {
  local name=$1; shift
  local t0=$SECONDS
  log "[$name]"
  if ! "$@" > "$BUILD/logs/$name.log" 2>&1; then
    tail -40 "$BUILD/logs/$name.log" >&2
    die "$name failed — full log: $BUILD/logs/$name.log"
  fi
  printf '    done in %ss\n' $((SECONDS - t0)) >&2
}

step postgresql  bash scripts/build_postgres.sh
if ((RELOCK)); then
  step python    bash scripts/build_python.sh --relock
else
  step python    bash scripts/build_python.sh
fi
step mosquitto   bash scripts/build_mosquitto.sh
step ffmpeg      bash scripts/build_ffmpeg.sh
step otp         bash scripts/build_otp.sh
step teslamate   bash scripts/build_teslamate.sh
step stage       bash scripts/stage_runtime.sh
step app         bash scripts/build_app.sh
((DMG)) && step dmg bash scripts/make_dmg.sh

echo
log "OhmyTesla.app: $BUILD/OhmyTesla.app ($(du -sh "$BUILD/OhmyTesla.app" | awk '{print $1}'))"
if ((DMG)); then
  out="$DIST/OhmyTesla-$OHMYTESLA_VERSION-arm64.dmg"
  log "DMG: $out"
  log "     $(du -h "$out" | awk '{print $1}'), sha256 $(cut -d' ' -f1 "$out.sha256")"
fi
