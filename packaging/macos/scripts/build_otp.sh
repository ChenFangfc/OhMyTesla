#!/bin/bash
# Erlang/OTP from source, for the TeslaMate release's ERTS.
#
# Why not Homebrew's erlang: its bottles are built for the build Mac's own macOS
# (on macOS 26 every ERTS binary and the crypto NIF say minos 26.0), and its
# crypto NIF links Homebrew's OpenSSL dylib. This build targets macOS 13 and
# links a static OpenSSL 3.5 LTS into the crypto NIF, so the release needs no
# dylibs besides the OS. Elixir itself (OS-independent .beam files) still comes
# from Homebrew; it simply runs on this OTP (`elixir` finds `erl` on PATH).
source "$(dirname "$0")/lib.sh"

DEST="$OUT/otp"
KEY="otp$OTP_VERSION $OTP_SHA256 ssl$OPENSSL_VERSION macos$MACOS_MIN r1"
if stamp_ok "$DEST" "$KEY"; then log "otp: cached"; exit 0; fi
c_env

# ---- OpenSSL (static) -------------------------------------------------------
# Neutral prefix + DESTDIR: the compiled-in OPENSSLDIR/MODULESDIR are not this
# build directory (at runtime OPENSSL_CONF points at the bundle's own config).
t=$(fetch openssl "$OPENSSL_URL" "$OPENSSL_SHA256")
extract "$t" "$SRC/openssl"
SSLROOT="$OUT/openssl-root"
SSL="$SSLROOT/opt/ohmytesla/openssl"
rm -rf "$SSLROOT"
log "otp: OpenSSL $OPENSSL_VERSION (static)"
(
  cd "$SRC/openssl"
  ./Configure darwin64-arm64-cc no-shared no-tests no-docs no-apps no-module \
    --prefix=/opt/ohmytesla/openssl --openssldir=/etc/ssl --libdir=lib \
    -mmacosx-version-min="$MACOS_MIN"
  make -j "$JOBS" build_libs
  make install_dev DESTDIR="$SSLROOT"
)

# ---- Erlang/OTP -------------------------------------------------------------
t=$(fetch otp "$OTP_URL" "$OTP_SHA256")
extract "$t" "$SRC/otp"
rm -rf "$DEST"
log "otp: Erlang/OTP $OTP_VERSION"
(
  cd "$SRC/otp"
  export ERL_TOP="$PWD"
  ./configure --prefix="$DEST" \
    --with-ssl="$SSL" --disable-dynamic-ssl-lib \
    --without-javac --without-wx --without-odbc --without-jinterface --without-megaco \
    --without-diameter --without-snmp --without-observer --without-debugger --without-et \
    --without-reltool
  make -j "$JOBS"
  make install
)
cp "$SRC/otp/LICENSE.txt" "$DEST/LICENSE.txt"
cp "$SRC/openssl/LICENSE.txt" "$DEST/OPENSSL-LICENSE.txt"
"$DEST/bin/erl" -noshell -eval 'io:format("~s ~s~n",[erlang:system_info(otp_release), crypto:info_lib() /= []]), halt().'
otool -L "$DEST"/lib/erlang/lib/crypto-*/priv/lib/crypto.so
stamp_set "$DEST" "$KEY"
log "otp: done -> $DEST"
