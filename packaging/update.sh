#!/usr/bin/env bash
set -Eeuo pipefail

# Release archiveを検証し、current symlinkを原子的に切り替える更新器。
# checksumが検証できないassetは適用しない。失敗時は旧releaseとDB backupへ戻す。

APP_ROOT="${EVENT_LOGGER_APP_ROOT:-/opt/event-logger}"
DATA_ROOT="${EVENT_LOGGER_DATA_ROOT:-/var/lib/event-logger}"
CONFIG_ROOT="${EVENT_LOGGER_CONFIG_ROOT:-/etc/event-logger}"
UPDATE_STATUS_FILE="${EVENT_LOGGER_UPDATE_STATUS_FILE:-$DATA_ROOT/update-status.json}"
CONTROL_SOCKET_PATH="${EVENT_LOGGER_SOCKET:-/run/event-logger/control.sock}"
SERVICE_USER="event-logger"
ARCHIVE=""
CHECKSUM_FILE=""
VERSION=""
# 同じ版・同じ秒の再試行も区別する。各更新プロセス内では同じIDを使う。
UPDATE_ATTEMPT_ID="${EVENT_LOGGER_UPDATE_ATTEMPT_ID:-$(date -u +%Y%m%dT%H%M%S)-$$}"
SOURCE_ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# 更新の進捗・成否をWebから読める状態ファイルへ反映する。
write_status() {
  local state="$1"
  local message="$2"
  local directory
  directory="$(dirname -- "$UPDATE_STATUS_FILE")"
  mkdir -p -- "$directory"
  local temporary="$UPDATE_STATUS_FILE.tmp.$$"
  printf '{"status":"%s","version":"%s","message":"%s","updated_at":"%s","attempt_id":"%s"}\n' \
    "$state" "$VERSION" "$message" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$UPDATE_ATTEMPT_ID" >"$temporary"
  chown "$SERVICE_USER:$SERVICE_USER" "$temporary"
  chmod 0640 "$temporary"
  mv -f -- "$temporary" "$UPDATE_STATUS_FILE"
}

die() {
  printf 'event-logger update: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --sha256) CHECKSUM_FILE="${2:-}"; shift 2 ;;
    --version) VERSION="${2:-}"; shift 2 ;;
    *) die "usage: $0 --archive FILE --sha256 SHA256SUMS --version VERSION" ;;
  esac
done
[[ "${EUID}" -eq 0 ]] || die "root権限で実行してください"
command -v systemctl >/dev/null 2>&1 || die "systemdが必要です"
[[ -f "$ARCHIVE" && -f "$CHECKSUM_FILE" && -n "$VERSION" ]] || die "archive/checksum/versionが必要です"
[[ "$VERSION" =~ ^[A-Za-z0-9._-]+$ ]] || die "versionが不正です"
ARCHIVE_NAME="$(basename -- "$ARCHIVE")"
[[ "$ARCHIVE_NAME" == "event-logger-$VERSION.tar.zst" ]] || die "archive名とversionが一致しません"
EXPECTED_SHA256="$(awk -v target="$ARCHIVE_NAME" '
  NF == 2 && $2 == target { count += 1; value = $1 }
  END { if (count != 1) exit 1; print value }
' "$CHECKSUM_FILE")" || die "checksum一覧に対象archiveが1件ありません"
EXPECTED_SHA256="${EXPECTED_SHA256,,}"
[[ "$EXPECTED_SHA256" =~ ^[[:xdigit:]]{64}$ ]] || die "checksum形式が不正です"
ACTUAL_SHA256="$(sha256sum -- "$ARCHIVE" | awk '{print $1}')"
[[ "$ACTUAL_SHA256" == "$EXPECTED_SHA256" ]] || die "SHA-256検証に失敗しました"
# installerと同じ排他lockを、OS dependencyやrelease/dataへ触れる前に取得する。
LOCK_DIRECTORY="/run/lock/event-logger-apply.lock"
install -d -m 0755 /run/lock
if ! mkdir "$LOCK_DIRECTORY" 2>/dev/null; then
  die "別のinstall/updateが実行中です"
