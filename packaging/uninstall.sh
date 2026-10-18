#!/usr/bin/env bash
set -Eeuo pipefail

# インストール先と保存データは別管理し、削除範囲を明示する。
APP_ROOT="${EVENT_LOGGER_APP_ROOT:-/opt/event-logger}"
DATA_ROOT="${EVENT_LOGGER_DATA_ROOT:-/var/lib/event-logger}"
CONFIG_ROOT="${EVENT_LOGGER_CONFIG_ROOT:-/etc/event-logger}"
SERVICE_USER="event-logger"

# 異常時は終了コードを返し、部分削除を成功と扱わない。
die() {
  printf 'event-logger uninstall: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "root権限で実行してください"
case "$APP_ROOT" in
  ""|"/"|"/opt"|"/var"|"/etc") die "APP_ROOTの指定が広すぎます" ;;
esac
case "$DATA_ROOT" in
  ""|"/"|"/var"|"/var/lib") die "DATA_ROOTの指定が広すぎます" ;;
esac
case "$CONFIG_ROOT" in
  ""|"/"|"/etc") die "CONFIG_ROOTの指定が広すぎます" ;;
esac
purge=0
if [[ "${1:-}" == "--purge" ]]; then
  purge=1
elif [[ "${1:-}" != "" ]]; then
  die "usage: $0 [--purge]"
fi

unit_names=(
  event-logger-update.service
  event-logger-maintenance.service
  event-logger-maintenance.timer
  event-logger-hostname.service
  event-logger-web.service
  event-loggerd.service
)
installed_units=()
for unit in "${unit_names[@]}"; do
  if [[ -e "/etc/systemd/system/$unit" || -L "/etc/systemd/system/$unit" ]]; then
    installed_units+=("$unit")
  elif systemctl is-active --quiet "$unit"; then
    # 管理対象のunit fileが先に消されていても、残っているserviceは停止する。
    installed_units+=("$unit")
  else
    status=$?
    [[ "$status" -eq 3 || "$status" -eq 4 ]] ||
      die "$unit の存在状態を確認できません"
  fi
done

if ((${#installed_units[@]} > 0)); then
  systemctl stop "${installed_units[@]}" || die "unitの停止に失敗しました。ファイルは保持します"
  for unit in "${installed_units[@]}"; do
    if systemctl is-active --quiet "$unit"; then
      die "$unit が停止していません。ファイルは保持します"
    else
      status=$?
      [[ "$status" -eq 3 ]] || die "$unit の停止状態を確認できません。ファイルは保持します"
    fi
  done
  systemctl disable "${installed_units[@]}" || die "unitの無効化に失敗しました。ファイルは保持します"
fi

rm -f /etc/systemd/system/event-loggerd.service \
  /etc/systemd/system/event-logger-web.service \
  /etc/systemd/system/event-logger-hostname.service \
  /etc/systemd/system/event-logger-maintenance.service \
  /etc/systemd/system/event-logger-maintenance.timer \
  /etc/systemd/system/event-logger-update.service
for unit in event-loggerd.service event-logger-web.service \
  event-logger-hostname.service event-logger-maintenance.service \
  event-logger-update.service; do
  dropin_directory="/etc/systemd/system/$unit.d"
  rm -f -- "$dropin_directory/99-event-logger-private-sqlite.conf"
  rmdir "$dropin_directory" 2>/dev/null || true
done
systemctl daemon-reload

# 通常uninstallではDB、設定、backupを残す。アプリ領域だけを削除する。
rm -rf -- "$APP_ROOT"
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
  userdel "$SERVICE_USER" 2>/dev/null || true
fi

if [[ "$purge" -eq 1 ]]; then
  printf 'DBと設定も削除します。続行するには PURGE と入力してください: '
  read -r confirmation
  [[ "$confirmation" == "PURGE" ]] || die "purgeを中止しました"
  rm -rf -- "$DATA_ROOT" "$CONFIG_ROOT"
fi

printf 'アンインストール完了 (DB/設定は%s)。\n' \
  "$([[ "$purge" -eq 1 ]] && echo '削除済み' || echo '保持')"
