#!/bin/bash
# OhmyTesla-<version>-arm64.dmg: the app, an /Applications link to drag it onto,
# and a short note about the first launch in Chinese and English (the app is ad-hoc
# signed, not notarized).
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
cat > "$STAGE/首次启动 First launch.txt" <<'EOF'
OhmyTesla — 首次启动
====================

1. 把 OhmyTesla 拖到“应用程序”文件夹。

2. OhmyTesla 是开源软件，只有“临时（ad hoc）”签名（没有付费的 Apple 开发者 ID），
   所以 macOS 会拦下第一次打开。允许一次即可：

   macOS 13 / 14：在“应用程序”中按住 Control 点按（右键）OhmyTesla，选“打开”，再点“打开”。
   macOS 15 及以后：双击 OhmyTesla，关掉警告，打开“系统设置 › 隐私与安全性”，
                   向下滚动，点 OhmyTesla 旁边的“仍要打开”。
   任何版本，在“终端”中：
                   xattr -dr com.apple.quarantine /Applications/OhmyTesla.app

3. 选择运行方式：
   · 已在 NAS 或服务器上运行 OhmyTesla：选“连接已有服务器”，最省资源；
   · 否则选“在这台 Mac 上运行”：完整服务装在应用内，无需 Docker。

之后点菜单栏里的 OhmyTesla 图标，可以快速查看车辆状态和常用设置。

数据：~/Library/Application Support/OhmyTesla   （config.env 中有门户密码）
日志：~/Library/Logs/OhmyTesla
更多：https://github.com/ChenFangfc/OhMyTesla/tree/main/packaging/macos


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

3. Choose how it runs:
   · You already run OhmyTesla on a NAS or server: "Connect to a server" — the lightest.
   · Otherwise "Run on this Mac": the whole stack inside the app, no Docker.

Then click the OhmyTesla icon in the menu bar for the car's status and quick settings.

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
