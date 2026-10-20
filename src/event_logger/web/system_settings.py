"""本体設定の検証・保存と初回セットアップ入力の正規化。

WebContextのRepositoryを受け取り、Flaskルートの登録処理から独立して扱う。
"""

from __future__ import annotations

import socket
from collections.abc import Mapping
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask

from event_logger.common.hostname import default_hostname, validate_hostname
from event_logger.web.auth import SharedPasswordAuth, hash_password
from event_logger.web.config_payloads import _json_bool, _json_int, _json_text

if TYPE_CHECKING:
    from event_logger.web.request_context import WebContext


def _system_settings_payload(
    context: WebContext,
    app: Flask,
    auth: SharedPasswordAuth,
) -> dict[str, object]:
    """本体・データ設定をDBと起動時defaultから組み立てる。

        Args:
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            auth: 共有パスワード認証の状態を管理するオブジェクト。

        Returns:
            password hashを除いたdevice name、hostname、timezone、auth enabled、
                retention等の表示用設定dict。
    """
    configured_hostname = app.config.get("HOSTNAME")
    default_hostname = (
        str(configured_hostname)
        if isinstance(configured_hostname, str) and configured_hostname
        else _safe_default_hostname()
    )
    raw_device_name = context.settings.get_json(
        "device_name", app.config.get("DEVICE_NAME", "")
    )
    raw_hostname = context.settings.get_json("hostname", default_hostname)
    raw_timezone = context.settings.get_json("timezone", context.timezone_name)
    raw_protection = context.settings.get_json("password_protection", auth.enabled)
    desired_hostname = (
        str(raw_hostname) if isinstance(raw_hostname, str) else default_hostname
    )
    # hostnamectlは次回起動時に反映するため、実機の現在値と比較して初めて
    # reboot案内を出す。常にtrueでは、設定済みの装置でも保守作業を誤誘導する。
    current_hostname = app.config.get("CURRENT_HOSTNAME", socket.gethostname())
    return {
        "device_name": (
            str(raw_device_name) if isinstance(raw_device_name, str) else ""
        ),
        "hostname": desired_hostname,
        "timezone": (
            str(raw_timezone)
            if isinstance(raw_timezone, str)
            else context.timezone_name
        ),
        "password_protection": bool(raw_protection),
        "hostname_restart_required": desired_hostname != current_hostname,
    }


