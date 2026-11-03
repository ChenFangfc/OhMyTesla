# dmgbuild settings for OhmyTesla-<version>-arm64.dmg. scripts/make_dmg.sh passes the
# paths: -D app=… (OhmyTesla.app), -D icon=… (its AppIcon.icns, as the volume icon)
# and -D background=… (background.png; dmgbuild picks up background@2x.png beside it).
#
# One window, no toolbar or sidebar: the app on the left, an Applications link on the
# right, the arrow between them drawn into the background (background.html).
# 660 points wide; 440 high, of which Finder may give some to the title bar — the
# background runs to 480 so neither way leaves a gap.

app = defines["app"]  # noqa: F821  (dmgbuild provides `defines`)
files = [app]
symlinks = {"Applications": "/Applications"}
icon = defines["icon"]  # noqa: F821
background = defines["background"]  # noqa: F821

format = "ULMO"
filesystem = "HFS+"

window_rect = ((200, 140), (660, 440))
default_view = "icon-view"
show_toolbar = False
show_status_bar = False
show_pathbar = False
show_sidebar = False
show_tab_view = False
show_icon_preview = False

icon_size = 128
text_size = 13
label_pos = "bottom"
# No hide_extensions: Finder leaves ".app" off anyway, and the flag writes Finder info onto
# the bundle, which `codesign --verify --strict` rejects ("detritus not allowed").
icon_locations = {
    "OhmyTesla.app": (170, 200),
    "Applications": (490, 200),
}
