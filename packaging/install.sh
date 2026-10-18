#!/usr/bin/env bash
set -Eeuo pipefail

# Raspberry Pi OS Liteへ、現在のreleaseをroot所有で配置するidempotent installer。
# DBと/etc設定は既存値を上書きしない。実行前にHAT revisionとOSを確認する。

APP_ROOT="${EVENT_LOGGER_APP_ROOT:-/opt/event-logger}"
DATA_ROOT="${EVENT_LOGGER_DATA_ROOT:-/var/lib/event-logger}"
CONFIG_ROOT="${EVENT_LOGGER_CONFIG_ROOT:-/etc/event-logger}"
UPDATE_VERIFIED_DIR="${EVENT_LOGGER_UPDATE_VERIFIED_DIR:-/var/lib/event-logger-update/verified}"
SERVICE_USER="event-logger"
SOURCE_ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
APP_VERSION="${EVENT_LOGGER_VERSION:-}"
PYTHON_BIN="${EVENT_LOGGER_PYTHON:-python3}"
WHEELHOUSE="${EVENT_LOGGER_WHEELHOUSE:-}"
CONTROL_SOCKET_PATH="${EVENT_LOGGER_SOCKET:-/run/event-logger/control.sock}"

# 引数: 1個以上の診断文。標準エラーへprefix付きで出力する。
# 戻り値: 戻らない。status 1でscriptを終了し、EXIT trapにcleanup/rollbackを委ねる。
# 副作用: 標準エラー出力とprocess終了。
die() {
  printf 'event-logger install: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "root権限で実行してください"
[[ -r /etc/os-release ]] || die "/etc/os-releaseがありません"
# Raspberry Pi OS 64-bitはDebian由来のためID=debianになる版がある。
# /proc/device-tree/modelも確認するので、Pi上のdebian/raspbianだけを許可する。
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
  raspbian|debian) ;;
  *) die "Raspberry Pi OS (ID=raspbian/debian)以外では実行しません" ;;
esac
[[ -r /proc/device-tree/model ]] || die "Raspberry Piのmodel情報を取得できません"
grep -q 'Raspberry Pi' /proc/device-tree/model || die "Raspberry Pi実機ではありません"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "$PYTHON_BIN がありません"
python_version="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
[[ "$python_version" == "3.13" ]] || die "Python 3.13が必要です (detected: $python_version)"
command -v systemctl >/dev/null 2>&1 || die "systemdが必要です"

# installerとRelease updaterが同時にDB/releaseを切り替えないよう、
# rootのみ作成でき、再起動で消えるruntime lockを共有する。
LOCK_DIRECTORY="/run/lock/event-logger-apply.lock"
install -d -m 0755 /run/lock
if ! mkdir "$LOCK_DIRECTORY" 2>/dev/null; then
  die "別のinstall/updateが実行中です"
fi
trap 'rmdir "$LOCK_DIRECTORY" 2>/dev/null || true' EXIT

# shellcheck disable=SC1091
. "$SOURCE_ROOT/packaging/python-build-dependencies.sh"
ensure_python_build_dependencies

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
if getent group gpio >/dev/null 2>&1; then
  # Raspberry Pi OSのgpiochip deviceへアクセスできるよう、daemon/webの
  # 専用userへ既存gpio groupだけを付与する。groupがない環境ではGPIO初期化
  # が失敗するため、インストーラー側で黙って作成して別権限を広げない。
  usermod -a -G gpio "$SERVICE_USER"
fi
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_ROOT"
# install -dは既存directoryのmodeを直さない。旧版からの更新でも
# event logを他OSユーザーへ公開しないよう、所有者とmodeを明示的に是正する。
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_ROOT"
chmod 0750 "$DATA_ROOT"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_ROOT/updates"
# update-agentが検証済みarchiveを置く領域は、Web service userが差し替え
# できないroot専用にする。DATA_ROOTはservice所有なので、その配下へ置くと
# 親directoryからunlink/raceできるため、/var/lib直下の専用root領域を使う。
install -d -m 0700 -o root -g root "$UPDATE_VERIFIED_DIR"
install -d -m 0755 "$APP_ROOT/releases" "$CONFIG_ROOT"

