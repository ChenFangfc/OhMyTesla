# shellcheck shell=bash
# Every third-party artifact the macOS build pulls in, pinned by version + SHA-256
# (tarballs) or by commit hash (git clones). build.sh refuses anything that
# doesn't match. To bump a component: change the version, set the hash to "",
# run build.sh once — it prints the hash it saw and stops — then paste it in.
#
# One-time provenance checks done when these pins were taken (2026-10-31):
#   ffmpeg    .asc  Good signature, FFmpeg release key FCF9 86EA 15E6 E293 A564 4F10 B432 2F04 D676 58D8
#   mosquitto .asc  Good signature, Roger A. Light  A0D6 EEA1 DCAE 49A6 35A3 B2F0 779B 22DF B3E7 17B7
#   freetype  .sig  Good signature, Werner Lemberg  E306 7470 7856 409F F194 8010 BE6C 3AAC 63AD 8E3F
#   fontconfig      matches the .sha256sum published next to the release
#   GitHub release assets (python, libass, fribidi, harfbuzz, otp, openssl) match
#   the sha256 digest GitHub records for the asset; postgresql matches the
#   .sha256 file published next to it on ftp.postgresql.org.

# ---- the app itself --------------------------------------------------------
: "${OHMYTESLA_VERSION:=0.2.1}"
BUNDLE_ID="io.github.ohmytesla.app"
MACOS_MIN="13.0"

# ---- PostgreSQL ---------------------------------------------------------------
# Built from source (scripts/build_postgres.sh). The theseus-rs relocatable
# archive (postgresql-18.6.1-aarch64-apple-darwin, sha256 89e91cc8…) has cube +
# earthdistance but its binaries require macOS 15 (LC_BUILD_VERSION minos 15.0),
# and this app targets macOS 13.
PG_VERSION="18.6"
PG_URL="https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.bz2"
PG_SHA256="555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f"   # = postgresql.org's .sha256

# ---- Erlang/OTP (for TeslaMate's ERTS) -------------------------------------
# From source with a static OpenSSL: Homebrew's bottles are built for the build
# Mac's macOS (minos 26.0 here) and link a Homebrew OpenSSL dylib.
OTP_VERSION="29.1.2"
OTP_URL="https://github.com/erlang/otp/releases/download/OTP-${OTP_VERSION}/otp_src_${OTP_VERSION}.tar.gz"
OTP_SHA256="c79966661e35fe4aff8ec003db63682e3b33b10b672220d51bf93c0a9244b049"   # = GitHub asset digest
OPENSSL_VERSION="3.5.9"   # LTS
OPENSSL_URL="https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz"
OPENSSL_SHA256="603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"   # = GitHub asset digest

# python-build-standalone. The interpreter *minor* version follows the hub's
# own Dockerfile (`FROM python:3.X-slim`), so the app and the Docker image
# never drift apart; build_python.sh picks the matching pin below and stops
# if there is none. (The hub moved to 3.13 for tesla-fleet-api.)
PY_BUILD="20261026"
PY312_VERSION="3.12.16"
PY312_SHA256="fbf4afef53c2a6b72bb741bc06d5671a9dfaf87bd84e5aa73ac9b3d0d589f14e"
PY313_VERSION="3.13.18"
PY313_SHA256="e5af2fde6a937a0e0c795a0c595c4efb70578d57463a2657c819992b9b9acbac"
PY314_VERSION="3.14.10"
PY314_SHA256="30a18bcb9ab75e485b4bf4e5d783dde165571e7e42c56dff47a2d2c5345183a4"
py_pin() {   # py_pin 3.13 -> sets PY_VERSION PY_SHA256 PY_URL PY_FILE
  local key; key="PY$(tr -d . <<<"$1")"
  local v="${key}_VERSION" h="${key}_SHA256"
  [[ -n ${!v:-} ]] || return 1
  PY_VERSION=${!v}; PY_SHA256=${!h}
  PY_FILE="cpython-${PY_VERSION}+${PY_BUILD}-aarch64-apple-darwin-install_only.tar.gz"
  PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_BUILD}/cpython-${PY_VERSION}%2B${PY_BUILD}-aarch64-apple-darwin-install_only.tar.gz"
}

