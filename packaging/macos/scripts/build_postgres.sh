#!/bin/bash
# PostgreSQL 18 from source for macOS 13+ (arm64), relocatable.
#
# The prebuilt theseus-rs archive was the first choice (it is relocatable and
# has cube + earthdistance) but its binaries need macOS 15. Building it
# ourselves also drops what an embedded TeslaMate database never uses: no ICU
# (initdb uses the builtin C.UTF-8 provider), no OpenSSL (local socket only;
# SCRAM uses PostgreSQL's own SHA-256), no LLVM JIT, and psql uses the system
# libedit instead of GNU readline.
source "$(dirname "$0")/lib.sh"

DEST="$OUT/postgresql"
PG="$DEST/pg"
KEY="src $PG_VERSION $PG_SHA256 macos$MACOS_MIN r1"
if stamp_ok "$DEST" "$KEY"; then log "postgresql: cached"; exit 0; fi

t=$(fetch postgresql-src "$PG_URL" "$PG_SHA256")
S="$SRC/postgresql"
extract "$t" "$S"
c_env
export PKG_CONFIG_LIBDIR=/nonexistent     # nothing from Homebrew's pkg-config tree
rm -rf "$DEST"
(
  cd "$S"
  # Defaults already leave out OpenSSL, LLVM, lz4, zstd and libxml.
  ./configure --prefix="$PG" --without-icu --with-libedit-preferred --with-zlib
  make -j "$JOBS" -s
  make -s install
  make -C contrib -j "$JOBS" -s
  make -C contrib -s install
)
for ext in cube earthdistance; do
  [[ -f $PG/share/extension/$ext.control && -f $PG/lib/$ext.dylib ]] \
    || die "postgresql build lacks the $ext extension"
done

# Trim what the app never runs.
rm -rf "$PG/include" "$PG/lib/pgxs" "$PG/lib/pkgconfig"
rm -f "$PG"/lib/*.a
for b in ecpg pgbench pg_test_fsync pg_test_timing oid2name vacuumlo pg_config \
         pg_receivewal pg_recvlogical pg_createsubscriber pg_rewind pg_combinebackup \
         pg_walsummary pg_archivecleanup pg_basebackup pg_verifybackup clusterdb reindexdb; do
  rm -f "$PG/bin/$b"
done

# libpq's install name is absolute ($PG/lib/libpq.5.dylib): point psql & co at
# it relative to themselves.
"$HOST_PY" -I "$SCRIPTS/relocate.py" fix "$PG" --libdir lib
"$HOST_PY" -I "$SCRIPTS/relocate.py" verify "$PG" --max-minos "$MACOS_MIN"
cp "$S/COPYRIGHT" "$DEST/COPYRIGHT"
"$PG/bin/postgres" --version
stamp_set "$DEST" "$KEY"
du -sh "$PG"
log "postgresql: done -> $PG"