if [[ -z "$APP_VERSION" ]]; then
  APP_VERSION="$(awk -F'"' '/^version =/{print $2; exit}' "$SOURCE_ROOT/pyproject.toml")"
fi
[[ -n "$APP_VERSION" ]] || die "pyproject.tomlからversionを読めません"
[[ "$APP_VERSION" =~ ^[A-Za-z0-9._-]+$ ]] || die "versionが不正です"
RELEASE_ROOT="$APP_ROOT/releases/$APP_VERSION"
STAGING_ROOT="$APP_ROOT/.install-$APP_VERSION.$$"
RELEASE_CREATED=0
CURRENT_LINKED=0
DATABASE_MIGRATION_STARTED=0
DATABASE_EXISTED=0
SERVICES_STOPPED=0
UNIT_FILES_CHANGED=0
INSTALL_SUCCEEDED=0
PRIVATE_RUNTIME_READY=0
PRIVATE_SQLITE_UNITS_READY=0
PREVIOUS_CURRENT_TARGET="$(readlink "$APP_ROOT/current" 2>/dev/null || true)"
PREVIOUS_VENV_TARGET="$(readlink "$APP_ROOT/venv" 2>/dev/null || true)"
PREVIOUS_RELEASE="$(readlink -f "$APP_ROOT/current" 2>/dev/null || true)"
PREVIOUS_VENV="$(readlink -f "$APP_ROOT/venv" 2>/dev/null || true)"
DATABASE_BACKUP=""
INSTALL_STATE_DIR="$(mktemp -d /run/event-logger-install.XXXXXX)"
chmod 0700 "$INSTALL_STATE_DIR"
PRODUCT_UNITS=(
  event-logger-update.service
  event-logger-maintenance.timer
  event-logger-maintenance.service
  event-logger-hostname.service
  event-logger-web.service
  event-loggerd.service
)
declare -A PREVIOUS_UNIT_EXISTS=()
declare -A PREVIOUS_UNIT_ACTIVE=()
declare -A PREVIOUS_UNIT_ENABLED=()
declare -A PREVIOUS_OVERRIDE_EXISTS=()
for unit in "${PRODUCT_UNITS[@]}"; do
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

# 引数: なし。PRODUCT_UNITSのうちsystemdへ登録済みのunitを対象にする。
# 戻り値: 全stopが成功すれば0、systemctl失敗はset -e経由でscriptへ伝播する。
# 副作用: SERVICES_STOPPEDを先に立て、登録済み製品unitを停止する。
stop_product_services() {
  local unit
  SERVICES_STOPPED=1
  for unit in "${PRODUCT_UNITS[@]}"; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
      systemctl stop "$unit"
    fi
  done
}

# 引数: なし。PRODUCT_UNITSと起動前に保存したactive/enabled状態を使う。
# 戻り値: 復元処理に1つでも失敗があれば1、全て成功なら0。
# 副作用: unitごとにenable/disableとstart/stopを実行する。
restore_product_services() {
  local unit
  local failed=0
  for unit in "${PRODUCT_UNITS[@]}"; do
    if [[ "${PREVIOUS_UNIT_EXISTS[$unit]}" -eq 1 ]]; then
      if [[ "${PREVIOUS_UNIT_ENABLED[$unit]}" -eq 1 ]]; then
        systemctl enable "$unit" || failed=1
      else
        systemctl disable "$unit" || failed=1
      fi
      if [[ "${PREVIOUS_UNIT_ACTIVE[$unit]}" -eq 1 ]]; then
        systemctl start "$unit" || failed=1
      else
        systemctl stop "$unit" || failed=1
      fi
    else
      systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
  done
  return "$failed"
}

