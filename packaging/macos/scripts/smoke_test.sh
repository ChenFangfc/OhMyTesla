#!/bin/bash
# End-to-end check of a built DMG, with throwaway data (never your real install):
#
#   scripts/smoke_test.sh [path/to/OhmyTesla-x.y.z-arm64.dmg]
#
# Mounts the DMG, copies the app out, checks signatures, linkage and minimum
# macOS of every Mach-O, starts the whole stack through the LaunchAgent entry
# point (`OhmyTesla --supervisor`) with OHMYTESLA_HOME in a temp dir, probes
# every service, shuts down with SIGTERM and removes everything it created.
# Ports are read from state.json, so a running OhmyTesla is not disturbed.
source "$(dirname "$0")/lib.sh"

DMG="${1:-$DIST/OhmyTesla-$OHMYTESLA_VERSION-arm64.dmg}"
[[ -f $DMG ]] || die "no DMG at $DMG"
# No trailing slash in the base: macOS's TMPDIR ends in "/", and a "T//…" path
# in $A matches only processes started with that literal spelling.
_tmp=${SMOKE_TMP:-${TMPDIR:-/tmp}}
WORK=$(mktemp -d "${_tmp%/}/ohmytesla-smoke.XXXXXX")
SUP=""
FAILED=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILED=1; }
check() { local what=$1; shift; if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi; }

cleanup() {
  if [[ -n $SUP ]] && kill -0 "$SUP" 2>/dev/null; then kill -TERM "$SUP"; wait "$SUP" 2>/dev/null || true; fi
  hdiutil detach "$WORK/mnt" -quiet 2>/dev/null || true
  rm -rf "$WORK"
  # long temp paths make the supervisor put the Postgres socket here; only
  # removed when empty, i.e. when no real install is using it
  rmdir "${TMPDIR%/}/ohmytesla-$(id -u)" 2>/dev/null || true
}
trap cleanup EXIT

log "smoke test: $DMG"
mkdir -p "$WORK/mnt"
hdiutil attach -nobrowse -readonly -mountpoint "$WORK/mnt" "$DMG" >/dev/null
[[ -L $WORK/mnt/Applications ]] && pass "DMG has the drag-to-Applications link" || fail "DMG lacks the Applications link"
# the window make_dmg.sh lays out: Finder's view settings, the background, the volume icon
if [[ -f $WORK/mnt/.DS_Store && -f $WORK/mnt/.background.tiff && -f $WORK/mnt/.VolumeIcon.icns ]]; then
  pass "DMG window: layout, background and volume icon"
else
  fail "DMG window: missing .DS_Store, .background.tiff or .VolumeIcon.icns"
fi
ditto "$WORK/mnt/OhmyTesla.app" "$WORK/OhmyTesla.app"
hdiutil detach "$WORK/mnt" -quiet
A="$WORK/OhmyTesla.app"
R="$A/Contents/Resources/runtime"

check "codesign --verify --deep --strict" codesign --verify --deep --strict "$A"
if "$HOST_PY" -I "$SCRIPTS/relocate.py" verify "$A/Contents" --max-minos "$MACOS_MIN" > "$WORK/relocate.txt"; then
  pass "$(grep -o '[0-9]* Mach-O files' "$WORK/relocate.txt" | head -1): only bundle/OS dependencies, signed, minos <= $MACOS_MIN"
else
  fail "relocation check:"; grep PROBLEM "$WORK/relocate.txt" | head -20
fi

