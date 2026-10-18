#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${EVENT_LOGGER_APP_ROOT:-/opt/event-logger}"
RUNTIME_ROOT="${EVENT_LOGGER_PRIVATE_SQLITE_ROOT:-$APP_ROOT/runtime/sqlite}"
VERSION="3.53.4"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SQLITE_BUNDLE_ROOT="$SCRIPT_DIR/private-runtime/sqlite"
SOURCE_LIBRARY="$SQLITE_BUNDLE_ROOT/libsqlite3.so.$VERSION"
VERSION_ROOT="$RUNTIME_ROOT/$VERSION"
CURRENT_LINK="$RUNTIME_ROOT/current"

# 引数: 1個以上の診断文。標準エラーへprefix付きで出力する。
# 戻り値: 戻らない。status 1でinstallerを終了する。
# 副作用: 標準エラー出力とprocess終了。
die() {
  printf 'private SQLite install: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "root権限で実行してください"
[[ "$(uname -m)" == aarch64 ]] || die "64-bit ARM64が必要です"
[[ -d "$SQLITE_BUNDLE_ROOT" && -f "$SOURCE_LIBRARY" ]] || \
  die "private SQLite bundleがありません"
(cd "$SQLITE_BUNDLE_ROOT" && sha256sum --check SHA256SUMS) || \
  die "private SQLite bundleのSHA-256検証に失敗しました"
readelf -h "$SOURCE_LIBRARY" | grep -q 'Machine:.*AArch64' || \
  die "bundle libraryがAArch64ではありません"
readelf -d "$SOURCE_LIBRARY" | grep -q 'SONAME.*libsqlite3.so.0' || \
  die "bundle libraryのSONAMEが不正です"
if readelf -d "$SOURCE_LIBRARY" | grep -Eq '\((RPATH|RUNPATH)\)'; then
  die "bundle libraryにRPATH/RUNPATHがあります"
fi

[[ ! -L "$RUNTIME_ROOT" ]] || die "runtime root must not be a symlink"
install -d -m 0755 -o root -g root "$RUNTIME_ROOT"
chown root:root "$RUNTIME_ROOT"
chmod 0755 "$RUNTIME_ROOT"
if [[ -e "$VERSION_ROOT" ]]; then
  [[ -d "$VERSION_ROOT" && ! -L "$VERSION_ROOT" ]] || \
    die "既存runtimeのpath種別が不正です"
  cmp -s "$SOURCE_LIBRARY" "$VERSION_ROOT/lib/libsqlite3.so.$VERSION" || \
    die "同じVersionの既存runtimeがbundleと一致しません"
  cmp -s "$SQLITE_BUNDLE_ROOT/sqlite-autoconf-3530400.tar.gz" \
    "$VERSION_ROOT/source/sqlite-autoconf-3530400.tar.gz" || \
    die "既存runtimeのsource archiveがbundleと一致しません"
else
  STAGING_ROOT="$(mktemp -d "$RUNTIME_ROOT/.stage-$VERSION.XXXXXX")"
  # 引数: なし。STAGING_ROOTが示す作成途中のversion directoryを使う。
  # 戻り値: rm成功なら0。失敗時はtrap実行時の終了statusへ影響する。
  # 副作用: 不完全なstaging directoryを再帰削除する。
  cleanup() {
    # build失敗・中断時も一時展開だけを除去し、公開済みversion directoryには触れない。
    rm -rf -- "${STAGING_ROOT:-}"
  }
  trap cleanup EXIT
  install -d -m 0755 "$STAGING_ROOT/lib" "$STAGING_ROOT/source" \
    "$STAGING_ROOT/build"
  install -m 0644 "$SOURCE_LIBRARY" \
    "$STAGING_ROOT/lib/libsqlite3.so.$VERSION"
  ln -s "libsqlite3.so.$VERSION" "$STAGING_ROOT/lib/libsqlite3.so.0"
  ln -s "libsqlite3.so.$VERSION" "$STAGING_ROOT/lib/libsqlite3.so"
  install -m 0644 "$SQLITE_BUNDLE_ROOT/sqlite-autoconf-3530400.tar.gz" \
    "$STAGING_ROOT/source/sqlite-autoconf-3530400.tar.gz"
  install -m 0644 "$SQLITE_BUNDLE_ROOT/SHA256SUMS" "$STAGING_ROOT/SHA256SUMS"
  install -m 0644 "$SQLITE_BUNDLE_ROOT/BUILD_INFO.md" "$STAGING_ROOT/BUILD_INFO.md"
  install -m 0644 "$SQLITE_BUNDLE_ROOT/THIRD_PARTY_NOTICES.md" \
    "$STAGING_ROOT/THIRD_PARTY_NOTICES.md"
  install -m 0644 "$SQLITE_BUNDLE_ROOT/LICENSE-ATTRIBUTION.md" \
    "$STAGING_ROOT/LICENSE-ATTRIBUTION.md"
  install -m 0755 "$SQLITE_BUNDLE_ROOT/build_private_sqlite.sh" \
    "$STAGING_ROOT/build_private_sqlite.sh"
  chown -R root:root "$STAGING_ROOT"
  chmod -R u=rwX,go=rX "$STAGING_ROOT"
  mv -T -- "$STAGING_ROOT" "$VERSION_ROOT"
  STAGING_ROOT=""
  trap - EXIT
fi

[[ "$(stat -c '%u:%g:%a' "$VERSION_ROOT")" == "0:0:755" ]] || \
  die "runtime version directory must be root-owned mode 0755"
for directory in lib source build; do
  [[ "$(stat -c '%u:%g:%a' "$VERSION_ROOT/$directory")" == "0:0:755" ]] || \
    die "$directory directory must be root-owned mode 0755"
done
[[ "$(stat -c '%u:%g:%a' "$VERSION_ROOT/lib/libsqlite3.so.$VERSION")" == \
  "0:0:644" ]] || die "SQLite library must be root-owned mode 0644"
[[ "$(readlink "$VERSION_ROOT/lib/libsqlite3.so.0")" == \
  "libsqlite3.so.$VERSION" ]] || die "libsqlite3.so.0 symlink is invalid"
[[ -r "$VERSION_ROOT/source/sqlite-autoconf-3530400.tar.gz" && \
  -r "$VERSION_ROOT/build_private_sqlite.sh" && \
  -r "$VERSION_ROOT/THIRD_PARTY_NOTICES.md" ]] || \
  die "runtime source, build recipe or license notice is missing"

if [[ -L "$CURRENT_LINK" && "$(readlink "$CURRENT_LINK")" == "$VERSION" ]]; then
  exit 0
fi
# version directoryの所有権とsymlink内容を検証してから一時linkをrenameする。
# 同一filesystem内renameでcurrentの切替を原子的に行う。
if [[ -e "$CURRENT_LINK" || -L "$CURRENT_LINK" ]]; then
  [[ -L "$CURRENT_LINK" ]] || die "runtime current path is not a symlink"
  CURRENT_TARGET="$(readlink "$CURRENT_LINK")"
  [[ "$CURRENT_TARGET" =~ ^[0-9][A-Za-z0-9.+_-]*$ ]] || \
    die "runtime current link target is not a version directory"
fi
TEMP_LINK="$RUNTIME_ROOT/.current.$$"
rm -f -- "$TEMP_LINK"
trap 'rm -f -- "$TEMP_LINK"' EXIT
ln -s "$VERSION" "$TEMP_LINK"
mv -Tf -- "$TEMP_LINK" "$CURRENT_LINK"
trap - EXIT