# 引数: なし。保存済みunit snapshotとPREVIOUS_OVERRIDE_EXISTSを使う。
# 戻り値: 全file復元とdaemon-reload成功で0、途中のfile操作失敗で1。
# 副作用: /etc/systemd/systemのunitを復元または削除し、systemd managerをreloadする。
restore_product_units() {
  local unit
  for unit in "${PRODUCT_UNITS[@]}"; do
    if [[ "${PREVIOUS_OVERRIDE_EXISTS[$unit]}" -eq 1 ]]; then
      install -m 0644 "$INSTALL_STATE_DIR/$unit" "/etc/systemd/system/$unit" || return 1
    else
      rm -f "/etc/systemd/system/$unit" || return 1
    fi
  done
  systemctl daemon-reload
}

# 引数: なし。EXIT trapが設定した直前statusとinstall状態flagを参照する。
# 戻り値: install本体の終了statusを返す。
# 副作用: rollback条件に応じservice停止、link/DB/unit復旧、temporary file削除を行う。
cleanup_install() {
  local exit_status=$?
  local rollback_failed=0
  rm -rf -- "$STAGING_ROOT" || true
  if [[ "$exit_status" -ne 0 && "$SERVICES_STOPPED" -eq 1 ]]; then
    # DBファイルを差し替える前に、途中まで起動した新版も含めて停止する。
    local unit
    for unit in "${PRODUCT_UNITS[@]}"; do
      if systemctl cat "$unit" >/dev/null 2>&1; then
        systemctl stop "$unit" || rollback_failed=1
      fi
    done
  fi
  if [[ "$exit_status" -ne 0 && "$rollback_failed" -eq 0 && \
      "$CURRENT_LINKED" -eq 1 ]]; then
    if [[ -n "$PREVIOUS_CURRENT_TARGET" ]]; then
      ln -sfn "$PREVIOUS_CURRENT_TARGET" "$APP_ROOT/current" || rollback_failed=1
    else
      rm -f "$APP_ROOT/current" || rollback_failed=1
    fi
    if [[ -n "$PREVIOUS_VENV_TARGET" ]]; then
      ln -sfn "$PREVIOUS_VENV_TARGET" "$APP_ROOT/venv" || rollback_failed=1
    else
      rm -f "$APP_ROOT/venv" || rollback_failed=1
    fi
  fi
  if [[ "$exit_status" -ne 0 && "$rollback_failed" -eq 0 && \
      "$DATABASE_MIGRATION_STARTED" -eq 1 ]]; then
    if [[ "$DATABASE_EXISTED" -eq 1 && -n "$DATABASE_BACKUP" && \
        -f "$DATABASE_BACKUP" ]]; then
      local restore_path="$DATA_ROOT/logger.db.restore.$$"
      if cp -- "$DATABASE_BACKUP" "$restore_path" && \
          chown "$SERVICE_USER:$SERVICE_USER" "$restore_path" && \
          chmod 0640 "$restore_path" && \
          mv -f -- "$restore_path" "$DATA_ROOT/logger.db" && \
          rm -f -- "$DATA_ROOT/logger.db-wal" "$DATA_ROOT/logger.db-shm"; then
        :
      else
        rm -f -- "$restore_path"
        rollback_failed=1
      fi
    elif [[ "$DATABASE_EXISTED" -eq 0 ]]; then
      rm -f -- "$DATA_ROOT/logger.db" "$DATA_ROOT/logger.db-wal" \
        "$DATA_ROOT/logger.db-shm" || rollback_failed=1
    else
      rollback_failed=1
    fi
  fi
  if [[ "$exit_status" -ne 0 && "$rollback_failed" -eq 0 && \
      "$UNIT_FILES_CHANGED" -eq 1 ]]; then
    restore_product_units || rollback_failed=1
  fi
  if [[ "$exit_status" -ne 0 && "$SERVICES_STOPPED" -eq 1 ]]; then
    # SQLite切替後にservice用loader pathを反映できなければ、旧unitを再開しない。
    if [[ "$PRIVATE_RUNTIME_READY" -eq 1 && \
        "$PRIVATE_SQLITE_UNITS_READY" -eq 0 ]]; then
      rollback_failed=1
    fi
    if [[ "$rollback_failed" -eq 0 ]]; then
      restore_product_services || rollback_failed=1
    fi
    if [[ "$rollback_failed" -eq 1 ]]; then
      for unit in "${PRODUCT_UNITS[@]}"; do
        systemctl stop "$unit" >/dev/null 2>&1 || true
      done
      printf 'rollback failed; services remain stopped\n' \
        >"$DATA_ROOT/install-failed"
      chmod 0600 "$DATA_ROOT/install-failed"
      printf 'event-logger install: rollback failed; services were stopped\n' >&2
    fi
  fi
  if [[ "$RELEASE_CREATED" -eq 1 && "$INSTALL_SUCCEEDED" -eq 0 && \
      "$rollback_failed" -eq 0 ]]; then
    rm -rf -- "$RELEASE_ROOT"
  fi
  rm -rf -- "$INSTALL_STATE_DIR"
  rmdir "$LOCK_DIRECTORY" 2>/dev/null || true
  return "$exit_status"
}
trap cleanup_install EXIT
if [[ -e "$RELEASE_ROOT" ]]; then
  # 同じversionの再実行は既存releaseを再利用し、DB/設定を壊さない。
  for required_binary in event-loggerd event-logger-web event-logger-migrate \
    event-logger-health event-logger-backup event-logger-update-agent; do
    [[ -x "$RELEASE_ROOT/venv/bin/$required_binary" ]] || \
      die "既存release $APP_VERSION の $required_binary がありません"
  done