# ---- built from source -----------------------------------------------------
TESLAMATE_REPO="https://github.com/teslamate-org/teslamate.git"
TESLAMATE_TAG="v4.4.0"
TESLAMATE_COMMIT="42d187daadd0104fc9c4509785df47a26705845a"


# 2.0.x rather than 2.1.x: from 2.1 on the broker itself links cJSON, and the
# brief is a minimal broker (no TLS / cJSON / websockets / docs). Same MQTT
# behaviour for TeslaMate and the hub; 2.0.22 is the current 2.0 release.
MOSQUITTO_VERSION="2.0.22"
MOSQUITTO_URL="https://mosquitto.org/files/source/mosquitto-${MOSQUITTO_VERSION}.tar.gz"
MOSQUITTO_SHA256="2f752589ef7db40260b633fbdb536e9a04b446a315138d64a7ff3c14e2de6b68"

FFMPEG_VERSION="8.1.3"
FFMPEG_URL="https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz"
FFMPEG_SHA256="7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3"

X264_COMMIT="b35605ace3ddf7c1a5d67a2eb553f034aef41d55"   # 'stable' branch head, 2025-06-08
X264_URL="https://code.videolan.org/videolan/x264/-/archive/${X264_COMMIT}/x264-${X264_COMMIT}.tar.bz2"
X264_SHA256="6eeb82934e69fd51e043bd8c5b0d152839638d1ce7aa4eea65a3fedcf83ff224"

FREETYPE_VERSION="2.14.3"
FREETYPE_URL="https://download.savannah.gnu.org/releases/freetype/freetype-${FREETYPE_VERSION}.tar.xz"
FREETYPE_SHA256="36bc4f1cc413335368ee656c42afca65c5a3987e8768cc28cf11ba775e785a5f"

FRIBIDI_VERSION="1.0.17"
FRIBIDI_URL="https://github.com/fribidi/fribidi/releases/download/v${FRIBIDI_VERSION}/fribidi-${FRIBIDI_VERSION}.tar.xz"
FRIBIDI_SHA256="6949dcde27d41cebad1fd741fcafc36d55a1020d2d872d4a6eb3914caabbada2"

HARFBUZZ_VERSION="14.6.0"
HARFBUZZ_URL="https://github.com/harfbuzz/harfbuzz/releases/download/${HARFBUZZ_VERSION}/harfbuzz-${HARFBUZZ_VERSION}.tar.xz"
HARFBUZZ_SHA256="d07a007327277708a2a73ae437887cdbaf282937f6d03ca5467723e9099af586"

FONTCONFIG_VERSION="2.17.1"
FONTCONFIG_URL="https://gitlab.freedesktop.org/api/v4/projects/890/packages/generic/fontconfig/${FONTCONFIG_VERSION}/fontconfig-${FONTCONFIG_VERSION}.tar.xz"
FONTCONFIG_SHA256="9f5cae93f4fffc1fbc05ae99cdfc708cd60dfd6612ffc0512827025c026fa541"

LIBASS_VERSION="0.17.5"
LIBASS_URL="https://github.com/libass/libass/releases/download/${LIBASS_VERSION}/libass-${LIBASS_VERSION}.tar.xz"
LIBASS_SHA256="2dca25c0e0c837ddf00b52011b3f82cac1e4ddd3ad018227806b0c2288864acc"

# ---- fonts (SIL OFL 1.1) ---------------------------------------------------
# Language-specific OTFs: family name is exactly "Noto Sans CJK SC", which is
# what services/hub/app/dashcam/telemetry.py asks libass for.
NOTO_TAG="Sans2.004"      # commit 523d033d6cb47f4a80c58a35753646f5c3608a78
NOTO_BASE="https://raw.githubusercontent.com/notofonts/noto-cjk/${NOTO_TAG}"
NOTO_REGULAR_SHA256="2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
NOTO_BOLD_SHA256="b5f0d1a190a7f9b43c310a8850630af12553df32c4c050543f9059732d9b4c0a"
NOTO_LICENSE_SHA256="6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"

# ---- toolchain expectations (Homebrew; build-time only) --------------------
# TeslaMate v4.4.0's Dockerfile builds with elixir:1.20.3-otp-29.
EXPECT_OTP_MAJOR="29"
EXPECT_ELIXIR_MINOR="1.20"
