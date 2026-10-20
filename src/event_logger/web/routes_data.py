"""履歴・CSV・診断・Dashboard表示のHTTPルート。"""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, request, stream_with_context

from event_logger.common.timeutil import format_timestamp
from event_logger.loggerd.ipc import UnixControlClient
from event_logger.web.auth import SharedPasswordAuth
from event_logger.web.control_service import (
    _clear_configuration_out_of_sync,
    _configuration_fingerprint,
    _daemon_status,
    _mark_configuration_out_of_sync,
)
from event_logger.web.csv_export import generate_csv
from event_logger.web.dashboard_periods import (
    _stored_dashboard_custom_range,
    _stored_dashboard_period,
)
from event_logger.web.dashboard_queries import (
    _dashboard_timeline_limit_warning,
)
from event_logger.web.dashboard_service import (
    DashboardReadDependencies,
    load_dashboard_values,
)
from event_logger.web.http_payloads import _channel_payload, _log_payload
from event_logger.web.http_responses import _error, _ok
from event_logger.web.log_filters import (
    _format_local_input,
    _parse_log_filters,
    _query_int,
)
from event_logger.web.request_context import WebContext
from event_logger.web.system_settings import (
    _effective_timezone,
    _system_settings_payload,
)

_DASHBOARD_TIMELINE_MAX_JSON_BYTES = 2 * 1024 * 1024

