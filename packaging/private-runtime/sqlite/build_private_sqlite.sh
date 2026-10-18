#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${1:-}"

die() {
  printf 'private SQLite build: %s\n' "$*" >&2
  exit 1
}

[[ -n "$OUTPUT_DIR" ]] || die "usage: $0 OUTPUT_DIRECTORY"
[[ "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]] || \
  die "this recipe builds only on Linux AArch64"
for tool in cc make tar sha256sum readelf; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
(
  cd "$SCRIPT_DIR"
  sha256sum --check SHA256SUMS
)

WORK_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/event-logger-sqlite-build.XXXXXX")"
# 一時ディレクトリを終了時に削除し、ビルド生成物と混在させない。
cleanup() {
  rm -rf -- "$WORK_ROOT"
}
trap cleanup EXIT
mkdir -p "$OUTPUT_DIR"
tar -xzf "$SCRIPT_DIR/sqlite-autoconf-3530400.tar.gz" \
  -C "$WORK_ROOT"
SOURCE_ROOT="$WORK_ROOT/sqlite-autoconf-3530400"
INSTALL_ROOT="$WORK_ROOT/install"
(
  cd "$SOURCE_ROOT"
  CFLAGS='-O2 -fPIC' LDFLAGS='-Wl,-soname,libsqlite3.so.0' \
    ./configure --prefix="$INSTALL_ROOT" --enable-shared \
      --disable-static --disable-rpath
  make -j2
  make install
)

BUILT_LIBRARY="$INSTALL_ROOT/lib/libsqlite3.so.3.53.4"
[[ -f "$BUILT_LIBRARY" ]] || die "build did not produce the versioned shared library"
readelf -h "$BUILT_LIBRARY" | grep -q 'Machine:.*AArch64' || \
  die "built library is not AArch64"
readelf -d "$BUILT_LIBRARY" | grep -q 'SONAME.*libsqlite3.so.0' || \
  die "built library has the wrong SONAME"
if readelf -d "$BUILT_LIBRARY" | grep -Eq '\((RPATH|RUNPATH)\)'; then
  die "built library contains RPATH/RUNPATH"
fi
install -m 0644 "$BUILT_LIBRARY" "$OUTPUT_DIR/libsqlite3.so.3.53.4"
sha256sum "$OUTPUT_DIR/libsqlite3.so.3.53.4"