fi
trap 'if [[ -n "${INSTALL_STATE_DIR:-}" ]]; then rm -rf -- "$INSTALL_STATE_DIR"; fi; rmdir "$LOCK_DIRECTORY" 2>/dev/null || true' EXIT
# shellcheck disable=SC1091
. "$SOURCE_ROOT/packaging/python-build-dependencies.sh"
ensure_python_build_dependencies

NEW_RELEASE="$APP_ROOT/releases/$VERSION"
[[ ! -e "$NEW_RELEASE" ]] || die "release $VERSION は既に配置済みです"
STAGING="$APP_ROOT/.update-$VERSION.$$"
BACKUP="$DATA_ROOT/backups/update-$(date -u +%Y%m%dT%H%M%SZ).db"
OLD_RELEASE=""
OLD_VENV=""
SERVICES_STOPPED=0
BACKUP_READY=0
CURRENT_SWITCHED=0
RELEASE_MOVED=0
ROLLBACK_FAILED=0
PRIVATE_RUNTIME_READY=0
PRIVATE_SQLITE_UNITS_READY=0
UNIT_NAMES=(
  event-loggerd.service
  event-logger-web.service
  event-logger-hostname.service
  event-logger-maintenance.service
  event-logger-maintenance.timer
  event-logger-update.service
)
RUNTIME_UNITS=(
  event-loggerd.service
  event-logger-web.service
  event-logger-hostname.service
  event-logger-maintenance.service
  event-logger-maintenance.timer
)
INSTALL_STATE_DIR="$(mktemp -d /run/event-logger-update.XXXXXX)"
chmod 0700 "$INSTALL_STATE_DIR"
declare -A PREVIOUS_UNIT_EXISTS=()
declare -A PREVIOUS_UNIT_ACTIVE=()
declare -A PREVIOUS_UNIT_ENABLED=()
declare -A PREVIOUS_OVERRIDE_EXISTS=()
for unit in "${UNIT_NAMES[@]}"; do
  PREVIOUS_UNIT_EXISTS["$unit"]=0
  PREVIOUS_UNIT_ACTIVE["$unit"]=0
  PREVIOUS_UNIT_ENABLED["$unit"]=0
  PREVIOUS_OVERRIDE_EXISTS["$unit"]=0
  if systemctl cat "$unit" >/dev/null 2>&1; then
    PREVIOUS_UNIT_EXISTS["$unit"]=1
    if systemctl is-active --quiet "$unit"; then
      PREVIOUS_UNIT_ACTIVE["$unit"]=1
    fi
    if systemctl is-enabled --quiet "$unit"; then
      PREVIOUS_UNIT_ENABLED["$unit"]=1
    fi
  fi
  if [[ -f "/etc/systemd/system/$unit" ]]; then
    PREVIOUS_OVERRIDE_EXISTS["$unit"]=1
    cp -p "/etc/systemd/system/$unit" "$INSTALL_STATE_DIR/$unit"
  fi
done
mkdir -p "$STAGING" "$DATA_ROOT/backups"

# 新しい配布物のsystemd定義をインストールする。
install_release_units() {
  local release_root="$1"
  local unit
  for unit in "${UNIT_NAMES[@]}"; do
    [[ -f "$release_root/packaging/systemd/$unit" ]] || return 1
    install -m 0644 "$release_root/packaging/systemd/$unit" \
      "/etc/systemd/system/$unit" || return 1
  done
  systemctl daemon-reload
}

# 失敗時は更新前のservice定義へ戻す。
restore_product_units() {
  local unit
  local restore_path
  for unit in "${UNIT_NAMES[@]}"; do
    if [[ "${PREVIOUS_OVERRIDE_EXISTS[$unit]}" -eq 1 ]]; then
      restore_path="/etc/systemd/system/.$unit.restore.$$"
      cp -p "$INSTALL_STATE_DIR/$unit" "$restore_path" || return 1
      mv -f -- "$restore_path" "/etc/systemd/system/$unit" || return 1
    else
      rm -f "/etc/systemd/system/$unit" || return 1
    fi
  done
  systemctl daemon-reload
}