def _register_log_routes(app: Flask, context: WebContext) -> None:
    """検索ログとCSV出力のrouteをappへ登録する。

        Args:
            app: Flask application。
            context: requestごとのRepository、Clock、設定値を保持するWebContext。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    @app.get("/api/logs")
    def logs() -> Response:
        """期間・チャンネルで絞った入力eventをJSONで返す。

            Args:
                なし。

            Returns:
                成功時はitemsと半開期間境界、limit/offset/has_next、
                    timezoneローカル日時を含むJSON 200を返す。
                    include_total_count=1の場合のみ一致総数を追加する。
                    期間・filter検証失敗はINVALID_FILTER/400。
        """
        try:
            timezone_name = _effective_timezone(context)
            filters = _parse_log_filters(context.clock, timezone_name)
            # next-pageの有無だけ判定できるよう要求limitより1件多く読み、応答には
            # 利用者が指定した件数まで戻す。
            page_filters = dict(filters)
            page_filters["limit"] = filters["limit"] + 1
            events = context.logs.list_between(**page_filters)
        except ValueError as error:
            return _error("INVALID_FILTER", str(error), 400)
        has_next = len(events) > filters["limit"]
        events = events[: filters["limit"]]
        channel_map = {channel.id: channel for channel in context.channels.list_all()}
        rule_names = {
            rule.condition.output_channel: rule.name
            for rule in context.output_rules.list_all()
        }
        # DB行から表示payloadを作り、境界値は検索に使った同じ半開区間で返す。
        # 総件数は高コストなので明示queryがある場合だけ別COUNTを実行する。
        payload = {
            "items": [
                _log_payload(event, channel_map, rule_names, timezone_name)
                for event in events
            ],
            "from_ms": filters["start_ms"],
            "to_ms": filters["end_ms"],
            "limit": filters["limit"],
            "offset": filters["offset"],
            "has_next": has_next,
            "from_local": _format_local_input(filters["start_ms"], timezone_name),
            "to_local": _format_local_input(filters["end_ms"], timezone_name),
        }
        if request.args.get("include_total_count") == "1":
            payload["total_count"] = context.logs.count_between(
                start_ms=filters["start_ms"],
                end_ms=filters["end_ms"],
                signals=filters["signals"],
                values=filters["values"],
                kinds=filters["kinds"],
                sources=filters["sources"],
            )
        return _ok(payload)

    @app.get("/api/logs.csv")
    def logs_csv() -> Response:
        """現在の検索条件に一致するIN/OUTログをUTF-8 BOM付きCSVで返す。

            Args:
                なし。

            Returns:
                一致する行をCSVとして逐次返し、Content-Dispositionをevents.csvにする。
                filter検証失敗はINVALID_FILTER/400で、streamを開始する前に拒否する。
        """
        try:
            timezone_name = _effective_timezone(context)
            filters = _parse_log_filters(context.clock, timezone_name)
            events = context.logs.iter_between(
                start_ms=filters["start_ms"],
                end_ms=filters["end_ms"],
                signals=filters["signals"],
                values=filters["values"],
                kinds=filters["kinds"],
                sources=filters["sources"],
            )
        except ValueError as error:
            return _error("INVALID_FILTER", str(error), 400)
        channel_map = {channel.id: channel for channel in context.channels.list_all()}
        rule_names = {
            rule.condition.output_channel: rule.name
            for rule in context.output_rules.list_all()
        }

        return Response(
            stream_with_context(
                generate_csv(events, channel_map, rule_names, timezone_name)
            ),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=events.csv",
            },
        )


def _register_status_routes(
    app: Flask,
    context: WebContext,
    auth: SharedPasswordAuth,
) -> None:
    """診断状態を返すrouteをappへ登録する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            auth: 共有パスワード認証の状態を管理するオブジェクト。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    @app.get("/api/status")
    def status() -> Response:
        """診断画面用の現在状態を返す。

            Args:
                なし。

            Returns:
                logger status、version、clock、記録件数、channel、IN/OUT、Heartbeat、
                    retention、
                    Dashboard期間、system、storage情報を含むJSON 200を返す。
                    storage読取失敗はcritical状態とstorage_errorをpayloadへ含める。
        """
        channels = context.channels.list_all()
        # statusとfingerprintを先に照合し、設定の反映済み判定を計数値やpayloadへ
        # 反映する。古いdaemonのdigest欠落も同期済みと誤判定しない。
        daemon_status, daemon_error, daemon_data = _daemon_status(context)
        event_count = context.events.count_all()
        output_event_count = context.output_events.count_all()
        unsynchronized_event_count = context.events.count_unsynchronized()
        unsynchronized_output_event_count = context.output_events.count_unsynchronized()
        configuration_sync = context.settings.get_json(
            "daemon_config_sync",
            {"out_of_sync": False},
        )
        reported_digest = daemon_data.get("configuration_digest")
        try:
            expected_digest = _configuration_fingerprint(context)
        except (TypeError, ValueError):
            expected_digest = None
        if isinstance(reported_digest, str):
            if expected_digest is not None and reported_digest == expected_digest:
                _clear_configuration_out_of_sync(context)
                configuration_sync = {"out_of_sync": False, "stopped": False}
            else:
                stop_was_confirmed = (
                    isinstance(configuration_sync, dict)
                    and configuration_sync.get("stopped") is True
                    and daemon_status == "maintenance"
                )
                _mark_configuration_out_of_sync(
                    context,
                    stopped=stop_was_confirmed,
                )
                configuration_sync = context.settings.get_json(
                    "daemon_config_sync", {"out_of_sync": True}
                )
        elif isinstance(context.control_client, UnixControlClient) and daemon_data:
            # 古いdaemonがSTATUSにfingerprintを返さない場合も、反映済みと
            # 誤認せず、更新後のdaemonへ切り替えるまで再開操作を抑止する。
            _mark_configuration_out_of_sync(context, stopped=False)
            configuration_sync = context.settings.get_json(
                "daemon_config_sync", {"out_of_sync": True}
            )
        # failure-proneなstorage inspectは他の状態構築後に独立して実行する。
        # filesystem確認だけ失敗しても診断API全体を500にせずcritical情報を返す。
        payload: dict[str, Any] = {
            "logger_status": daemon_status,
            "app_version": str(
                daemon_data.get("app_version", app.config["APP_VERSION"])
            ),
            "clock": {
                "utc_ms": context.clock.now_utc_ms(),
                "synchronized": context.clock.is_synchronized(),
            },
            "event_count": event_count,
            "output_event_count": output_event_count,
            "log_count": event_count + output_event_count,
            "unsynchronized_event_count": (
                unsynchronized_event_count + unsynchronized_output_event_count
            ),
            "channels": [_channel_payload(channel) for channel in channels],
            "demo": context.demo,
            "inputs": daemon_data.get("inputs", {}),
            "outputs": daemon_data.get("outputs", {}),
            "daemon": dict(daemon_data),
            "configuration_sync": configuration_sync,
        }
        heartbeat = context.settings.get_json(
            "heartbeat",
            {"enabled": False, "channel_id": None, "interval_ms": 5_000},
        )
        payload["heartbeat"] = heartbeat
        retention_days = context.settings.get_json("retention_days", 365)
        payload["retention_days"] = retention_days
        payload["dashboard_period"] = _stored_dashboard_period(context.settings)
        payload["dashboard_custom_range"] = _stored_dashboard_custom_range(
            context.settings
        )
        payload["system"] = _system_settings_payload(context, app, auth)
        try:
            storage = context.storage_monitor.inspect(context.database_path)
            payload["storage"] = {
                "total_bytes": storage.total_bytes,
                "used_bytes": storage.used_bytes,
                "free_bytes": storage.free_bytes,
                "free_ratio": storage.free_ratio,
                "db_bytes": storage.db_bytes,
                "status": storage.status.value,
            }
        except OSError as error:
            payload["storage"] = {
                "total_bytes": 0,
                "used_bytes": 0,
                "free_bytes": 0,
                "free_ratio": 0.0,
                "db_bytes": 0,
                "status": "critical",
            }
            payload["storage_error"] = str(error)
        if daemon_error is not None:
            payload["daemon_error"] = daemon_error
        return _ok(payload)


