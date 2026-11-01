#!/bin/bash
# Relocatable CPython 3.12 (python-build-standalone) + the hub's dependencies.
#
#   build_python.sh            install from python-requirements.lock (hash-pinned)
#   build_python.sh --relock   re-resolve services/hub/requirements.txt and
#                              rewrite python-requirements.lock, then install
#
# The hub *code* is not copied here; build_app.sh copies the current tree of
# services/hub/app every time, so the app always ships what is checked out.
source "$(dirname "$0")/lib.sh"

RELOCK=0
[[ ${1:-} == --relock ]] && RELOCK=1
export PYTHONDONTWRITEBYTECODE=1

# Same interpreter minor as the hub's Docker image.
PY_MINOR=$(sed -nE 's/^FROM[[:space:]]+python:([0-9]+\.[0-9]+).*/\1/p' "$REPO_ROOT/services/hub/Dockerfile" | head -1)
[[ -n $PY_MINOR ]] || die "cannot read the Python version from services/hub/Dockerfile"
py_pin "$PY_MINOR" || die "services/hub/Dockerfile wants Python $PY_MINOR but versions.sh has no python-build-standalone pin for it"
log "python: CPython $PY_VERSION (hub Dockerfile uses python:$PY_MINOR)"

DEST="$OUT/python"
PYROOT="$DEST/python"
LOCK="$MACOS_DIR/python-requirements.lock"
REQ="$REPO_ROOT/services/hub/requirements.txt"
PY="$PYROOT/bin/python$PY_MINOR"

fresh_python() {
  local t; t=$(fetch python "$PY_URL" "$PY_SHA256" "$PY_FILE")
  extract "$t" "$PYROOT"
  [[ -x $PY ]] || die "python-build-standalone layout changed: $PY missing"
}

if [[ -f $LOCK ]] && ! grep -q "cpython=$PY_MINOR " "$LOCK"; then
  (( RELOCK )) || die "python-requirements.lock was resolved for another Python minor; run: build.sh --relock"
fi
if (( RELOCK )) || [[ ! -f $LOCK ]]; then
  log "python: resolving $REQ for macOS arm64 / CPython $PY_VERSION"
  fresh_python
  tmp="$BUILD/tmp/pylock"; rm -rf "$tmp"; mkdir -p "$tmp"
  "$PY" -I -B -m pip install --disable-pip-version-check --no-cache-dir --prefer-binary \
      --dry-run --ignore-installed --quiet --report "$tmp/report.json" -r "$REQ"
  "$PY" -I -B "$SCRIPTS/pylock.py" "$tmp/report.json" "$REQ" "$LOCK"
fi

KEY="$PY_VERSION+$PY_BUILD $(sha256_of "$LOCK") r4"
if stamp_ok "$DEST" "$KEY"; then
  log "python: cached"
else
  fresh_python
  log "python: installing hash-pinned hub dependencies"
  "$PY" -I -B -m pip install --disable-pip-version-check --no-cache-dir --no-deps \
      --require-hashes --prefer-binary -r "$LOCK"
  "$PY" -I -B -m pip check

  # Trim what a headless server never uses.
  SP="$PYROOT/lib/python$PY_MINOR"
  rm -rf "$SP/test" "$SP/idlelib" "$SP/tkinter" "$SP/turtledemo" "$SP/ensurepip" \
         "$SP/lib2to3" "$SP"/config-$PY_MINOR-darwin "$PYROOT/include" "$PYROOT/share" \
         "$PYROOT"/lib/libtcl* "$PYROOT"/lib/libtk* "$PYROOT"/lib/tcl* "$PYROOT"/lib/tk* \
         "$PYROOT"/lib/itcl* "$PYROOT"/lib/thread* "$PYROOT/lib/pkgconfig"
  rm -f "$SP"/lib-dynload/_tkinter*.so
  # pyobjc-core installs its own test suite (16 MB, with universal dSYM bundles),
  # and a few packages ship their tests inside the package.
  rm -rf "$SP/site-packages/PyObjCTest"
  find "$SP/site-packages" -name '*.dSYM' -prune -exec rm -rf {} +
  for t in pywebpush/tests certifi/tests py_vapid/tests aiosqlite/tests; do rm -rf "$SP/site-packages/$t"; done
  # pip-generated console scripts carry an absolute shebang into this build
  # directory; the supervisor always runs `python -m ...`, so drop them.
  find "$PYROOT/bin" -type f ! -name 'python3*' -exec sh -c \
    'head -c 2 "$1" | grep -q "#!" && rm -f "$1"' _ {} \;
  rm -f "$PYROOT"/bin/python*-config "$PYROOT"/bin/idle* "$PYROOT"/bin/pydoc*
  find "$PYROOT/bin" -type l ! -exec test -e {} \; -exec rm -f {} \;
  # No bytecode in the bundle: pip-compiled .pyc files embed this build path,
  # and a signed bundle must not be written to at runtime. The supervisor
  # sets PYTHONPYCACHEPREFIX to ~/Library/Caches/OhmyTesla/pycache instead.
  find "$PYROOT" -type d -name __pycache__ -prune -exec rm -rf {} +
  stamp_set "$DEST" "$KEY"
fi

# Byte-code written by pip/pip check above must not end up in the bundle.
find "$PYROOT" -type d -name __pycache__ -prune -exec rm -rf {} +

# requirements.txt may have changed since the lock was written; make sure the
# locked environment still satisfies it (resolves against installed dists only).
if ! "$PY" -I -B -m pip install --disable-pip-version-check --no-index --dry-run --quiet -r "$REQ" >/dev/null 2>&1; then
  die "services/hub/requirements.txt is no longer satisfied by python-requirements.lock — run: build.sh --relock"
fi
"$HOST_PY" -I "$SCRIPTS/relocate.py" fix "$PYROOT" --libdir lib/_dylibs
printf '%s\n' "$PY_MINOR" > "$DEST/minor"
log "python: ready -> $PYROOT"
