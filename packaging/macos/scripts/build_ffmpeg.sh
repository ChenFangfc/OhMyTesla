#!/bin/bash
# Static ffmpeg + ffprobe (arm64) with libass for the dashcam overlays.
#
#   freetype -> fribidi -> harfbuzz -> fontconfig -> libass -> x264 -> ffmpeg
#
# Everything is linked statically; the binaries depend only on libSystem,
# libc++, libz/libbz2/libiconv/libexpat from /usr/lib and Apple frameworks
# (VideoToolbox for the hardware H.264 encoder the hub probes for).
#
# libass is built with the *fontconfig* font provider and without CoreText on
# purpose: the hub asks for the family "Noto Sans CJK SC" by name and never
# passes a fontsdir, so the font has to be discoverable by name. CoreText only
# sees fonts installed on the Mac; fontconfig honours $FONTCONFIG_FILE, which
# the supervisor points at a generated fonts.conf that lists the bundled
# Noto Sans CJK fonts (plus the macOS font folders for fallback).
#
# Licence of the result: GPL-2.0-or-later (--enable-gpl, because of libx264).
source "$(dirname "$0")/lib.sh"

DEST="$OUT/ffmpeg"
DEPS="$BUILD/out/ffdeps"          # static libs + headers, build-time only
DEPS_KEY="x264$X264_COMMIT ft$FREETYPE_VERSION fb$FRIBIDI_VERSION hb$HARFBUZZ_VERSION fc$FONTCONFIG_VERSION ass$LIBASS_VERSION r2"
KEY="ff$FFMPEG_VERSION $DEPS_KEY r4"
if stamp_ok "$DEST" "$KEY"; then log "ffmpeg: cached"; exit 0; fi

c_env
# Only our own .pc files are visible — Homebrew has freetype/fontconfig/
# harfbuzz too, and silently linking those would break relocation.
export PKG_CONFIG_LIBDIR="$DEPS/lib/pkgconfig"

build_deps() {
rm -rf "$DEPS"
mkdir -p "$DEPS/lib/pkgconfig"
# expat ships in the macOS SDK (headers + libexpat.tbd) but without a .pc file.
cat > "$DEPS/lib/pkgconfig/expat.pc" <<'EOF2'
Name: expat
Description: expat XML parser from the macOS SDK (/usr/lib/libexpat.1.dylib)
Version: 2.6.0
Libs: -lexpat
Cflags:
EOF2

MESON_COMMON=(--prefix="$DEPS" --libdir=lib --buildtype=release --default-library=static
              --wrap-mode=nodownload -Db_ndebug=true)

meson_build() {   # <name> <srcdir> [meson options...]
  local name=$1 src=$2; shift 2
  log "ffmpeg deps: $name"
  meson setup "$src/_build" "$src" "${MESON_COMMON[@]}" "$@"
  meson compile -C "$src/_build" -j "$JOBS"
  meson install -C "$src/_build" --no-rebuild
}

# freetype --------------------------------------------------------------
t=$(fetch freetype "$FREETYPE_URL" "$FREETYPE_SHA256"); extract "$t" "$SRC/freetype"
meson_build freetype "$SRC/freetype" \
  -Dbrotli=disabled -Dbzip2=disabled -Dharfbuzz=disabled -Dpng=disabled \
  -Dzlib=internal -Dtests=disabled -Dmmap=enabled

# fribidi ---------------------------------------------------------------
t=$(fetch fribidi "$FRIBIDI_URL" "$FRIBIDI_SHA256"); extract "$t" "$SRC/fribidi"
meson_build fribidi "$SRC/fribidi" -Ddocs=false -Dbin=false -Dtests=false

# harfbuzz --------------------------------------------------------------
t=$(fetch harfbuzz "$HARFBUZZ_URL" "$HARFBUZZ_SHA256"); extract "$t" "$SRC/harfbuzz"
meson_build harfbuzz "$SRC/harfbuzz" \
  -Dfreetype=enabled -Dglib=disabled -Dgobject=disabled -Dcairo=disabled -Dchafa=disabled \
  -Dpng=disabled -Dzlib=disabled -Dicu=disabled -Dgraphite=disabled -Dgraphite2=disabled \
  -Dfontations=disabled -Dcoretext=disabled -Dharfrust=disabled -Dkbts=disabled -Dwasm=disabled \
  -Draster=disabled -Dvector=disabled -Dgpu=disabled -Dgpu_demo=disabled -Dsubset=disabled \
  -Dtests=disabled -Dintrospection=disabled -Ddocs=disabled -Dutilities=disabled -Dbenchmark=disabled

# fontconfig ------------------------------------------------------------
# Compiled-in defaults point at the conventional /etc/fonts and /var/cache
# rather than this build directory; at runtime FONTCONFIG_FILE is always set.
# Installed through a scratch DESTDIR so nothing is written to /etc.
t=$(fetch fontconfig "$FONTCONFIG_URL" "$FONTCONFIG_SHA256"); extract "$t" "$SRC/fontconfig"
log "ffmpeg deps: fontconfig"
meson setup "$SRC/fontconfig/_build" "$SRC/fontconfig" "${MESON_COMMON[@]}" \
  -Ddoc=disabled -Dnls=disabled -Dtests=disabled -Dtools=disabled -Dcache-build=disabled \
  -Diconv=disabled -Dxml-backend=expat -Dfontations=disabled \
  -Dbaseconfig-dir=/etc/fonts -Dconfig-dir=/etc/fonts/conf.d -Dtemplate-dir=/etc/fonts/conf.avail \
  -Dxml-dir=/etc/fonts -Dcache-dir=/var/cache/fontconfig
meson compile -C "$SRC/fontconfig/_build" -j "$JOBS"
FCROOT="$SRC/fontconfig/_destdir"
rm -rf "$FCROOT"
DESTDIR="$FCROOT" meson install -C "$SRC/fontconfig/_build" --no-rebuild
cp -R "$FCROOT$DEPS/." "$DEPS/"

# libass ----------------------------------------------------------------
t=$(fetch libass "$LIBASS_URL" "$LIBASS_SHA256"); extract "$t" "$SRC/libass"
meson_build libass "$SRC/libass" \
  -Dfontconfig=enabled -Dcoretext=disabled -Ddirectwrite=disabled -Dlibunibreak=disabled \
  -Dtest=disabled -Dcompare=disabled -Dprofile=disabled -Dfuzz=disabled -Dcheckasm=disabled \
  -Drequire-system-font-provider=true

# x264 ------------------------------------------------------------------
t=$(fetch x264 "$X264_URL" "$X264_SHA256"); extract "$t" "$SRC/x264"
log "ffmpeg deps: x264"
(
  cd "$SRC/x264"
  ./configure --prefix="$DEPS" --enable-static --enable-pic --disable-cli --disable-opencl \
    --disable-avs --disable-swscale --disable-lavf --disable-ffms --disable-gpac --disable-lsmash \
    --extra-cflags="$CFLAGS" --extra-ldflags="$LDFLAGS"
  make -j "$JOBS"
  make install-lib-static
)

stamp_set "$DEPS" "$DEPS_KEY"
}
stamp_ok "$DEPS" "$DEPS_KEY" && log "ffmpeg deps: cached" || build_deps

