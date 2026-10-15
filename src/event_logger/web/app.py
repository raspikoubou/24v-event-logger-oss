"""Flask app factoryと製品内部のログendpoint。"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    session,
)
from flask import (
    request as request,
)

from event_logger.adapters.clock import Clock, FakeClock, SystemClock
from event_logger.common.models import ChannelConfig, LogRecord
from event_logger.common.timeutil import format_timestamp
from event_logger.database.connection import connect_database, initialize_connection
from event_logger.domain.widgets import (
    WIDGET_PERIOD_DURATIONS_MS,
    WidgetType,
)
from event_logger.loggerd.ipc import (
    ControlCommand,
    ControlRequest,
    IpcProtocolError,
    IpcRemoteError,
    UnixControlClient,
)
from event_logger.version import VERSION
from event_logger.web.auth import SharedPasswordAuth, hash_password
from event_logger.web.config_payloads import _build_output_rule as _build_output_rule
from event_logger.web.config_payloads import _build_widget as _build_widget
from event_logger.web.config_payloads import (
    _counter_definition_changed as _counter_definition_changed,
)
from event_logger.web.config_payloads import _json_bool as _json_bool
from event_logger.web.config_payloads import _json_int as _json_int
from event_logger.web.config_payloads import _json_text as _json_text
from event_logger.web.config_payloads import (
    _output_rule_payload as _output_rule_payload,
)
from event_logger.web.config_payloads import _widget_payload as _widget_payload
from event_logger.web.control_service import ControlClient as ControlClient
from event_logger.web.control_service import (
    _clear_configuration_out_of_sync as _clear_configuration_out_of_sync,
)
from event_logger.web.control_service import (
    _configuration_fingerprint as _configuration_fingerprint,
)
from event_logger.web.control_service import _control_client as _control_client
from event_logger.web.control_service import _daemon_status as _daemon_status
from event_logger.web.control_service import _DemoControlClient as _DemoControlClient
from event_logger.web.control_service import (
    _mark_configuration_out_of_sync as _mark_configuration_out_of_sync,
)
from event_logger.web.control_service import (
    _outputs_confirmed_off as _outputs_confirmed_off,
)
from event_logger.web.control_service import (
    _reload_after_rollback as _reload_after_rollback,
)
from event_logger.web.control_service import (
    _request_config_reload as _request_config_reload,
)
from event_logger.web.control_service import _restore_channel as _restore_channel
from event_logger.web.control_service import _restore_heartbeat as _restore_heartbeat
from event_logger.web.control_service import (
    _stop_for_configuration_recovery as _stop_for_configuration_recovery,
)
from event_logger.web.csv_export import _output_name
from event_logger.web.dashboard_periods import (
    _dashboard_range_ms as _dashboard_range_ms,
)
from event_logger.web.demo_fixtures import _seed_demo_data as _seed_demo_data
from event_logger.web.log_filters import _format_local_input as _format_local_input
from event_logger.web.log_filters import _parse_log_filters as _parse_log_filters
from event_logger.web.log_filters import _query_int as _query_int
from event_logger.web.request_context import (
    _REQUEST_WEB_CONTEXT as _REQUEST_WEB_CONTEXT,
)
from event_logger.web.request_context import WebContext as WebContext
from event_logger.web.request_context import _create_web_context as _create_web_context
from event_logger.web.request_context import (
    _DatabaseRequestMiddleware as _DatabaseRequestMiddleware,
)
from event_logger.web.request_context import (
    _DatabaseScopedResponse as _DatabaseScopedResponse,
)
from event_logger.web.request_context import (
    _RequestScopedWebContext as _RequestScopedWebContext,
)
from event_logger.web.routes_access import (
    _register_auth_routes,
    _register_control_routes,
    _register_page_routes,
)
from event_logger.web.routes_data import (
    _register_dashboard_routes,
    _register_log_routes,
    _register_status_routes,
)
from event_logger.web.routes_settings import (
    _register_basic_settings_routes,
    _register_channel_routes,
    _register_setup_routes,
    _register_system_settings_routes,
)
from event_logger.web.routes_updates import _register_update_routes
from event_logger.web.routes_widgets import (
    _register_output_rule_routes,
    _register_widget_routes,
)
from event_logger.web.system_settings import _effective_timezone as _effective_timezone
from event_logger.web.system_settings import (
    _parse_setup_channels as _parse_setup_channels,
)
from event_logger.web.system_settings import (
    _safe_default_hostname as _safe_default_hostname,
)
from event_logger.web.system_settings import (
    _save_system_settings as _save_system_settings,
)
from event_logger.web.system_settings import (
    _system_settings_payload as _system_settings_payload,
)
from event_logger.web.system_settings import _validate_hostname as _validate_hostname
from event_logger.web.system_settings import (
    _validated_system_settings as _validated_system_settings,
)

# 既存ルートモジュールが遅延importする共通依存を明示的に再エクスポートする。
# 本体の挙動は変えず、mypy strictのno_implicit_reexportにも対応する。
__all__ = [
    "Any",
    "ControlCommand",
    "ControlRequest",
    "IpcProtocolError",
    "IpcRemoteError",
    "Mapping",
    "Response",
    "UnixControlClient",
    "WIDGET_PERIOD_DURATIONS_MS",
    "WidgetType",
    "ZoneInfo",
    "ZoneInfoNotFoundError",
    "cast",
    "create_app",
    "create_production_app",
    "format_timestamp",
    "hash_password",
    "render_template",
    "request",
    "secrets",
    "session",
    "sqlite3",
]

_LOGGER = logging.getLogger(__name__)
_DASHBOARD_TIMELINE_MAX_JSON_BYTES = 2 * 1024 * 1024


def create_app(config: Mapping[str, Any] | None = None) -> Flask:
    """Flask appを作成する。

    `EVENT_LOGGER_DEMO=1` 相当の `DEMO=True` を渡すと、外部DBやGPIOなしで
    固定fixtureを作成する。module import時にはDB・GPIO・threadを初期化しない。
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_mapping(
        DEMO=False,
        APP_VERSION=VERSION,
        DATABASE_PATH=":memory:",
        TIMEZONE="Asia/Tokyo",
        DEVICE_NAME="",
        HOSTNAME=None,
        LOGGER_STATUS="unknown",
        LOGGER_STATUS_PROVIDER=None,
        CONTROL_SOCKET_PATH=None,
        CONTROL_CLIENT=None,
        AUTH_ENABLED=False,
        PASSWORD_HASH=None,
        SECRET_KEY=None,
        CSRF_ENABLED=True,
        THREAD_SAFE_REQUESTS=False,
        UPDATE_REPOSITORY="",
        UPDATE_HELPER="/opt/event-logger/current/packaging/update.sh",
        UPDATE_WORK_DIR="/var/lib/event-logger/updates",
        UPDATE_SOCKET_PATH="",
        UPDATE_STATUS_FILE="/var/lib/event-logger/update-status.json",
    )
    if config is not None:
        app.config.update(config)
    app.secret_key = app.config["SECRET_KEY"] or secrets.token_urlsafe(32)
    auth = SharedPasswordAuth(
        enabled=bool(app.config["AUTH_ENABLED"]),
        password_hash=app.config["PASSWORD_HASH"],
    )
    app.extensions["event_logger.auth"] = auth
    _register_auth_routes(app, auth)

    demo = bool(app.config["DEMO"])
    clock = app.config.get("CLOCK") or _default_clock(demo)
    database_path = app.config["DATABASE_PATH"]
    database_path_text = str(database_path)
    thread_safe_requests = bool(app.config["THREAD_SAFE_REQUESTS"])
    if thread_safe_requests and database_path_text == ":memory:":
        raise ValueError("thread-safe requests require a file-backed SQLite database")
    connection = connect_database(database_path_text)
    try:
        return _finish_app_creation(
            app,
            connection=connection,
            database_path_text=database_path_text,
            clock=clock,
            demo=demo,
            thread_safe_requests=thread_safe_requests,
            auth=auth,
        )
    except BaseException:
        # 成功時はWebContextがapp寿命の接続を所有する。構築途中で失敗した
        # 場合だけ、返却先のないbase connectionをfactoryが閉じる。
        connection.close()
        raise


