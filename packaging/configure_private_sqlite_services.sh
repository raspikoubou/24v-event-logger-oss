#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${EVENT_LOGGER_APP_ROOT:-/opt/event-logger}"
RUNTIME_ROOT="${EVENT_LOGGER_PRIVATE_SQLITE_ROOT:-$APP_ROOT/runtime/sqlite}"
LIBRARY_DIR="$RUNTIME_ROOT/current/lib"
DROPIN_NAME="99-event-logger-private-sqlite.conf"
UNIT_NAMES=(
  event-loggerd.service
  event-logger-web.service
  event-logger-hostname.service
  event-logger-maintenance.service
  event-logger-update.service
)

die() {
  printf 'private SQLite services: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "root権限で実行してください"
[[ -r "$LIBRARY_DIR/libsqlite3.so.0" ]] || \
  die "fixed private SQLite runtime is not installed"
case "$LIBRARY_DIR" in
  *[[:space:]]*) die "runtime path must not contain whitespace" ;;
esac

for unit in "${UNIT_NAMES[@]}"; do
  dropin_directory="/etc/systemd/system/$unit.d"
  [[ ! -L "$dropin_directory" ]] || die "$dropin_directory must not be a symlink"
  install -d -m 0755 -o root -g root "$dropin_directory"
  chown root:root "$dropin_directory"
  chmod 0755 "$dropin_directory"
  dropin_path="$dropin_directory/$DROPIN_NAME"
  temporary_path="$dropin_path.tmp.$$"
  printf '[Service]\nEnvironment=LD_LIBRARY_PATH=%s\n' "$LIBRARY_DIR" \
    >"$temporary_path"
  if [[ -e "$dropin_path" || -L "$dropin_path" ]]; then
    [[ -f "$dropin_path" && ! -L "$dropin_path" ]] || {
      rm -f -- "$temporary_path"
      die "$dropin_path must be a regular file"
    }
    [[ "$(stat -c '%u:%g:%a' "$dropin_path")" == "0:0:644" ]] || {
      rm -f -- "$temporary_path"
      die "$dropin_path must be root-owned mode 0644"
    }
    cmp -s "$temporary_path" "$dropin_path" || {
      rm -f -- "$temporary_path"
      die "$dropin_path exists with different contents"
    }
    rm -f -- "$temporary_path"
  else
    chown root:root "$temporary_path"
    chmod 0644 "$temporary_path"
    mv -T -- "$temporary_path" "$dropin_path"
  fi
done