export OHMYTESLA_HOME="$WORK/home" OHMYTESLA_LOGS="$WORK/logs" OHMYTESLA_CACHES="$WORK/caches"
"$A/Contents/MacOS/OhmyTesla" --supervisor > "$WORK/supervisor.out" 2>&1 &
SUP=$!
state() { "$HOST_PY" -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' \
            "$OHMYTESLA_HOME/state.json" "$1" 2>/dev/null; }
t0=$SECONDS
until [[ $(state 'd["status"]') == running ]]; do
  [[ $(state 'd["status"]') == error ]] && break
  ((SECONDS - t0 > 300)) && break
  kill -0 "$SUP" 2>/dev/null || break
  sleep 1
done
if [[ $(state 'd["status"]') == running ]]; then
  pass "fresh install: all services running after $((SECONDS - t0))s ($(state "' '.join(k + '=' + v['state'] for k, v in sorted(d['services'].items()))"))"
else
  fail "stack did not come up: $(state 'd')"; tail -30 "$OHMYTESLA_LOGS"/*.log; exit 1
fi

HUB=$(state 'd["ports"]["hub"]'); TM=$(state 'd["ports"]["teslamate"]'); MQ=$(state 'd["ports"]["mqtt"]')
PGP=$(state 'd["ports"]["postgres"]'); SOCK=$(state 'd["postgres_socket_dir"]')
cfg() { sed -n "s/^$1=//p" "$OHMYTESLA_HOME/config.env"; }
PW=$(cfg OHMYTESLA_PASSWORD)
q() { PGPASSWORD=$(cfg TM_DB_PASS) "$R/postgresql/bin/psql" -X -q -h "$SOCK" -p "$PGP" -U teslamate -d teslamate -tAc "$1"; }

[[ $(stat -f %Lp "$OHMYTESLA_HOME/config.env") == 600 ]] && pass "config.env is 0600 with generated secrets" || fail "config.env permissions"
ext=$(q "select string_agg(extname, ',' order by extname) from pg_extension")
[[ $ext == *cube* && $ext == *earthdistance* ]] && pass "PostgreSQL $(q 'show server_version') extensions: $ext" || fail "extensions: $ext"
[[ -z $(q "show listen_addresses") ]] && pass "PostgreSQL: Unix socket only ($SOCK)" || fail "PostgreSQL listens on TCP"
mig=$(q "select count(*) from schema_migrations")
((mig > 0)) && pass "TeslaMate migrations applied: $mig (latest $(q 'select max(version) from schema_migrations'))" || fail "no TeslaMate migrations"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$TM/")
((code > 0 && code < 500)) && pass "TeslaMate web on 127.0.0.1:$TM answers ($code)" || fail "TeslaMate web: $code"
clients=$(lsof -nP -iTCP:"$MQ" -sTCP:ESTABLISHED 2>/dev/null | awk 'NR>1 && $1!="mosquitto"{print $1}' | sort -u | tr '\n' ' ')
[[ $clients == *beam* && $clients == *python* ]] && pass "Mosquitto on :$MQ, clients: $clients" || fail "MQTT clients: $clients"
[[ $(curl -s "http://127.0.0.1:$HUB/healthz") == *'"ok":true'* ]] && pass "hub /healthz ok" || fail "hub /healthz"
[[ $(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HUB/welcome") == 401 ]] && pass "hub /welcome without password: 401" || fail "hub served /welcome without auth"
code=$(curl -s -o "$WORK/welcome.html" -w '%{http_code}' -u "ohmytesla:$PW" "http://127.0.0.1:$HUB/welcome")
[[ $code == 200 ]] && pass "hub /welcome with the generated password: 200 $(grep -o '<title>[^<]*</title>' "$WORK/welcome.html")" || fail "hub /welcome with password: $code"
pids=$( (pgrep -f "$A/Contents/Resources/runtime" || true) | tr '\n' ',')
# lsof exits 1 when a process listens on nothing; that must not end the run.
wide=$( (lsof -nP -a -p "${pids%,}" -iTCP -sTCP:LISTEN 2>/dev/null || true) | awk 'NR>1 && $9 !~ /^127\.0\.0\.1:/{print $1, $9}')
[[ -z $wide ]] && pass "every TCP listener is bound to 127.0.0.1" || fail "listeners beyond loopback: $wide"
pgrep -x epmd >/dev/null && fail "epmd is running" || pass "no epmd (no Erlang distribution)"
cat > "$WORK/t.ass" <<'EOF'
[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 120

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Big,Noto Sans CJK SC,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,Big,,0,0,0,,哨兵模式
EOF
font=$(cd "$WORK" && env -i PATH="$R/ffmpeg/bin:/usr/bin:/bin" HOME="$HOME" \
  FONTCONFIG_FILE="$OHMYTESLA_HOME/fontconfig/fonts.conf" ffmpeg -hide_banner -v verbose -y \
  -f lavfi -i color=c=black:s=320x120:d=1 -vf ass=filename=t.ass -frames:v 1 overlay.png 2>&1 \
  | sed -n 's/.*fontselect: (Noto Sans CJK SC[^)]*) -> \([^,]*\),.*/\1/p' | head -1)
R_REAL=$(cd "$R" && pwd -P)   # ffmpeg reports /private/var/…, the real path of /var/…
[[ $font == "$R/fonts/"* || $font == "$R_REAL/fonts/"* ]] && pass "overlay font resolves to the bundled ${font##*/}" || fail "overlay font: ${font:-not found}"

t0=$SECONDS
kill -TERM "$SUP"; wait "$SUP" 2>/dev/null || true; SUP=""
left=$(pgrep -f "$A/Contents" || true)
[[ -z $left ]] && pass "SIGTERM: clean shutdown in $((SECONDS - t0))s, no processes left" || fail "processes left: $left"
grep -q "database system is shut down" "$OHMYTESLA_LOGS/postgres.log" && pass "PostgreSQL shut down cleanly (pg_ctl stop -m fast)" || fail "PostgreSQL shutdown not logged"

((FAILED)) && die "smoke test FAILED"
log "smoke test passed"