# 更新前の自動起動設定を復元する。
restore_product_enabled_states() {
  local unit
  local failed=0
  for unit in "${UNIT_NAMES[@]}"; do
    if [[ "${PREVIOUS_UNIT_ENABLED[$unit]}" -eq 1 ]]; then
      systemctl enable "$unit" || failed=1
    elif systemctl cat "$unit" >/dev/null 2>&1; then
      systemctl disable "$unit" >/dev/null 2>&1 || failed=1
    fi
  done
  return "$failed"
}

# 更新前の稼働状態を復元する。
restore_product_active_states() {
  local unit
  local failed=0
  for unit in "${RUNTIME_UNITS[@]}"; do
    if [[ "${PREVIOUS_UNIT_EXISTS[$unit]}" -eq 0 ]]; then
      continue
    elif [[ "${PREVIOUS_UNIT_ACTIVE[$unit]}" -eq 1 ]]; then
      systemctl start "$unit" || failed=1
    else
      systemctl stop "$unit" || failed=1
    fi
  done
  return "$failed"
}

# 途中失敗後のrollbackと一時資源の解放をまとめる。
cleanup_update() {
  local exit_status=$?
  if [[ "$exit_status" -ne 0 ]]; then
    # set -eで途中終了しても、停止済みserviceとcurrent symlinkを旧状態へ
    # 戻す。health check失敗だけをrollback対象にしてはいけない。
    if [[ "$SERVICES_STOPPED" -eq 1 ]]; then
      if systemctl cat event-logger-hostname.service >/dev/null 2>&1 && \
         ! systemctl stop event-logger-hostname.service; then
        ROLLBACK_FAILED=1
      fi
      if ! systemctl stop event-logger-maintenance.timer \
        event-logger-maintenance.service \
        event-logger-web.service event-loggerd.service; then
        ROLLBACK_FAILED=1
      fi
    fi
    if [[ "$CURRENT_SWITCHED" -eq 1 && -n "$OLD_RELEASE" ]]; then
      if ! ln -sfn "$OLD_RELEASE" "$APP_ROOT/current" || \
         ! ln -sfn "$OLD_VENV" "$APP_ROOT/venv"; then
        ROLLBACK_FAILED=1
      fi
      restore_product_enabled_states || ROLLBACK_FAILED=1
      if ! restore_product_units; then
        ROLLBACK_FAILED=1
      fi
    fi
    if [[ "$BACKUP_READY" -eq 1 && -n "$BACKUP" && "$ROLLBACK_FAILED" -eq 0 ]]; then
      if ! cp -- "$BACKUP" "$DATA_ROOT/logger.db" || \
         ! rm -f -- "$DATA_ROOT/logger.db-wal" "$DATA_ROOT/logger.db-shm" || \
         ! chown "$SERVICE_USER:$SERVICE_USER" "$DATA_ROOT/logger.db"; then
        ROLLBACK_FAILED=1
      fi
    fi
    # SQLite切替後にloader pathを反映できなければ、旧unitを再開しない。
    if [[ "$PRIVATE_RUNTIME_READY" -eq 1 && \
        "$PRIVATE_SQLITE_UNITS_READY" -eq 0 ]]; then
      ROLLBACK_FAILED=1
    fi
    if [[ "$SERVICES_STOPPED" -eq 1 && "$ROLLBACK_FAILED" -eq 0 ]]; then
      restore_product_active_states || ROLLBACK_FAILED=1
    fi
    if [[ "$ROLLBACK_FAILED" -eq 1 ]]; then
      # 再開途中で一部unitだけ起動した場合も、復旧未確認の状態を
      # 稼働中として残さない。systemctlの失敗は元のrollback失敗を隠さない。
      systemctl stop event-logger-maintenance.timer \
        event-logger-maintenance.service event-logger-web.service \
        event-loggerd.service >/dev/null 2>&1 || true
      printf 'event-logger update: rollback failed; services remain stopped\n' >&2
      write_status "rollback_failed" "rollback failed; services remain stopped" || \
        printf 'event-logger update: could not write update status\n' >&2
    else
      write_status "failed" "update failed and rollback completed" || \
        printf 'event-logger update: could not write update status\n' >&2
      if [[ "$RELEASE_MOVED" -eq 1 ]]; then
        rm -rf -- "$NEW_RELEASE"
      fi
    fi
  fi
  rm -rf -- "$STAGING" || true
  rm -rf -- "$INSTALL_STATE_DIR" || true
  if [[ "$exit_status" -eq 0 ]]; then
    write_status "completed" "update completed" || \
      printf 'event-logger update: could not write update status\n' >&2
  fi
  rmdir "${LOCK_DIRECTORY:-}" 2>/dev/null || true
  return "$exit_status"
}
trap cleanup_update EXIT
write_status "started" "update started"
if tar --zstd -t -f "$ARCHIVE" | awk '/^\// || /(^|\/)[.][.]($|\/)/ {bad=1} END {exit bad}'; then
  :