def _finish_app_creation(
    app: Flask,
    *,
    connection: sqlite3.Connection,
    database_path_text: str,
    clock: Clock,
    demo: bool,
    thread_safe_requests: bool,
    auth: SharedPasswordAuth,
) -> Flask:
    """DB接続後のapp構築を完了する。失敗時の接続解放はfactoryが担う。"""
    initialize_connection(connection)
    control_client = _control_client(app, demo)
    status_provider = _status_provider(app)
    timezone_name = str(app.config["TIMEZONE"])
    base_context = _create_web_context(
        connection=connection,
        database_path=database_path_text,
        clock=clock,
        demo=demo,
        status_provider=status_provider,
        timezone_name=timezone_name,
        control_client=control_client,
    )
    stored_protection = base_context.settings.get_json(
        "password_protection", auth.enabled
    )
    stored_hash = base_context.settings.get_json(
        "password_hash", app.config.get("PASSWORD_HASH")
    )
    if not isinstance(stored_protection, bool):
        raise ValueError("stored password_protection setting is invalid")
    if stored_hash is not None and not isinstance(stored_hash, str):
        raise ValueError("stored password hash is invalid")
    auth.configure(enabled=stored_protection, password_hash=stored_hash)
    context = base_context
    if thread_safe_requests:
        context = cast(WebContext, _RequestScopedWebContext(base_context))
        # Flask上のWSGI middleware設定なので、methodをinstance側でwrapする。
        app.wsgi_app = _DatabaseRequestMiddleware(  # type: ignore[method-assign]
            app.wsgi_app,
            database_path_text,
            lambda request_connection: _create_web_context(
                connection=request_connection,
                database_path=database_path_text,
                clock=clock,
                demo=demo,
                status_provider=status_provider,
                timezone_name=timezone_name,
                control_client=control_client,
            ),
        )
    app.extensions["event_logger.web_context"] = context
    if isinstance(control_client, _DemoControlClient):
        control_client.set_configuration_digest_provider(
            lambda: _configuration_fingerprint(context)
        )
    if demo:
        _seed_demo_data(context)

    _register_page_routes(app, context)

    _register_status_routes(app, context, auth)

    _register_update_routes(app, demo)

    _register_dashboard_routes(app, context)

    _register_control_routes(app, context)
    _register_channel_routes(app, context)
    _register_basic_settings_routes(app, context)
    _register_system_settings_routes(app, context, auth)
    _register_setup_routes(app, context, auth)
    _register_output_rule_routes(app, context)
    _register_widget_routes(app, context)
    _register_log_routes(app, context)

    return app