# ffmpeg ----------------------------------------------------------------
# Generic --cc/--pkg-config values and no -I/-L flags (pkg-config supplies
# those): the configure line is embedded verbatim in the binaries, and the
# default prefix keeps the compiled-in datadir off this build directory.
# Not `make install`ed — the two binaries are copied out directly.
t=$(fetch ffmpeg "$FFMPEG_URL" "$FFMPEG_SHA256"); extract "$t" "$SRC/ffmpeg"
log "ffmpeg: configure"
rm -rf "$DEST"; mkdir -p "$DEST/bin"
(
  cd "$SRC/ffmpeg"
  ./configure \
    --cc=clang --cxx=clang++ --arch=arm64 --target-os=darwin \
    --pkg-config=pkg-config --pkg-config-flags=--static \
    --extra-cflags="-mmacosx-version-min=$MACOS_MIN" \
    --extra-ldflags="-mmacosx-version-min=$MACOS_MIN" \
    --extra-libs="-lc++" \
    --enable-gpl --disable-nonfree \
    --enable-static --disable-shared \
    --disable-autodetect \
    --enable-videotoolbox --enable-audiotoolbox --enable-zlib --enable-bzlib --enable-iconv \
    --enable-libass --enable-libfreetype --enable-libfribidi --enable-libharfbuzz --enable-libfontconfig \
    --enable-libx264 \
    --disable-ffplay --disable-doc --disable-debug \
    --disable-lzma --disable-sdl2 --disable-xlib --disable-libxcb \
    --disable-securetransport --disable-gnutls --disable-openssl
  make -j "$JOBS" ffmpeg ffprobe
)
install -m 755 "$SRC/ffmpeg/ffmpeg" "$SRC/ffmpeg/ffprobe" "$DEST/bin/"

# Licences of everything linked into the binaries.
L="$DEST/licenses"; mkdir -p "$L"
cp "$SRC/ffmpeg/COPYING.GPLv2" "$L/ffmpeg-COPYING.GPLv2"
cp "$SRC/ffmpeg/LICENSE.md" "$L/ffmpeg-LICENSE.md"
cp "$SRC/x264/COPYING" "$L/x264-COPYING"
cp "$SRC/libass/COPYING" "$L/libass-COPYING"
cp "$SRC/freetype/LICENSE.TXT" "$L/freetype-LICENSE.TXT"
cp "$SRC/freetype/docs/FTL.TXT" "$L/freetype-FTL.TXT"
cp "$SRC/fribidi/COPYING" "$L/fribidi-COPYING"
cp "$SRC/harfbuzz/COPYING" "$L/harfbuzz-COPYING"
cp "$SRC/fontconfig/COPYING" "$L/fontconfig-COPYING"
"$DEST/bin/ffmpeg" -hide_banner -buildconf > "$L/ffmpeg-buildconf.txt" 2>&1 || true

for b in ffmpeg ffprobe; do
  codesign --force --sign - --timestamp=none "$DEST/bin/$b"
  otool -L "$DEST/bin/$b"
done
stamp_set "$DEST" "$KEY"
log "ffmpeg: done -> $DEST/bin"