def _register_dashboard_routes(app: Flask, context: WebContext) -> None:
    """Dashboard計算値を返すrouteをappへ登録する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    @app.get("/api/dashboard")
    def dashboard() -> Response:
        """保存済みwidgetをDomain計算値付きで返す。

            Args:
                なし。

            Returns:
                成功時は計算済みwidget値、
                    event/timeline等のDashboard payloadをJSON 200で返す。
                    期間値が不正・過去範囲でない場合はINVALID_FILTER/400。
                        timeline件数が上限を超える場合は全計算をせず、
                    期間を絞る案内payloadをJSON 200で返す。
        """
        timezone_name = _effective_timezone(context)
        period = request.args.get("period")
        try:
            result = load_dashboard_values(
                DashboardReadDependencies(
                    clock=context.clock,
                    settings=context.settings,
                    widgets=context.widgets,
                    channels=context.channels,
                    events=context.events,
                    counter_resets=context.counter_resets,
                    output_events=context.output_events,
                    sessions=context.sessions,
                    status_provider=context.status_provider,
                    control_client=context.control_client,
                ),
                timezone_name=timezone_name,
                period=period,
                from_ms=(
                    _query_int("from_ms", 0)
                    if "from_ms" in request.args
                    else None
                ),
                to_ms=(
                    _query_int("to_ms", 0) if "to_ms" in request.args else None
                ),
            )
        except ValueError as error:
            return _error("INVALID_FILTER", str(error), 400)
        window = result.window
        if result.timeline_too_large:
            return _ok(
                _dashboard_timeline_limit_warning(
                    start_ms=window.start_ms,
                    end_ms=window.end_ms,
                    period=window.period,
                    timezone_name=timezone_name,
                )
            )
        items = result.items
        for value in items:
            raw_events = value.get("events")
            if isinstance(raw_events, list):
                for raw_event in raw_events:
                    if isinstance(raw_event, dict) and isinstance(
                        raw_event.get("timestamp_ms"), int
                    ):
                        raw_event["timestamp"] = format_timestamp(
                            raw_event["timestamp_ms"], timezone_name
                        )
            timestamp_ms = value.get("timestamp_ms")
            if isinstance(timestamp_ms, int):
                value["timestamp"] = format_timestamp(timestamp_ms, timezone_name)
            reset_at_ms = value.get("reset_at_ms")
            if isinstance(reset_at_ms, int):
                value["reset_at"] = format_timestamp(reset_at_ms, timezone_name)
        data = {
            "from_ms": window.start_ms,
            "to_ms": window.end_ms,
            "period": window.period,
            "timezone": timezone_name,
            "items": items,
        }
        # 最終JSONのbyte数も制限する。件数上限内でも各eventの長い文字列により
        # 応答が膨らむ場合があるため、送信前に同じ絞込み案内へ切り替える。
        response = _ok(data)
        if len(response.get_data()) > _DASHBOARD_TIMELINE_MAX_JSON_BYTES:
            return _ok(
                _dashboard_timeline_limit_warning(
                    start_ms=window.start_ms,
                    end_ms=window.end_ms,
                    period=window.period,
                    timezone_name=timezone_name,
                )
            )
        return response