def create_production_app() -> Flask:
    """Gunicorn用に環境変数から本番Web設定を読み込む。"""
    demo = os.environ.get("EVENT_LOGGER_DEMO") == "1"
    database_path = os.environ.get(
        "EVENT_LOGGER_DB",
        ":memory:" if demo else "/var/lib/event-logger/logger.db",
    )
    return create_app(
        {
            "DEMO": demo,
            "DATABASE_PATH": database_path,
            # ファイルDBでは各gthread要求が別connectionを所有する。
            # in-memory demoは単一connectionのままにしてschemaを共有する。
            "THREAD_SAFE_REQUESTS": database_path != ":memory:",
            "CONTROL_SOCKET_PATH": (
                None
                if demo
                else os.environ.get(
                    "EVENT_LOGGER_SOCKET", "/run/event-logger/control.sock"
                )
            ),
            "TIMEZONE": os.environ.get("EVENT_LOGGER_TIMEZONE", "Asia/Tokyo"),
            "APP_VERSION": os.environ.get("EVENT_LOGGER_VERSION", VERSION),
            "AUTH_ENABLED": os.environ.get("EVENT_LOGGER_AUTH_ENABLED") == "1",
            "PASSWORD_HASH": os.environ.get("EVENT_LOGGER_PASSWORD_HASH"),
            "SECRET_KEY": os.environ.get("EVENT_LOGGER_SECRET_KEY"),
            "UPDATE_REPOSITORY": os.environ.get("EVENT_LOGGER_UPDATE_REPOSITORY", ""),
            "UPDATE_HELPER": os.environ.get(
                "EVENT_LOGGER_UPDATE_HELPER",
                "/opt/event-logger/current/packaging/update.sh",
            ),
            "UPDATE_WORK_DIR": os.environ.get(
                "EVENT_LOGGER_UPDATE_WORK_DIR",
                "/var/lib/event-logger/updates",
            ),
            "UPDATE_SOCKET_PATH": os.environ.get(
                "EVENT_LOGGER_UPDATE_SOCKET", "/run/event-logger-update/update.sock"
            ),
            "UPDATE_STATUS_FILE": os.environ.get(
                "EVENT_LOGGER_UPDATE_STATUS_FILE",
                "/var/lib/event-logger/update-status.json",
            ),
        }
    )


