#!/bin/bash
# TeslaMate v4.4.0 as a self-contained, relocatable Elixir release.
# Mirrors the builder stage of the upstream Dockerfile. Runs on the Erlang/OTP
# built by build_otp.sh (macOS 13 target, static OpenSSL), so the release's
# ERTS — which `mix release` copies from the running OTP — needs nothing but
# the OS. Elixir (OS-independent .beam files) comes from Homebrew.
source "$(dirname "$0")/lib.sh"

[[ -x $OUT/otp/bin/erl ]] || die "build Erlang/OTP first (build_otp.sh)"
export PATH="$OUT/otp/bin:$PATH"

DEST="$OUT/teslamate"
OTP=$(erl -noshell -eval 'io:format("~s",[erlang:system_info(otp_release)]), halt().')
ELIXIR=$(elixir -e 'IO.write(System.version())')
KEY="$TESLAMATE_COMMIT otp$OTP_VERSION-src elixir$ELIXIR r4"

if stamp_ok "$DEST" "$KEY"; then
  log "teslamate: cached ($KEY)"
else
  [[ $OTP == "$EXPECT_OTP_MAJOR" ]] || warn "OTP $OTP (TeslaMate $TESLAMATE_TAG is built upstream with OTP $EXPECT_OTP_MAJOR)"
  [[ $ELIXIR == $EXPECT_ELIXIR_MINOR.* ]] || warn "Elixir $ELIXIR (upstream uses $EXPECT_ELIXIR_MINOR.x)"
  command -v node >/dev/null || die "node is required to build TeslaMate's assets"

  TM_SRC="$SRC/teslamate"
  git_checkout "$TESLAMATE_REPO" "$TESLAMATE_TAG" "$TESLAMATE_COMMIT" "$TM_SRC"

  # Keep hex/rebar/npm state inside the build tree instead of ~/.mix, ~/.hex, ~/.npm
  export MIX_ENV=prod
  export MIX_HOME="$CACHE/mix" HEX_HOME="$CACHE/hex"
  export npm_config_cache="$CACHE/npm"
  export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

  cd "$TM_SRC/elixir"
  rm -rf _build          # anything compiled by another OTP build
  log "teslamate: mix deps (hex packages are verified against mix.lock)"
  mix local.rebar --force >/dev/null
  mix local.hex --force >/dev/null
  mix deps.get --only prod
  mix deps.compile

  log "teslamate: assets (npm ci verifies package-lock integrity hashes)"
  npm ci --prefix ./assets --progress=false --no-audit --no-fund --loglevel=error
  mix assets.deploy

  log "teslamate: compile + release (include_erts: true is the mix default)"
  mix compile
  rm -rf "$DEST"
  mix release --overwrite --path "$DEST/release"
  cp "$TM_SRC/LICENSE" "$TM_SRC/NOTICE" "$DEST/release/"
  stamp_set "$DEST" "$KEY"
fi

# ---- post-processing (idempotent, runs every time) -------------------------
REL="$DEST/release"
# Developer tools and the non-relocatable OTP boot scripts are never used by
# `bin/teslamate start|eval`; `start` even hard-codes the Homebrew ROOTDIR.
for f in start start.src start_erl.src erl.src dialyzer typer ct_run erlc yielding_c_fun; do
  rm -f "$REL"/erts-*/bin/"$f"
done
rm -f "$REL"/lib/crypto-*/priv/lib/otp_test_engine.so

# Safety net: bundle and re-link anything that still points outside the release
# (nothing should: the crypto NIF has OpenSSL linked in statically).
"$HOST_PY" -I "$SCRIPTS/relocate.py" fix "$REL" --libdir "lib/_dylibs"
"$HOST_PY" -I "$SCRIPTS/relocate.py" verify "$REL" --max-minos "$MACOS_MIN"
log "teslamate: ready -> $REL"
