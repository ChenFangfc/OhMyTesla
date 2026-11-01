#!/bin/bash
# OhmyTesla-<version>-arm64.dmg: the app, an /Applications link to drag it onto,
# and a short note about the first launch (the app is ad-hoc signed, not notarized).
source "$(dirname "$0")/lib.sh"

APP="$BUILD/OhmyTesla.app"
[[ -d $APP ]] || die "build the app first (build_app.sh)"
codesign --verify --deep --strict "$APP" || die "app signature is not valid"

DMG="$DIST/OhmyTesla-$OHMYTESLA_VERSION-arm64.dmg"
STAGE="$BUILD/dmg"
rm -rf "$STAGE"
mkdir -p "$STAGE" "$DIST"
ditto "$APP" "$STAGE/OhmyTesla.app"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/First launch.txt" <<'EOF'
OhmyTesla — first launch
========================

1. Drag OhmyTesla onto the Applications folder.

2. OhmyTesla is open-source and signed only "ad hoc" (no paid Apple Developer ID),
   so macOS blocks the first launch. Allow it once:

   macOS 13 / 14:  Control-click (right-click) OhmyTesla in Applications, choose Open,
                   then Open again.
   macOS 15 and later:  double-click OhmyTesla, close the warning, then open
                   System Settings › Privacy & Security, scroll down and click
                   "Open Anyway" next to OhmyTesla.
   Any version, from Terminal:
                   xattr -dr com.apple.quarantine /Applications/OhmyTesla.app

3. Choose "Run on this Mac" (installs the whole stack inside the app — no Docker) or
   "Connect to an existing OhmyTesla server".

Data:   ~/Library/Application Support/OhmyTesla   (config.env holds your portal password)
Logs:   ~/Library/Logs/OhmyTesla
More:   https://github.com/ChenFangfc/OhMyTesla/tree/main/packaging/macos
EOF

log "dmg: creating $DMG"
rm -f "$DMG"
hdiutil create -quiet -volname "OhmyTesla $OHMYTESLA_VERSION" -srcfolder "$STAGE" \
  -fs HFS+ -format ULMO -ov "$DMG"
hdiutil verify -quiet "$DMG"
rm -rf "$STAGE"
SIZE=$(du -h "$DMG" | awk '{print $1}')
SHA=$(sha256_of "$DMG")
printf '%s  %s\n' "$SHA" "$(basename "$DMG")" > "$DMG.sha256"
log "dmg: $DMG ($SIZE, sha256 $SHA)"
