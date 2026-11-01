#!/bin/bash
# Minimal Mosquitto broker: no TLS, no cJSON, no websockets, no docs, no
# clients/apps/plugins. The result links only libSystem.
source "$(dirname "$0")/lib.sh"

DEST="$OUT/mosquitto"
KEY="$MOSQUITTO_VERSION $MOSQUITTO_SHA256 r1"
if stamp_ok "$DEST" "$KEY"; then log "mosquitto: cached"; exit 0; fi

tarball=$(fetch mosquitto "$MOSQUITTO_URL" "$MOSQUITTO_SHA256")
S="$SRC/mosquitto"
extract "$tarball" "$S"
c_env

rm -rf "$S/_build" "$DEST"
cmake -S "$S" -B "$S/_build" -G "Unix Makefiles" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$DEST" \
  -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DCMAKE_OSX_DEPLOYMENT_TARGET="$MACOS_MIN" \
  -DCMAKE_FIND_FRAMEWORK=NEVER \
  -DCMAKE_IGNORE_PREFIX_PATH="$BREW_PREFIX" \
  -DCMAKE_PROJECT_INCLUDE="$SCRIPTS/mosquitto-no-tls.cmake" \
  -DWITH_TLS=OFF -DWITH_TLS_PSK=OFF -DWITH_EC=OFF \
  -DWITH_CJSON=OFF -DWITH_WEBSOCKETS=OFF -DDOCUMENTATION=OFF \
  -DWITH_CLIENTS=OFF -DWITH_APPS=OFF -DWITH_PLUGINS=OFF \
  -DWITH_SRV=OFF -DWITH_DLT=OFF -DUSE_LIBWRAP=OFF \
  -DWITH_STATIC_LIBRARIES=OFF -DWITH_BROKER=ON -DWITH_LIB_CPP=OFF \
  -DINC_MEMTRACK=OFF
cmake --build "$S/_build" --target mosquitto -j "$JOBS"

mkdir -p "$DEST/sbin"
install -m 755 "$S/_build/src/mosquitto" "$DEST/sbin/mosquitto"
cp "$S/LICENSE.txt" "$S/epl-v20" "$S/edl-v10" "$DEST/"
strip -x "$DEST/sbin/mosquitto" 2>/dev/null || true
codesign --force --sign - --timestamp=none "$DEST/sbin/mosquitto"
otool -L "$DEST/sbin/mosquitto"
stamp_set "$DEST" "$KEY"
log "mosquitto: done -> $DEST/sbin/mosquitto"