else
  die "archiveに危険なパスが含まれています"
fi
tar -x --zstd -f "$ARCHIVE" -C "$STAGING"
[[ -f "$STAGING/pyproject.toml" ]] || die "release archiveの構造が不正です"
# 現行releaseのPython環境にある検証器で、checksum済みarchive内Versionと
# installed Versionを比較する。新しいassetのコードをrootで実行する前に判定し、
# service停止・backup・migrationへ進む前にprerelease/downgrade/metadata不一致を拒否する。
OLD_RELEASE="$(readlink -f "$APP_ROOT/current")" || die "current releaseを特定できません"
OLD_VENV="$(readlink -f "$APP_ROOT/venv")" || die "current venvを特定できません"
[[ -f "$OLD_RELEASE/pyproject.toml" && -x "$OLD_VENV/bin/python" ]] || \
  die "current releaseのVersion metadataまたはPython環境がありません"
if ! "$OLD_VENV/bin/python" - "$OLD_RELEASE/pyproject.toml" \
  "$STAGING/pyproject.toml" "$VERSION" <<'PY'
import sys

from event_logger.maintenance.update import UpdateError, validate_update_candidate

try:
    validate_update_candidate(sys.argv[1], sys.argv[2], sys.argv[3])
except UpdateError as error:
    print(f"event-logger update: {error}", file=sys.stderr)
    raise SystemExit(1)
PY
then
  die "candidate must be a newer stable version matching its archive metadata"
fi
# archiveのルート権限を引き継ぐと、作成元checkoutが0700の場合に
# event-loggerユーザーがrelease配下を実行できないため、公開可能な権限へ揃える。
chmod 0755 "$STAGING"

mv "$STAGING" "$NEW_RELEASE"
RELEASE_MOVED=1
# venvはscript shebangに作成時の絶対pathを埋め込むため、最終release pathで
# 作成する。仮配置先で作ったvenvをmvしてはならない。
"${EVENT_LOGGER_PYTHON:-python3}" -m venv "$NEW_RELEASE/venv"
if [[ -n "${EVENT_LOGGER_WHEELHOUSE:-}" ]]; then
  "$NEW_RELEASE/venv/bin/pip" install --no-index --find-links "$EVENT_LOGGER_WHEELHOUSE" "$NEW_RELEASE"
else
  "$NEW_RELEASE/venv/bin/pip" install "$NEW_RELEASE"
fi

# download・検証・release/venv準備までは現Versionの記録を止めない。
SERVICES_STOPPED=1
systemctl stop event-logger-maintenance.timer event-logger-maintenance.service \
  event-logger-web.service event-loggerd.service
