#!/bin/bash
# Assemble Contents/Resources/runtime from the component builds:
#
#   runtime/
#     supervisor/supervisor.py      packaging/macos/runtime/supervisor.py
#     python/                       CPython + hub dependencies (build_python.sh)
#     hub/app/                      services/hub/app — copied fresh on every build
#     postgresql/ mosquitto/ teslamate/ ffmpeg/
#     fonts/                        Noto Sans CJK SC (OFL)
#     etc/openssl.cnf               keeps bundled libcrypto away from Homebrew's config
#     licenses/                     one directory per component
#     VERSIONS.json
source "$(dirname "$0")/lib.sh"

R="${1:-$BUILD/stage/runtime}"
log "staging runtime -> $R"
rm -rf "$R"
mkdir -p "$R"/{supervisor,hub,fonts,etc,licenses}

cp "$MACOS_DIR/runtime/supervisor.py" "$R/supervisor/supervisor.py"

ditto "$OUT/python/python" "$R/python"
PY_MINOR=$(cat "$OUT/python/minor")

# The hub is under active development: ship the working tree as it is right
# now (tracked and untracked files) — but only what git would publish. Files
# .gitignore keeps out (e.g. static/vehicle/cybertruck.png, a Tesla-copyrighted
# render for the owner's own install) must never end up in a DMG for others.
( cd "$REPO_ROOT/services/hub/app" && git ls-files --cached --others --exclude-standard ) \
  | grep -v -e '__pycache__/' -e '\.pyc$' -e '\.DS_Store$' | sort > "$OUT/hub-files.txt"
mkdir -p "$R/hub/app"
rsync -a --files-from="$OUT/hub-files.txt" "$REPO_ROOT/services/hub/app/" "$R/hub/app/"
# --files-from never deletes; drop whatever an earlier build left that isn't listed.
( cd "$R/hub/app" && find . -type f | sed 's|^\./||' | sort ) | comm -13 "$OUT/hub-files.txt" - \
  | while IFS= read -r f; do rm -f "$R/hub/app/$f"; done
cp "$REPO_ROOT/services/hub/requirements.txt" "$R/hub/requirements.txt"

ditto "$OUT/postgresql/pg" "$R/postgresql"
mkdir -p "$R/mosquitto/sbin"; cp "$OUT/mosquitto/sbin/mosquitto" "$R/mosquitto/sbin/"
ditto "$OUT/teslamate/release" "$R/teslamate"
mkdir -p "$R/ffmpeg/bin"; cp "$OUT/ffmpeg/bin/ffmpeg" "$OUT/ffmpeg/bin/ffprobe" "$R/ffmpeg/bin/"

for f in Regular Bold; do
  sha_var="NOTO_$(tr '[:lower:]' '[:upper:]' <<<"$f")_SHA256"
  cp "$(fetch noto-cjk "$NOTO_BASE/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-$f.otf" "${!sha_var}")" "$R/fonts/"
done

cat > "$R/etc/openssl.cnf" <<'EOF'
# Minimal OpenSSL configuration for the libcrypto copies bundled with
# OhmyTesla (Erlang crypto NIF, Python wheels). Its only job is to stop them
# from reading the build machine's OPENSSLDIR (e.g. Homebrew's openssl.cnf).
openssl_conf = openssl_init

[openssl_init]
providers = provider_sect

[provider_sect]
default = default_sect

[default_sect]
activate = 1
EOF

# ---- licences --------------------------------------------------------------
L="$R/licenses"
mkdir -p "$L"/{ohmytesla,postgresql,mosquitto,teslamate,erlang-otp,elixir,openssl,python,ffmpeg,noto-sans-cjk}
cp "$REPO_ROOT/LICENSE" "$L/ohmytesla/LICENSE"
cp "$OUT/postgresql/COPYRIGHT" "$L/postgresql/COPYRIGHT"
cp "$OUT/otp/LICENSE.txt" "$L/erlang-otp/LICENSE.txt"
cp "$OUT/otp/OPENSSL-LICENSE.txt" "$L/openssl/LICENSE.txt"
cp "$BREW_PREFIX/opt/elixir/LICENSE" "$L/elixir/LICENSE" 2>/dev/null || true
cp "$OUT/mosquitto/LICENSE.txt" "$OUT/mosquitto/epl-v20" "$OUT/mosquitto/edl-v10" "$L/mosquitto/"
mv "$R/teslamate/LICENSE" "$R/teslamate/NOTICE" "$L/teslamate/"
cp "$OUT/python/python/lib/python$PY_MINOR/LICENSE.txt" "$L/python/LICENSE.txt" 2>/dev/null || true
ditto "$OUT/ffmpeg/licenses" "$L/ffmpeg"
cp "$(fetch noto-cjk "$NOTO_BASE/LICENSE" "$NOTO_LICENSE_SHA256" LICENSE)" "$L/noto-sans-cjk/LICENSE"
( cd "$R/python/lib/python$PY_MINOR/site-packages" &&
  for d in *.dist-info; do
    mkdir -p "$L/python/site-packages/$d"
    find "$d" -maxdepth 3 -type f \( -iname 'LICEN[SC]E*' -o -iname 'COPYING*' -o -iname 'NOTICE*' -o -name METADATA \) \
      -exec cp {} "$L/python/site-packages/$d/" \;
  done )

# ---- versions ----------------------------------------------------------------
HUB_REV=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
HUB_DIRTY=$([[ -n $(git -C "$REPO_ROOT" status --porcelain -- services/hub 2>/dev/null) ]] && echo true || echo false)
PY_FULL=$("$R/python/bin/python3" -I -B -c 'import platform; print(platform.python_version())')
cat > "$R/VERSIONS.json" <<EOF
{
  "ohmytesla": "$OHMYTESLA_VERSION",
  "hub_git_commit": "$HUB_REV",
  "hub_working_tree_modified": $HUB_DIRTY,
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "postgresql": "$PG_VERSION",
  "erlang_otp": "$OTP_VERSION (OpenSSL $OPENSSL_VERSION, static)",
  "elixir": "$(elixir -e 'IO.write(System.version())' 2>/dev/null || echo unknown)",
  "teslamate": "$TESLAMATE_TAG ($TESLAMATE_COMMIT)",
  "erts": "$(basename "$(ls -d "$R"/teslamate/erts-*)")",
  "mosquitto": "$MOSQUITTO_VERSION",
  "python": "$PY_FULL",
  "ffmpeg": "$FFMPEG_VERSION",
  "x264": "$X264_COMMIT",
  "libass": "$LIBASS_VERSION",
  "noto_sans_cjk": "$NOTO_TAG"
}
EOF
log "runtime staged ($(du -sh "$R" | awk '{print $1}'))"
