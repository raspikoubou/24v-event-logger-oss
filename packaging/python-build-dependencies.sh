#!/usr/bin/env bash

# lgpioのwheelが対象Python/ARM64向けに提供されない場合でも、
# install/updateのvenv構築を同じOS前提で再現できるようにする。
# 対象Pythonでwheelを利用できない場合だけビルド依存の導入を検討する。
ensure_python_build_dependencies() {
  # wheelhouseを指定した導入では、依存wheelが揃っていればコンパイル不要である。
  # オフライン復旧時にAPT更新を要求して導入不能にしない。
  if [[ -n "${EVENT_LOGGER_WHEELHOUSE:-}" ]]; then
    return 0
  fi
  local package
  local status
  local missing=()

  for package in python3-dev swig liblgpio-dev; do
    status="$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)"
    if [[ "$status" != "install ok installed" ]]; then
      missing+=("$package")
    fi
  done
  if [[ "${#missing[@]}" -eq 0 ]]; then
    return 0
  fi
  command -v apt-get >/dev/null 2>&1 || {
    printf 'event-logger: lgpioのビルドに必要なAPTがありません: %s\n' \
      "${missing[*]}" >&2
    return 1
  }
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    "${missing[@]}"
}
