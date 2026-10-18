"""入力設定・本体設定・初回セットアップに関するHTTPルート。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask

from event_logger.maintenance.main import RETENTION_CHOICES
from event_logger.web.auth import SharedPasswordAuth

if TYPE_CHECKING:
    from event_logger.web.app import WebContext


def _register_channel_routes(app: Flask, context: WebContext) -> None:
    """入力チャンネル設定のHTTP routeをappへ登録する。

        Args:
            app: Flask application。
            context: requestごとのRepository、Clock、設定値を保持するWebContext。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    from event_logger.web.app import (
        IpcProtocolError,
        IpcRemoteError,
        Response,
        _channel_payload,
        _error,
        _json_bool,
        _json_int,
        _json_text,
        _mark_configuration_out_of_sync,
        _ok,
        _reload_after_rollback,
        _request_config_reload,
        _restore_channel,
        request,
    )


    @app.post("/api/channels/<int:channel_id>")
    def update_channel(channel_id: int) -> Response:
        """入力設定を保存し、loggerd reload失敗時は旧値へ戻す。

            Args:
                channel_id: 対象チャンネルの製品上の番号。

            Returns:
                保存後のchannel設定payloadをJSON 200で返す。channel不存在は404、
                    body/field不正は400、
                    loggerd拒否は409、IPC利用不可は503。reload失敗時は変更前設定へ戻す。
        """
        before = context.channels.get(channel_id)
        if before is None:
            return _error("NOT_FOUND", "channel was not found", 404)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        try:
            updated = context.channels.update(
                channel_id=channel_id,
                enabled=_json_bool(payload.get("enabled", before.enabled), "enabled"),
                name=_json_text(payload.get("name", before.name), "name"),
                debounce_ms=_json_int(
                    payload.get("debounce_ms", before.debounce_ms), "debounce_ms"
                ),
                on_label=_json_text(
                    payload.get("on_label", before.on_label), "on_label"
                ),
                off_label=_json_text(
                    payload.get("off_label", before.off_label), "off_label"
                ),
                updated_at_ms=context.clock.now_utc_ms(),
            )
        except (TypeError, ValueError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        if context.control_client is None:
            _restore_channel(context, before)
            _mark_configuration_out_of_sync(context, stopped=False)
            return _error(
                "LOGGERD_UNAVAILABLE",
                "loggerd reload was not acknowledged",
                503,
            )
        try:
            _request_config_reload(context)
        except IpcRemoteError as error:
            _restore_channel(context, before)
            _reload_after_rollback(context)
            return _error(error.code, error.message, 409)
        except (OSError, IpcProtocolError) as error:
            _restore_channel(context, before)
            _reload_after_rollback(context)
            return _error("LOGGERD_UNAVAILABLE", str(error), 503)
        return _ok(_channel_payload(updated))


def _register_basic_settings_routes(app: Flask, context: WebContext) -> None:
    """Heartbeat・保存期間・Dashboard期間のrouteをappへ登録する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    from event_logger.web.app import (
        WIDGET_PERIOD_DURATIONS_MS,
        IpcProtocolError,
        IpcRemoteError,
        Response,
        _error,
        _json_bool,
        _json_int,
        _mark_configuration_out_of_sync,
        _ok,
        _reload_after_rollback,
        _request_config_reload,
        _restore_heartbeat,
        _stored_dashboard_custom_range,
        request,
    )


    @app.post("/api/settings/heartbeat")
    def update_heartbeat() -> Response:
        """Heartbeat設定を保存し、loggerd reload ACKを確認する。

            Args:
                なし。

            Returns:
                保存したHeartbeat設定をJSON 200で返す。JSONや設定値不正は400、
                    保存済み値破損は500、loggerd拒否は409、
                    IPC利用不可は503。reload失敗時は旧設定へ戻す。
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        old = context.settings.get_json(
            "heartbeat",
            {"enabled": False, "channel_id": None, "interval_ms": 5_000},
        )
        if not isinstance(old, dict):
            return _error("CONFIG_INVALID", "stored heartbeat setting is invalid", 500)
        try:
            enabled = _json_bool(
                payload.get("enabled", old.get("enabled", False)),
                "enabled",
            )
            channel_id = payload.get("channel_id", old.get("channel_id"))
            if enabled:
                channel_id = _json_int(channel_id, "channel_id")
                if not 1 <= channel_id <= 6:
                    raise ValueError("channel_id must be between 1 and 6")
            elif channel_id is not None:
                channel_id = _json_int(channel_id, "channel_id")
            interval_ms = _json_int(
                payload.get("interval_ms", old.get("interval_ms", 5_000)),
                "interval_ms",
            )
            if not 1_000 <= interval_ms <= 60_000:
                raise ValueError("interval_ms must be between 1000 and 60000")
            if enabled and any(
                rule.condition.output_channel == channel_id
                for rule in context.output_rules.list_all()
            ):
                raise ValueError("channel is already assigned to a condition rule")
            new = {
                "enabled": enabled,
                "channel_id": channel_id,
                "interval_ms": interval_ms,
            }
        except (TypeError, ValueError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        context.settings.set_json(
            key="heartbeat",
            value=new,
            updated_at_ms=context.clock.now_utc_ms(),
        )
        if context.control_client is None:
            _restore_heartbeat(context, old)
            _mark_configuration_out_of_sync(context, stopped=False)
            return _error(
                "LOGGERD_UNAVAILABLE",
                "loggerd reload was not acknowledged",
                503,
            )
        try:
            _request_config_reload(context)
        except IpcRemoteError as error:
            _restore_heartbeat(context, old)
            _reload_after_rollback(context)
            return _error(error.code, error.message, 409)
        except (OSError, IpcProtocolError) as error:
            _restore_heartbeat(context, old)
            _reload_after_rollback(context)
            return _error("LOGGERD_UNAVAILABLE", str(error), 503)
        return _ok(new)

    @app.post("/api/settings/retention")
    def update_retention() -> Response:
        """保存期間を仕様で定義した選択肢へ限定して保存する。

            Args:
                なし。

            Returns:
                保存日数を {retention_days: number} としてJSON 200で返す。
                    JSON不正または許可された保持日数以外はINVALID_REQUEST/400。
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        try:
            raw_retention = payload.get("retention_days")
            retention_days = (
                None
                if raw_retention is None
                else _json_int(raw_retention, "retention_days")
            )
            if retention_days is not None and retention_days not in RETENTION_CHOICES:
                raise ValueError(f"retention_days must be one of {RETENTION_CHOICES}")
        except (TypeError, ValueError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        context.settings.set_json(
            key="retention_days",
            value=retention_days,
            updated_at_ms=context.clock.now_utc_ms(),
        )
        return _ok({"retention_days": retention_days})

    @app.post("/api/settings/dashboard")
    def update_dashboard_settings() -> Response:
        """Dashboard共通期間を保存する。

            Args:
                なし。

            Returns:
                保存した共通periodとcustom_rangeを含むJSON 200を返す。
                    JSONやperiod/range不正はINVALID_REQUEST/400。
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        period = payload.get("period")
        supported_periods = {
            *WIDGET_PERIOD_DURATIONS_MS,
            "today",
            "yesterday",
            "this_week",
            "custom",
        }
        if not isinstance(period, str) or period not in supported_periods:
            return _error(
                "INVALID_REQUEST",
                "period is not a supported dashboard period",
                400,
            )
        custom_range = _stored_dashboard_custom_range(context)
        if period == "custom":
            try:
                start_ms = _json_int(payload.get("from_ms"), "from_ms")
                end_ms = _json_int(payload.get("to_ms"), "to_ms")
                now_ms = context.clock.now_utc_ms()
                if start_ms < 0 or start_ms >= end_ms or start_ms >= now_ms:
                    raise ValueError(
                        "custom range must satisfy 0 <= from_ms < to_ms "
                        "and start before now"
                    )
                custom_range = {"from_ms": start_ms, "to_ms": end_ms}
            except (TypeError, ValueError) as error:
                return _error("INVALID_REQUEST", str(error), 400)
        context.settings.set_many_json(
            values={
                "dashboard_time_range": period,
                "dashboard_custom_range": custom_range,
            },
            updated_at_ms=context.clock.now_utc_ms(),
            audit_action="dashboard_settings_updated",
        )
        return _ok({"period": period, "custom_range": custom_range})


def _register_system_settings_routes(
    app: Flask,
    context: WebContext,
    auth: SharedPasswordAuth,
) -> None:
    """設備名・hostname・timezone・共有認証のrouteをappへ登録する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            auth: 共有パスワード認証の状態を管理するオブジェクト。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    from event_logger.web.app import (
        Response,
        ZoneInfo,
        ZoneInfoNotFoundError,
        _error,
        _json_bool,
        _json_text,
        _ok,
        _system_settings_payload,
        _validate_hostname,
        hash_password,
        request,
    )


    @app.get("/api/settings/system")
    def system_settings() -> Response:
        """本体・データ設定を返す（パスワードhashは返さない）。

            Args:
                なし。

            Returns:
                パスワードhashを除いた本体・データ設定payloadをJSON 200で返す。
        """
        return _ok(_system_settings_payload(context, app, auth))

    @app.post("/api/settings/system")
    def update_system_settings() -> Response:
        """設備名・hostname・timezone・共有認証を保存する。

            Args:
                なし。

            Returns:
                保存後のhashなし設定payloadをJSON 200で返す。JSON、hostname、timezone、
                    認証fieldが不正ならINVALID_REQUEST/400。
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        current = _system_settings_payload(context, app, auth)
        try:
            device_name = _json_text(
                payload.get("device_name", current["device_name"]),
                "device_name",
            ).strip()
            if not device_name or len(device_name) > 200:
                raise ValueError("device_name must be 1..200 characters")
            hostname = (
                _json_text(payload.get("hostname", current["hostname"]), "hostname")
                .strip()
                .lower()
            )
            _validate_hostname(hostname)
            timezone_name = _json_text(
                payload.get("timezone", current["timezone"]), "timezone"
            )
            ZoneInfo(timezone_name)
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
        except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        timestamp_ms = context.clock.now_utc_ms()
        context.settings.set_many_json(
            values={
                "device_name": device_name,
                "hostname": hostname,
                "timezone": timezone_name,
                "password_protection": password_enabled,
                "password_hash": password_hash,
            },
            updated_at_ms=timestamp_ms,
            audit_action="system_settings_updated",
        )
        auth.configure(enabled=password_enabled, password_hash=password_hash)
        return _ok(_system_settings_payload(context, app, auth))


def _register_setup_routes(
    app: Flask,
    context: WebContext,
    auth: SharedPasswordAuth,
) -> None:
    """初回設定の状態取得・一括反映routeをappへ登録する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            auth: 共有パスワード認証の状態を管理するオブジェクト。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    from event_logger.web.app import (
        ControlCommand,
        ControlRequest,
        IpcProtocolError,
        IpcRemoteError,
        Response,
        ZoneInfoNotFoundError,
        _channel_payload,
        _daemon_status,
        _error,
        _json_int,
        _ok,
        _parse_setup_channels,
        _reload_after_rollback,
        _request_config_reload,
        _safe_default_hostname,
        _save_system_settings,
        _system_settings_payload,
        _validated_system_settings,
        cast,
        request,
    )


    @app.get("/api/setup")
    def setup_status() -> Response:
        """初回設定の完了状態と入力設定を返す。

            Args:
                なし。

            Returns:
                初回設定の完了状態、channel設定、Heartbeat等を含むJSON 200を返す。
        """
        channels = context.channels.list_all()
        completed = context.settings.get_json("setup_completed", False)
        return _ok(
            {
                "completed": bool(completed),
                "system": _system_settings_payload(context, app, auth),
                "retention_days": context.settings.get_json("retention_days", 365),
                "channels": [_channel_payload(channel) for channel in channels],
            }
        )

    @app.post("/api/setup")
    def complete_setup() -> Response:
        """初回設定を一括保存し、監視再開時のsnapshotを作成する。

            Args:
                なし。

            Returns:
                初回設定を保存した結果と監視再開用snapshot情報をJSON 200で返す。
                    未完了の必須入力や値の形式不正は400、
                    loggerdへ反映できない場合はその制御errorを返す。
        """
        if context.settings.get_json("setup_completed", False):
            return _error("SETUP_COMPLETED", "initial setup is already completed", 409)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        try:
            channel_values = _parse_setup_channels(context, payload.get("channels"))
            raw_retention = payload.get("retention_days", 365)
            retention_days = (
                None
                if raw_retention is None
                else _json_int(raw_retention, "retention_days")
            )
            if retention_days is not None and retention_days not in RETENTION_CHOICES:
                raise ValueError(f"retention_days must be one of {RETENTION_CHOICES}")
            system_payload = dict(payload)
            system_payload.setdefault("password_protection", False)
            system = _validated_system_settings(context, app, auth, system_payload)
        except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        if context.control_client is None:
            return _error("LOGGERD_UNAVAILABLE", "loggerd is required for setup", 503)
        daemon_status, daemon_error, _ = _daemon_status(context)
        if daemon_status not in {"logging", "maintenance"}:
            return _error("LOGGERD_UNAVAILABLE", daemon_error or daemon_status, 503)
        stopped_here = daemon_status == "logging"
        start_after_setup = daemon_status in {"logging", "maintenance"}
        old_settings = {
            "device_name": context.settings.get_json("device_name", ""),
            "hostname": context.settings.get_json("hostname", _safe_default_hostname()),
            "timezone": context.settings.get_json("timezone", context.timezone_name),
            "password_protection": context.settings.get_json(
                "password_protection", auth.enabled
            ),
            "password_hash": context.settings.get_json("password_hash", None),
            "retention_days": context.settings.get_json("retention_days", 365),
            "setup_completed": context.settings.get_json("setup_completed", False),
        }
        try:
            if stopped_here:
                context.control_client.request(
                    ControlRequest(ControlCommand.STOP_LOGGING)
                )
            old_channels = {
                channel.id: channel for channel in context.channels.list_all()
            }
            timestamp_ms = context.clock.now_utc_ms()
            context.channels.update_many(
                channel_values,
                updated_at_ms=timestamp_ms,
                audit_action="initial_setup_channel_updated",
            )
            _save_system_settings(context, system, retention_days)
            # daemonは初回設定完了を確認してからSTART_LOGGINGを受け付ける。
            # start後にこのflagを書くと、再起動・ACK遅延時にsnapshotのない
            # 不整合が残るため、反映前に同じDBへcommitする。
            context.settings.set_many_json(
                values={"setup_completed": True},
                updated_at_ms=context.clock.now_utc_ms(),
                audit_action="initial_setup_started",
            )
            _request_config_reload(context)
            if start_after_setup:
                context.control_client.request(
                    ControlRequest(ControlCommand.START_LOGGING)
                )
            auth.configure(
                enabled=bool(system["password_protection"]),
                password_hash=cast(str | None, system["password_hash"]),
            )
        except (
            OSError,
            IpcProtocolError,
            IpcRemoteError,
            RuntimeError,
            ValueError,
        ) as error:
            old_channel_values = [
                {
                    "channel_id": channel.id,
                    "enabled": channel.enabled,
                    "name": channel.name,
                    "debounce_ms": channel.debounce_ms,
                    "on_label": channel.on_label,
                    "off_label": channel.off_label,
                }
                for channel in locals().get("old_channels", {}).values()
            ]
            if old_channel_values:
                context.channels.update_many(
                    old_channel_values,
                    updated_at_ms=context.clock.now_utc_ms(),
                    audit_action="initial_setup_rollback",
                )
            context.settings.set_many_json(
                values=old_settings,
                updated_at_ms=context.clock.now_utc_ms(),
                audit_action="initial_setup_rollback",
            )
            auth.configure(
                enabled=bool(old_settings["password_protection"]),
                password_hash=(
                    old_settings["password_hash"]
                    if isinstance(old_settings["password_hash"], str)
                    else None
                ),
            )
            _reload_after_rollback(context)
            return _error("SETUP_FAILED", str(error), 503)
        return _ok({"completed": True})


