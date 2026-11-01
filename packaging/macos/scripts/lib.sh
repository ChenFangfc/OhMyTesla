# shellcheck shell=bash
# Shared helpers for the macOS build scripts. Sourced, never executed.

set -euo pipefail

MACOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$MACOS_DIR/../.." && pwd)"
SCRIPTS="$MACOS_DIR/scripts"
BUILD="${OHMYTESLA_BUILD_DIR:-$MACOS_DIR/build}"
DIST="$MACOS_DIR/dist"
DL="$BUILD/downloads"        # one sub-directory per artifact, verified by SHA-256
SRC="$BUILD/src"             # extracted / cloned sources, one directory each
OUT="$BUILD/out"             # per-component install trees (cached by stamp files)
CACHE="$BUILD/cache"         # toolchain caches (hex, mix, npm, go) kept out of $HOME
JOBS="$(sysctl -n hw.ncpu)"

# shellcheck source=../versions.sh
source "$MACOS_DIR/versions.sh"

# Build-time tools come from Homebrew; nothing from there may end up linked
# into the bundle (relocate.py --verify enforces that at the end).
BREW_PREFIX="${BREW_PREFIX:-/opt/homebrew}"
export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:/usr/bin:/bin:/usr/sbin:/sbin"
export MACOSX_DEPLOYMENT_TARGET="$MACOS_MIN"
# Make sure no stray search paths leak Homebrew headers/libs into C builds.
unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH PKG_CONFIG_PATH LDFLAGS CFLAGS CPPFLAGS

# Sources live under the OhmyTesla checkout; stop their build systems from
# running `git describe` against *our* repository (libass, x264 do that).
export GIT_CEILING_DIRECTORIES="$SRC"

HOST_PY=/usr/bin/python3     # Apple's python, only used to run our own helper scripts

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }

# fetch <name> <url> <sha256> [filename]  -> prints the verified local path.
# Each artifact lives in its own directory so archives never see each other.
fetch() {
  local name=$1 url=$2 sha=$3 file=${4:-}
  [[ -n $file ]] || file=$(basename "${url%%\?*}")
  local dir="$DL/$name" dest
  dest="$dir/$file"
  mkdir -p "$dir"
  if [[ -f $dest && $(sha256_of "$dest") == "$sha" ]]; then
    printf '%s\n' "$dest"; return 0
  fi
  log "downloading $name ($file)"
  rm -f "$dest" "$dest.part"
  curl -fL --retry 4 --retry-delay 3 --connect-timeout 30 -o "$dest.part" "$url" >&2
  local got; got=$(sha256_of "$dest.part")
  if [[ -z $sha ]]; then
    die "no SHA-256 pinned for $name; downloaded file has $got — add it to versions.sh after checking provenance"
  fi
  if [[ $got != "$sha" ]]; then
    rm -f "$dest.part"
    die "SHA-256 mismatch for $name: expected $sha, got $got"
  fi
  mv "$dest.part" "$dest"
  printf '%s\n' "$dest"
}

# extract <archive> <destdir>: fresh, empty destination; strips the top directory.
extract() {
  local archive=$1 dest=$2
  rm -rf "$dest"; mkdir -p "$dest"
  tar -xf "$archive" -C "$dest" --strip-components 1
}

# git_checkout <repo> <tag> <commit> <dest>: shallow clone and refuse a moved tag.
git_checkout() {
  local repo=$1 tag=$2 commit=$3 dest=$4
  if [[ -d $dest/.git ]] && [[ $(git -C "$dest" rev-parse HEAD) == "$commit" ]]; then
    return 0
  fi
  rm -rf "$dest"; mkdir -p "$(dirname "$dest")"
  log "cloning $repo @ $tag"
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$tag" "$repo" "$dest"
  local head; head=$(git -C "$dest" rev-parse HEAD)
  [[ $head == "$commit" ]] || die "$repo tag $tag resolves to $head, pinned commit is $commit"
}

# Stamp files let a re-run skip components whose inputs didn't change.
stamp_ok()  { [[ -f "$1/.stamp" && "$(cat "$1/.stamp")" == "$2" ]]; }
stamp_set() { mkdir -p "$1"; printf '%s' "$2" > "$1/.stamp"; }

is_macho() {
  local magic
  magic=$(xxd -l 4 -p "$1" 2>/dev/null || true)
  case $magic in cffaedfe|cefaedfe|cafebabe|bebafeca) return 0 ;; *) return 1 ;; esac
}

# Common flags for the C dependencies we compile.
c_env() {
  export CC=/usr/bin/clang CXX=/usr/bin/clang++
  export CFLAGS="-arch arm64 -mmacosx-version-min=$MACOS_MIN -O2"
  export CXXFLAGS="$CFLAGS"
  export LDFLAGS="-arch arm64 -mmacosx-version-min=$MACOS_MIN"
}
