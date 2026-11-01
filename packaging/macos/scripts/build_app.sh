#!/bin/bash
# Compile the Swift app and assemble + ad-hoc sign OhmyTesla.app:
#
#   OhmyTesla.app/Contents/
#     Info.plist  PkgInfo
#     MacOS/OhmyTesla                         Swift menu-bar app (also `--supervisor`)
#     Library/LaunchAgents/io.github.ohmytesla.supervisor.plist   (SMAppService.agent)
#     Resources/AppIcon.icns
#     Resources/runtime/                      from stage_runtime.sh
source "$(dirname "$0")/lib.sh"

APP="$BUILD/OhmyTesla.app"
RUNTIME_STAGE="$BUILD/stage/runtime"
[[ -d $RUNTIME_STAGE ]] || die "run stage_runtime.sh first"

log "app: swift build (release, arm64)"
swift build -c release --arch arm64 --package-path "$MACOS_DIR/App" --scratch-path "$BUILD/swift" 2>&1 \
  | grep -vE '^\[[0-9]+/[0-9]+\] (Compiling|Write|Emitting|Linking|Applying)' || true
BIN="$BUILD/swift/arm64-apple-macosx/release/OhmyTesla"
[[ -x $BIN ]] || die "swift build did not produce $BIN"

log "app: icon"
mkdir -p "$BUILD/tools"
if [[ ! -x $BUILD/tools/make_icon || $SCRIPTS/make_icon.swift -nt $BUILD/tools/make_icon ]]; then
  swiftc -O "$SCRIPTS/make_icon.swift" -o "$BUILD/tools/make_icon"
fi
rm -rf "$BUILD/AppIcon.iconset"
"$BUILD/tools/make_icon" "$BUILD/AppIcon.iconset"
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$BUILD/AppIcon.icns"

log "app: assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$APP/Contents/Library/LaunchAgents"
BUILD_NUMBER=$(git -C "$REPO_ROOT" rev-list --count HEAD 2>/dev/null || echo 1)
sed -e "s/@BUNDLE_ID@/$BUNDLE_ID/g" -e "s/@VERSION@/$OHMYTESLA_VERSION/g" \
    -e "s/@BUILD@/$BUILD_NUMBER/g" -e "s/@MACOS_MIN@/$MACOS_MIN/g" \
    "$MACOS_DIR/App/Resources/Info.plist" > "$APP/Contents/Info.plist"
sed -e "s/@BUNDLE_ID@/$BUNDLE_ID/g" "$MACOS_DIR/App/Resources/io.github.ohmytesla.supervisor.plist" \
    > "$APP/Contents/Library/LaunchAgents/io.github.ohmytesla.supervisor.plist"
plutil -lint -s "$APP/Contents/Info.plist" "$APP/Contents/Library/LaunchAgents/io.github.ohmytesla.supervisor.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"
install -m 755 "$BIN" "$APP/Contents/MacOS/OhmyTesla"
EXE="$APP/Contents/MacOS/OhmyTesla"
# Drop the debug map (it names this machine's source directory) ...
strip -S -x "$EXE" 2>/dev/null
# ... and SwiftPM's LC_RPATH into the Xcode toolchain (back-deployment libs).
# Nothing in the binary loads via @rpath; it links only /usr/lib/swift.
otool -l "$EXE" | awk '/cmd LC_RPATH/{getline; getline; print $2}' | while read -r rp; do
  case $rp in /usr/lib/*|@*) ;; *) install_name_tool -delete_rpath "$rp" "$EXE" ;; esac
done
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
ditto "$RUNTIME_STAGE" "$APP/Contents/Resources/runtime"

# ---- sign ------------------------------------------------------------------
# No Developer ID on this machine: ad-hoc signatures. Every nested Mach-O is
# signed on its own first (arm64 refuses unsigned code), then the bundle seals
# all resources. No hardened runtime: the BEAM's JIT and Python's ctypes
# need writable+executable memory, which an ad-hoc app can't be entitled to.
log "app: signing (ad-hoc)"
xattr -cr "$APP"
n=0
while IFS= read -r -d '' f; do
  if is_macho "$f"; then
    codesign --force --sign - --timestamp=none "$f" 2>/dev/null || die "codesign failed for $f"
    n=$((n + 1))
  fi
done < <(find "$APP/Contents/Resources/runtime" -type f -perm -u+x -print0; \
         find "$APP/Contents/Resources/runtime" -type f \( -name '*.so' -o -name '*.dylib' \) ! -perm -u+x -print0)
log "app: signed $n nested Mach-O files"
codesign --force --sign - --timestamp=none --identifier "$BUNDLE_ID" "$APP"

log "app: verifying"
codesign --verify --deep --strict "$APP"
"$HOST_PY" -I "$SCRIPTS/relocate.py" verify "$APP/Contents" --forbid "$BREW_PREFIX/" --forbid "$BUILD/" \
  --max-minos "$MACOS_MIN"
du -sh "$APP"
log "app: ready -> $APP"
