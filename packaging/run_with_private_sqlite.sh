#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${EVENT_LOGGER_APP_ROOT:-/opt/event-logger}"
RUNTIME_ROOT="${EVENT_LOGGER_PRIVATE_SQLITE_ROOT:-$APP_ROOT/runtime/sqlite}"
LIBRARY_DIR="$RUNTIME_ROOT/current/lib"

# 必須コマンドが与えられていない場合にライブラリ設定だけを行わない。
if [[ "$#" -eq 0 ]]; then
  printf 'private SQLite runner: command is required\n' >&2
  exit 2
fi
if [[ ! -r "$LIBRARY_DIR/libsqlite3.so.0" ]]; then
  printf 'private SQLite runner: fixed private SQLite runtime is missing\n' >&2
  exit 1
fi
if [[ ! -f "$LIBRARY_DIR/libsqlite3.so.3.53.4" ]]; then
  printf 'private SQLite runner: runtime version does not match the release\n' >&2
  exit 1
fi

export LD_LIBRARY_PATH="$LIBRARY_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$@"