if systemctl cat event-logger-hostname.service >/dev/null 2>&1; then
  systemctl stop event-logger-hostname.service
fi
"$NEW_RELEASE/packaging/install_private_sqlite.sh"
PRIVATE_RUNTIME_READY=1
"$NEW_RELEASE/packaging/configure_private_sqlite_services.sh"
systemctl daemon-reload
PRIVATE_SQLITE_UNITS_READY=1
# backupはloggerd停止後に取得する。動作中のbackup後に停止すると、停止までに
# 保存されたeventがrollback時に失われる。
"$NEW_RELEASE/packaging/run_with_private_sqlite.sh" \
  "$OLD_VENV/bin/event-logger-backup" \
  --database "$DATA_ROOT/logger.db" --destination "$BACKUP"
BACKUP_READY=1

"$NEW_RELEASE/packaging/run_with_private_sqlite.sh" \
  "$NEW_RELEASE/venv/bin/event-logger-migrate" \
  --database "$DATA_ROOT/logger.db"
CURRENT_SWITCHED=1
ln -sfn "$NEW_RELEASE" "$APP_ROOT/current"
ln -sfn "$NEW_RELEASE/venv" "$APP_ROOT/venv"
chown -R root:root "$NEW_RELEASE"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_ROOT/logger.db"
chmod 0640 "$DATA_ROOT/logger.db"
install_release_units "$NEW_RELEASE" || die "release unit files are incomplete"
install -d -m 0755 "$CONFIG_ROOT"
if [[ ! -e "$CONFIG_ROOT/event-logger.env" ]]; then
  install -m 0640 -o root -g "$SERVICE_USER" \
    "$NEW_RELEASE/packaging/systemd/event-logger.env.example" \
    "$CONFIG_ROOT/event-logger.env"
fi
# 旧installで鍵未設定の場合も、新しいworkerを起動する前に共通鍵を用意する。
"$NEW_RELEASE/venv/bin/python" \
  "$NEW_RELEASE/packaging/ensure_web_secret.py" "$CONFIG_ROOT/event-logger.env"
chown "root:$SERVICE_USER" "$CONFIG_ROOT/event-logger.env"
chmod 0640 "$CONFIG_ROOT/event-logger.env"
systemctl enable "${UNIT_NAMES[@]}"
if [[ -f "$NEW_RELEASE/packaging/systemd/event-logger-hostname.service" ]]; then
  systemctl start event-logger-hostname.service
fi
systemctl start event-loggerd.service event-logger-web.service
# update-agentは現在実行中のupdate.shを同じservice cgroupで管理している。
# そのため、このshellから同期restartすると更新処理自身まで停止される。
# 別の通常shellから遅延・非同期でrestart要求だけをsystemdへ渡し、この処理が
# 終了してからcurrent symlink先の新releaseを常駐agentへ読み込ませる。

# 配布物切替え後に各サービスが正常に動くことを検証する。
health_check() {
  local attempt
  for attempt in $(seq 1 15); do
    if { [[ ! -f "$NEW_RELEASE/packaging/systemd/event-logger-hostname.service" ]] || \
         systemctl is-active --quiet event-logger-hostname.service; } && \
       systemctl is-active --quiet event-loggerd.service && \
       systemctl is-active --quiet event-logger-web.service && \
       systemctl is-active --quiet event-logger-update.service && \
       "$NEW_RELEASE/packaging/run_with_private_sqlite.sh" \
         "$NEW_RELEASE/venv/bin/event-logger-health" \
         --database "$DATA_ROOT/logger.db" \
         --socket "$CONTROL_SOCKET_PATH" \
         --url http://127.0.0.1/ \
         --attempts 1
    then
      return 0
    fi
    sleep 1
  done
  return 1
}

health_check || die "health check failed; rollbackを実行します"
systemctl start event-logger-maintenance.timer
nohup sh -c \
  'sleep 2; systemctl --no-block restart event-logger-update.service' \
  >/dev/null 2>&1 &

printf '更新完了: %s\n' "$VERSION"