else
  mkdir -p "$STAGING_ROOT"

  # 開発用cacheや実機で不要なSitesデモを製品releaseへ混ぜない。
  tar -C "$SOURCE_ROOT" \
    --exclude='.venv' \
    --exclude='.git' \
    --exclude='.mypy_cache' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='*.egg-info' \
    --exclude='sites-demo' \
    -cf - . | tar -C "$STAGING_ROOT" -xf -

  mv "$STAGING_ROOT" "$RELEASE_ROOT"
  RELEASE_CREATED=1
  # venvのscript shebangは作成時の絶対pathを埋め込むため、仮配置先で
  # 作ったvenvを移動しない。releaseの最終pathで作り直してから公開する。
  rm -rf -- "$RELEASE_ROOT/venv"
  "$PYTHON_BIN" -m venv "$RELEASE_ROOT/venv"
  if [[ -n "$WHEELHOUSE" ]]; then
    "$RELEASE_ROOT/venv/bin/pip" install --no-index --find-links "$WHEELHOUSE" "$RELEASE_ROOT"
  else
    "$RELEASE_ROOT/venv/bin/pip" install "$RELEASE_ROOT"
  fi
fi
# migration/DB backup中の同時書込みと、DBを開いたままの置換を防ぐ。
# 既存データのbackupは停止後に取得し、backup自体のintegrityもCLIで確認する。
stop_product_services
"$SOURCE_ROOT/packaging/install_private_sqlite.sh"
PRIVATE_RUNTIME_READY=1
"$SOURCE_ROOT/packaging/configure_private_sqlite_services.sh"
systemctl daemon-reload
PRIVATE_SQLITE_UNITS_READY=1
if [[ -f "$DATA_ROOT/logger.db" ]]; then
  DATABASE_EXISTED=1
  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_ROOT/backups"
  DATABASE_BACKUP="$DATA_ROOT/backups/install-$APP_VERSION-$(date -u +%Y%m%dT%H%M%SZ)-$$.db"
  BACKUP_VENV="$PREVIOUS_VENV"
  # readlink -fは存在しないlink先も文字列として返す場合がある。uninstall後に
  # DBだけを残して再導入する経路では、旧backup CLIの実在まで確認してfallbackする。
  if [[ -z "$BACKUP_VENV" || ! -x "$BACKUP_VENV/bin/event-logger-backup" ]]; then
    BACKUP_VENV="$RELEASE_ROOT/venv"
  fi
  "$SOURCE_ROOT/packaging/run_with_private_sqlite.sh" \
    "$BACKUP_VENV/bin/event-logger-backup" \
    --database "$DATA_ROOT/logger.db" --destination "$DATABASE_BACKUP"