def _default_clock(demo: bool) -> Clock:
    """demo fixtureと本番で異なるClockを選ぶ。"""
    if demo:
        return FakeClock(utc_ms=1_700_000_000_000, monotonic_ns=0, synchronized=True)
    return SystemClock()


def _control_endpoint(
    context: WebContext,
    control_request: ControlRequest,
) -> Response:
    """制御要求をIPCへ渡し、UI向けの安全なHTTP envelopeへ変換する。"""
    if context.control_client is None:
        return _error(
            "LOGGERD_UNAVAILABLE",
            "loggerd control socket is not configured",
            503,
        )
    sync_state = context.settings.get_json("daemon_config_sync", {})
    if (
        control_request.command
        in {
            ControlCommand.START_LOGGING,
            ControlCommand.TEST_OUTPUT,
        }
        and isinstance(sync_state, dict)
        and sync_state.get("out_of_sync") is True
    ):
        return _error(
            "CONFIG_OUT_OF_SYNC",
            "現在の設定がloggerdへ反映済みと確認できるまで再開・出力テストできません",
            409,
        )
    if (
        control_request.command is ControlCommand.RELOAD_CONFIG
        and isinstance(sync_state, dict)
        and sync_state.get("out_of_sync") is True
    ):
        stopped = _stop_for_configuration_recovery(context)
        if not stopped:
            return _error(
                "LOGGERD_UNAVAILABLE",
                "loggerdの停止と出力OFFを確認できません。実機のOUT状態を確認してください",
                503,
            )
        _mark_configuration_out_of_sync(context, stopped=True)
    expected_digest = (
        _configuration_fingerprint(context)
        if control_request.command is ControlCommand.RELOAD_CONFIG
        else None
    )
    try:
        data = context.control_client.request(control_request)
    except IpcRemoteError as error:
        if control_request.command is ControlCommand.RELOAD_CONFIG:
            stopped = _stop_for_configuration_recovery(context)
            _mark_configuration_out_of_sync(context, stopped=stopped)
        return _error(error.code, error.message, 409)
    except TimeoutError as error:
        # timeout後もloggerdが要求を実行済みの場合がある。状態を再照会し、
        # 確認できた操作だけ成功として返す。再送するとsessionやOUTを重複操作する。
        try:
            status_request = ControlRequest(ControlCommand.STATUS)
            if isinstance(context.control_client, UnixControlClient):
                # 元の要求を処理中のdaemonでも、処理完了後のSTATUSを待てる。
                state = context.control_client.request(status_request, timeout=6.0)
            else:
                state = context.control_client.request(status_request)
        except (OSError, IpcProtocolError, IpcRemoteError):
            if control_request.command is ControlCommand.RELOAD_CONFIG:
                stopped = _stop_for_configuration_recovery(context)
                _mark_configuration_out_of_sync(context, stopped=stopped)
            return _error("LOGGERD_UNAVAILABLE", str(error), 503)
        command = control_request.command
        confirmed = (
            (
                command is ControlCommand.STOP_LOGGING
                and state.get("logger_status") == "maintenance"
            )
            or (
                command is ControlCommand.START_LOGGING
                and state.get("logger_status") == "logging"
            )
            or (
                command is ControlCommand.TEST_OUTPUT
                and state.get("output_test_channel") == control_request.channel
            )
            or (
                command is ControlCommand.END_OUTPUT_TEST
                and state.get("output_test_channel") is None
            )
            or (
                command is ControlCommand.RELOAD_CONFIG
                and state.get("configuration_digest") == expected_digest
            )
        )
        if confirmed:
            if command is ControlCommand.RELOAD_CONFIG:
                _clear_configuration_out_of_sync(context)
            return _ok(state)
        if command is ControlCommand.RELOAD_CONFIG:
            stopped = _stop_for_configuration_recovery(context)
            _mark_configuration_out_of_sync(context, stopped=stopped)
        return _error("LOGGERD_UNAVAILABLE", str(error), 503)
    except (OSError, IpcProtocolError) as error:
        if control_request.command is ControlCommand.RELOAD_CONFIG:
            stopped = _stop_for_configuration_recovery(context)
            _mark_configuration_out_of_sync(context, stopped=stopped)
        return _error("LOGGERD_UNAVAILABLE", str(error), 503)
    if (
        control_request.command is ControlCommand.RELOAD_CONFIG
        and data.get("configuration_digest") != expected_digest
    ):
        stopped = _stop_for_configuration_recovery(context)
        _mark_configuration_out_of_sync(context, stopped=stopped)
        return _error(
            "CONFIG_NOT_APPLIED",
            "loggerdが現在の設定を適用したことを確認できません",
            503,
        )
    if control_request.command is ControlCommand.RELOAD_CONFIG:
        _clear_configuration_out_of_sync(context)
    return _ok(data)