def _validated_system_settings(
    context: WebContext,
    app: Flask,
    auth: SharedPasswordAuth,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """system設定payloadを検証し、保存用password hashを含めて返す。

        Args:
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            auth: 共有パスワード認証の状態を管理するオブジェクト。
            payload: HTTP JSON bodyまたは保存対象の設定値。
                呼出元で形式を検証している場合はその検証済み値。

        Returns:
            型・文字数・hostname・timezoneを検証したsystem設定dict。
                新passwordがある場合はhashだけを含み、平文は返さない。
    """
    # 既存値を初期値に使うpartial updateでも、最終結果は完全な設定mappingとして
    # 検証する。hostname規則とtimezone databaseを確認してから保存へ渡す。
    current = _system_settings_payload(context, app, auth)
    device_name = _json_text(
        payload.get("device_name", current["device_name"]), "device_name"
    ).strip()
    if not device_name or len(device_name) > 200:
        raise ValueError("device_name must be 1..200 characters")
    hostname = (
        _json_text(payload.get("hostname", current["hostname"]), "hostname")
        .strip()
        .lower()
    )
    _validate_hostname(hostname)
    timezone_name = _json_text(payload.get("timezone", current["timezone"]), "timezone")
    ZoneInfo(timezone_name)
    # 認証fieldは他の値の検証後に処理し、保存済みhashを維持するか新passwordを
    # hash化するかを決める。平文は戻り値にもRepositoryにも残さない。
    password_enabled = _json_bool(
        payload.get("password_protection", current["password_protection"]),
        "password_protection",
    )
    password_value = payload.get("password")
    old_hash = context.settings.get_json("password_hash")
    if old_hash is not None and not isinstance(old_hash, str):
        raise ValueError("stored password hash is invalid")
    if password_enabled:
        if password_value is None:
            password_hash = old_hash
        else:
            password = _json_text(password_value, "password")
            if not password:
                raise ValueError("password must not be empty")
            password_hash = hash_password(password)
        if not password_hash:
            raise ValueError("password is required when protection is enabled")
    else:
        password_hash = old_hash
    return {
        "device_name": device_name,
        "hostname": hostname,
        "timezone": timezone_name,
        "password_protection": password_enabled,
        "password_hash": password_hash,
    }


def _save_system_settings(
    context: WebContext,
    system: Mapping[str, object],
    retention_days: int | None = None,
) -> None:
    """検証済みsystem設定を一貫したaudit付きで保存する。

Args:
    context: 現在のWebContext。関数が使うRepository、Clock、
        設定値や制御clientを所有する。
    system: 検証済みsystem設定値。
    retention_days: event保持期間を表す日数。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    timestamp_ms = context.clock.now_utc_ms()
    values: dict[str, object] = {
        "device_name": system["device_name"],
        "hostname": system["hostname"],
        "timezone": system["timezone"],
        "password_protection": system["password_protection"],
        "password_hash": system["password_hash"],
    }
    # Noneは「保存期間なし」という明示設定なので、未指定時にもDBへ書く。
    values["retention_days"] = retention_days
    context.settings.set_many_json(
        values=values,
        updated_at_ms=timestamp_ms,
        audit_action="system_settings_updated",
    )


def _parse_setup_channels(
    context: WebContext,
    raw_channels: object,
) -> list[dict[str, object]]:
    """初回設定の入力6点を検証し、保存引数へ変換する。

        Args:
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            raw_channels: 初回設定用のchannel入力列。

        Returns:
            6 channel分の検証済み保存値dict列。
                件数・名前・debounce・enabled型が仕様外ならValueError。
    """
    if not isinstance(raw_channels, list):
        raise ValueError("channels must be an array")
    existing = {channel.id: channel for channel in context.channels.list_all()}
    provided: dict[int, Mapping[str, object]] = {}
    for raw_channel in raw_channels:
        if not isinstance(raw_channel, Mapping):
            raise ValueError("each channel must be an object")
        channel_id = _json_int(raw_channel.get("id"), "channel.id")
        if not 1 <= channel_id <= 6 or channel_id in provided:
            raise ValueError("channel.id must be unique and between 1 and 6")
        provided[channel_id] = raw_channel
    result: list[dict[str, object]] = []
    timestamp_ms = context.clock.now_utc_ms()
    for channel_id in range(1, 7):
        current = existing[channel_id]
        raw = provided.get(channel_id, {})
        enabled = _json_bool(raw.get("enabled", False), "channel.enabled")
        name = _json_text(raw.get("name", current.name), "channel.name").strip()
        debounce_ms = _json_int(
            raw.get("debounce_ms", current.debounce_ms), "channel.debounce_ms"
        )
        if not name or len(name) > 200:
            raise ValueError("channel.name must be 1..200 characters")
        if not 0 <= debounce_ms <= 5000:
            raise ValueError("channel.debounce_ms must be between 0 and 5000")
        result.append(
            {
                "channel_id": channel_id,
                "enabled": enabled,
                "name": name,
                "debounce_ms": debounce_ms,
                "on_label": current.on_label,
                "off_label": current.off_label,
                "updated_at_ms": timestamp_ms,
            }
        )
    return result


def _effective_timezone(context: WebContext) -> str:
    """DB保存済みtimezoneを検証して返す。

        Args:
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。

        Returns:
            保存値が有効ならそのIANA timezone名、未保存・不正ならAsia/Tokyo。
    """
    raw_timezone = context.settings.get_json("timezone", context.timezone_name)
    if not isinstance(raw_timezone, str):
        return context.timezone_name
    try:
        ZoneInfo(raw_timezone)
    except ZoneInfoNotFoundError:
        return context.timezone_name
    return raw_timezone


def _validate_hostname(hostname: str) -> None:
    """OS hostnameへ渡せる単一ラベルを検証する。

Args:
    hostname: OSへ設定する候補hostname。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    validate_hostname(hostname)


def _safe_default_hostname() -> str:
    """設定未保存時の端末固有hostnameを返す。

        Args:
            なし。

        Returns:
            OSから取得したhostnameが安全な単一labelならそれ、
                そうでなければ event-logger。
    """
    return default_hostname()


