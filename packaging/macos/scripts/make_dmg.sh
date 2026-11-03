#!/bin/bash
# OhmyTesla-<version>-arm64.dmg: one window with the app on the left, an /Applications
# link on the right and an arrow between them (dmg/background.html, rendered by
# render_dmg_background.py). The background also says how to allow the first launch:
# the app is ad-hoc signed, not notarized. dmgbuild writes the window's layout straight
# into the image — no Finder scripting, so it works headless; it is a build-time tool,
# pinned by hash in dmg/requirements.txt.
source "$(dirname "$0")/lib.sh"

APP="$BUILD/OhmyTesla.app"
[[ -d $APP ]] || die "build the app first (build_app.sh)"
codesign --verify --deep --strict "$APP" || die "app signature is not valid"

REQ="$MACOS_DIR/dmg/requirements.txt"
VENV="$BUILD/dmgbuild"
if ! stamp_ok "$VENV" "$(sha256_of "$REQ")"; then
  log "dmg: installing dmgbuild (dmg/requirements.txt)"
  rm -rf "$VENV"
  /usr/bin/python3 -m venv "$VENV"
  "$VENV/bin/python3" -m pip install --quiet --disable-pip-version-check --no-cache-dir \
    --require-hashes --only-binary=:all: -r "$REQ"
  stamp_set "$VENV" "$(sha256_of "$REQ")"
fi

DMG="$DIST/OhmyTesla-$OHMYTESLA_VERSION-arm64.dmg"
mkdir -p "$DIST"
log "dmg: creating $DMG"
rm -f "$DMG"
# background.png: dmgbuild joins it with background@2x.png beside it into one HiDPI image
"$VENV/bin/dmgbuild" -s "$MACOS_DIR/dmg/settings.py" \
  -D app="$APP" -D icon="$APP/Contents/Resources/AppIcon.icns" \
  -D background="$MACOS_DIR/dmg/background.png" \
  "OhmyTesla $OHMYTESLA_VERSION" "$DMG" >/dev/null
hdiutil verify -quiet "$DMG"
SIZE=$(du -h "$DMG" | awk '{print $1}')
SHA=$(sha256_of "$DMG")
printf '%s  %s\n' "$SHA" "$(basename "$DMG")" > "$DMG.sha256"
log "dmg: $DMG ($SIZE, sha256 $SHA)"