def _ensure_csrf_token() -> str:
    """sessionにCSRF tokenを一度だけ発行する。"""
    token = session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _status_provider(app: Flask) -> Callable[[], str]:
    """loggerd状態providerを設定値から構築する。"""
    configured = app.config.get("LOGGER_STATUS_PROVIDER")
    if callable(configured):
        return lambda: str(configured())
    return lambda: str(app.config.get("LOGGER_STATUS", "unknown"))


def _stored_dashboard_period(context: WebContext) -> str:
    """保存済みDashboard共通期間を検証して返す。"""
    value = context.settings.get_json("dashboard_time_range", "8h")
    supported_periods = {
        *WIDGET_PERIOD_DURATIONS_MS,
        "today",
        "yesterday",
        "this_week",
        "custom",
    }
    if isinstance(value, str) and value in supported_periods:
        return value
    return "8h"


def _stored_dashboard_custom_range(
    context: WebContext,
) -> dict[str, int] | None:
    """保存済みの任意期間を検証して返す。"""
    value = context.settings.get_json("dashboard_custom_range")
    if not isinstance(value, Mapping):
        return None
    start_ms = value.get("from_ms")
    end_ms = value.get("to_ms")
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or start_ms < 0
        or start_ms >= end_ms
    ):
        return None
    return {"from_ms": start_ms, "to_ms": end_ms}


def _channel_payload(channel: ChannelConfig) -> dict[str, Any]:
    """ChannelConfigをJSON payloadへ変換する。"""
    return {
        "id": channel.id,
        "enabled": channel.enabled,
        "name": channel.name,
        "debounce_ms": channel.debounce_ms,
        "on_label": channel.on_label,
        "off_label": channel.off_label,
    }


def _log_payload(
    event: LogRecord,
    channel_map: Mapping[int, ChannelConfig],
    rule_names: Mapping[int, str],
    timezone_name: str,
) -> dict[str, Any]:
    """統合ログレコードをUI用payloadへ変換する。"""
    if event.record_type == "input":
        signal = f"IN{event.channel_id}"
        name = channel_map[event.channel_id].name
    else:
        signal = f"OUT{event.channel_id}"
        name = _output_name(event, rule_names)
    return {
        "id": f"{event.record_type}:{event.id}",
        "timestamp_ms": event.ts_utc_ms,
        "timestamp": format_timestamp(event.ts_utc_ms, timezone_name),
        "signal": signal,
        "name": name,
        "value": "ON" if event.value else "OFF",
        "kind": event.kind,
        "source": event.source.value if event.source is not None else "",
        "clock_synced": event.clock_synced,
    }


def _read_update_status(path_value: object) -> dict[str, object] | None:
    """root updaterが出力した状態を表示用に読み取る。"""
    if not isinstance(path_value, str) or not path_value:
        return None
    try:
        with open(path_value, encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        key: value
        for key, value in payload.items()
        if key in {"status", "version", "message", "updated_at", "attempt_id"}
    }


def _ok(data: Mapping[str, Any]) -> Response:
    """成功レスポンスを統一形式で返す。"""
    return jsonify({"ok": True, "data": data})


def _error(code: str, message: str, status_code: int) -> Response:
    """内部exceptionを漏らさずエラーレスポンスを返す。"""
    response = jsonify({"ok": False, "error": {"code": code, "message": message}})
    response.status_code = status_code
    return response


def main() -> None:
    """開発・demo確認用にlocalhostだけでFlask appを起動する。"""
    app = create_production_app()
    app.run(
        host=os.environ.get("EVENT_LOGGER_WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("EVENT_LOGGER_WEB_PORT", "5000")),
        debug=False,
        # WebContextはapp生成時に作ったSQLite connectionをrepositoriesと共有
        # する。標準Flask serverのthreaded=Trueでは別threadから同じconnection
        # を触ってProgrammingErrorになるため、demo/開発起動は直列化する。
        threaded=False,
    )


if __name__ == "__main__":
    main()