fi
DATABASE_MIGRATION_STARTED=1
"$SOURCE_ROOT/packaging/run_with_private_sqlite.sh" \
  "$RELEASE_ROOT/venv/bin/event-logger-migrate" \
  --database "$DATA_ROOT/logger.db"
# current/venvを片方だけ切り替えた場合にもrollback trapを確実に走らせる。
CURRENT_LINKED=1
ln -sfn "$RELEASE_ROOT" "$APP_ROOT/current"
ln -sfn "$RELEASE_ROOT/venv" "$APP_ROOT/venv"
chown -R root:root "$APP_ROOT"
chmod -R u=rwX,go=rX "$APP_ROOT"

UNIT_FILES_CHANGED=1
install -m 0644 "$SOURCE_ROOT/packaging/systemd/event-loggerd.service" /etc/systemd/system/
install -m 0644 "$SOURCE_ROOT/packaging/systemd/event-logger-web.service" /etc/systemd/system/
install -m 0644 "$SOURCE_ROOT/packaging/systemd/event-logger-hostname.service" /etc/systemd/system/
install -m 0644 "$SOURCE_ROOT/packaging/systemd/event-logger-maintenance.service" /etc/systemd/system/
install -m 0644 "$SOURCE_ROOT/packaging/systemd/event-logger-maintenance.timer" /etc/systemd/system/
install -m 0644 "$SOURCE_ROOT/packaging/systemd/event-logger-update.service" /etc/systemd/system/
if [[ ! -e "$CONFIG_ROOT/event-logger.env" ]]; then
  install -m 0640 -o root -g "$SERVICE_USER" \
    "$SOURCE_ROOT/packaging/systemd/event-logger.env.example" "$CONFIG_ROOT/event-logger.env"
fi
# Gunicornはworkerごとに別processなので、Flaskのcookie署名鍵を1つの
# EnvironmentFileへ保存する。更新後やinstaller再実行でも鍵を作り直さない。
"$PYTHON_BIN" \
  "$SOURCE_ROOT/packaging/ensure_web_secret.py" "$CONFIG_ROOT/event-logger.env"
chown "root:$SERVICE_USER" "$CONFIG_ROOT/event-logger.env"
chmod 0640 "$CONFIG_ROOT/event-logger.env"

chown "$SERVICE_USER:$SERVICE_USER" "$DATA_ROOT/logger.db"
chmod 0640 "$DATA_ROOT/logger.db"
systemctl daemon-reload
systemctl enable event-logger-hostname.service event-loggerd.service event-logger-web.service event-logger-maintenance.timer event-logger-update.service
systemctl restart event-logger-hostname.service
systemctl restart event-loggerd.service
systemctl restart event-logger-web.service
systemctl restart event-logger-maintenance.timer
systemctl restart event-logger-update.service
systemctl is-active --quiet event-loggerd.service || die "event-loggerdの起動に失敗しました"
systemctl is-active --quiet event-logger-web.service || die "Web serviceの起動に失敗しました"
systemctl is-active --quiet event-logger-update.service || die "update-agentの起動に失敗しました"
"$SOURCE_ROOT/packaging/run_with_private_sqlite.sh" \
  "$APP_ROOT/venv/bin/event-logger-health" \
  --database "$DATA_ROOT/logger.db" \
  --socket "$CONTROL_SOCKET_PATH" \
  --url http://127.0.0.1/ \
  --attempts 15 \
  --interval 1 || die "health checkに失敗しました"
INSTALL_SUCCEEDED=1

printf 'インストール完了: http://%s/\n' "$(hostname -I 2>/dev/null | awk '{print $1}')"
