"""履歴・CSV・診断・Dashboard表示のHTTPルート。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask, stream_with_context

from event_logger.domain.widget_values import evaluate_widget
from event_logger.domain.widgets import widget_end_ms, widget_start_ms
from event_logger.web.auth import SharedPasswordAuth
from event_logger.web.csv_export import generate_csv
from event_logger.web.dashboard_periods import _fixed_widget_calendar_ranges
from event_logger.web.dashboard_queries import (
    _DASHBOARD_TIMELINE_MAX_INTERVALS,
    _dashboard_events,
    _dashboard_output_events,
    _dashboard_period_counts,
    _dashboard_reset_counts,
    _dashboard_timeline_event_count_exceeds_limit,
    _dashboard_timeline_interval_count,
    _dashboard_timeline_limit_warning,
    _stored_widget_count,
)

if TYPE_CHECKING:
    from event_logger.web.app import WebContext


def _register_log_routes(app: Flask, context: WebContext) -> None:
    """検索ログとCSV出力のrouteをappへ登録する。

        Args:
            app: Flask application。
            context: requestごとのRepository、Clock、設定値を保持するWebContext。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    from event_logger.web.app import (
        Response,
        _effective_timezone,
        _error,
        _format_local_input,
        _log_payload,
        _ok,
        _parse_log_filters,
        request,
    )


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

    from event_logger.web.app import (
        Any,
        Response,
        UnixControlClient,
        _channel_payload,
        _clear_configuration_out_of_sync,
        _configuration_fingerprint,
        _daemon_status,
        _mark_configuration_out_of_sync,
        _ok,
        _stored_dashboard_custom_range,
        _stored_dashboard_period,
        _system_settings_payload,
    )


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
        payload["dashboard_period"] = _stored_dashboard_period(context)
        payload["dashboard_custom_range"] = _stored_dashboard_custom_range(context)
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

    from event_logger.web.app import (
        _DASHBOARD_TIMELINE_MAX_JSON_BYTES,
        Mapping,
        Response,
        _daemon_status,
        _dashboard_range_ms,
        _effective_timezone,
        _error,
        _ok,
        _query_int,
        _stored_dashboard_custom_range,
        _stored_dashboard_period,
        format_timestamp,
        request,
    )


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
        now_ms = context.clock.now_utc_ms()
        timezone_name = _effective_timezone(context)
        period = request.args.get("period")
        if period is None and request.args.get("from_ms") is None:
            period = _stored_dashboard_period(context)
        try:
            if period is None or period == "custom":
                stored_range = _stored_dashboard_custom_range(context)
                start_default = (
                    stored_range["from_ms"]
                    if stored_range is not None and "from_ms" not in request.args
                    else -1
                )
                end_default = (
                    stored_range["to_ms"]
                    if stored_range is not None
                    and "from_ms" not in request.args
                    and "to_ms" not in request.args
                    else now_ms
                )
                requested_start = _query_int("from_ms", start_default)
                requested_end = _query_int("to_ms", end_default)
                if requested_start < 0:
                    raise ValueError("custom range requires a non-negative from_ms")
            else:
                requested_start, requested_end = _dashboard_range_ms(
                    context.clock,
                    timezone_name=timezone_name,
                    period=period,
                )
        except ValueError as error:
            return _error("INVALID_FILTER", str(error), 400)
        start_ms = max(0, min(requested_start, now_ms - 1))
        period_end_ms = min(requested_end, now_ms)
        if start_ms >= period_end_ms:
            return _error(
                "INVALID_FILTER",
                "from_ms must be before to_ms and both must include past time",
                400,
            )
        now_mono_ns = context.clock.now_monotonic_ns()
        widgets = context.widgets.list_all(include_disabled=False)
        channels = context.channels.list_all()
        enabled_channel_ids = {channel.id for channel in channels if channel.enabled}
        disabled_channel_ids = {
            channel.id for channel in channels if not channel.enabled
        }
        try:
            fixed_calendar_ranges = _fixed_widget_calendar_ranges(
                widgets,
                context.clock,
                timezone_name=timezone_name,
            )
        except ValueError as error:
            return _error("INVALID_FILTER", str(error), 400)
        if _dashboard_timeline_event_count_exceeds_limit(
            context,
            widgets,
            start_ms=start_ms,
            period_end_ms=period_end_ms,
            now_ms=now_ms,
            fixed_calendar_ranges=fixed_calendar_ranges,
        ):
            # 上限超過時はcounter等の全件集計やloggerd IPCを始めない。
            # 部分表示も遅い巨大応答も返さず、絞込み案内を先に返す。
            return _ok(
                _dashboard_timeline_limit_warning(
                    start_ms=start_ms,
                    end_ms=period_end_ms,
                    period=period or "custom",
                    timezone_name=timezone_name,
                )
            )
        widget_ranges = {
            widget.id: (
                fixed_calendar_ranges[widget.id]
                if widget.id in fixed_calendar_ranges
                else (
                    widget_start_ms(
                        widget,
                        dashboard_start_ms=start_ms,
                        now_ms=now_ms,
                    ),
                    widget_end_ms(
                        widget,
                        dashboard_end_ms=period_end_ms,
                        now_ms=now_ms,
                    ),
                )
            )
            for widget in widgets
        }
        resets = {
            widget.id: context.counter_resets.latest(widget.id) for widget in widgets
        }
        reset_counts = _dashboard_reset_counts(context, widgets, resets)
        period_counts = _dashboard_period_counts(context, widgets, widget_ranges)
        stored_counts = {
            widget.id: _stored_widget_count(context, widget)
            for widget in widgets
            if widget.config.get("count_mode", "period") == "stored"
        }
        sessions = context.sessions.list_all()
        daemon_status, _, daemon_data = _daemon_status(context)
        raw_session_id = daemon_data.get("session_id")
        active_session_id = (
            raw_session_id
            if daemon_status == "logging" and isinstance(raw_session_id, int)
            else None
        )
        current_inputs: dict[int, bool] = {}
        current_input_since_ns: dict[int, int] = {}
        raw_inputs = daemon_data.get("inputs")
        if active_session_id is not None and isinstance(raw_inputs, Mapping):
            for raw_channel, raw_value in raw_inputs.items():
                try:
                    channel_id = int(raw_channel)
                except (TypeError, ValueError):
                    continue
                # loggerd statusが値を返しても、Web設定上監視していない入力は
                # eventへ記録されず、現在状態widgetではUNKNOWNとして扱う。
                if channel_id not in enabled_channel_ids:
                    continue
                if isinstance(raw_value, bool) or raw_value not in (0, 1):
                    continue
                current_inputs[channel_id] = bool(raw_value)
        raw_input_since = daemon_data.get("input_since_mono_ns")
        if active_session_id is not None and isinstance(raw_input_since, Mapping):
            for raw_channel, raw_since in raw_input_since.items():
                try:
                    channel_id = int(raw_channel)
                except (TypeError, ValueError):
                    continue
                if channel_id not in enabled_channel_ids:
                    continue
                if isinstance(raw_since, bool) or not isinstance(raw_since, int):
                    continue
                current_input_since_ns[channel_id] = raw_since
        if (
            active_session_id is None
            and daemon_status == "unknown"
            and context.control_client is None
        ):
            # Web単体のfixture/test serverにはloggerd statusがない。未終了の
            # 最新sessionを表示上のactiveとして扱い、OUT履歴のfixtureを
            # 直ちにUNKNOWNへ潰さない。実daemon接続時はstatusを正とする。
            open_sessions = [
                session.id for session in sessions if session.ended_at_ms is None
            ]
            if len(open_sessions) == 1:
                active_session_id = open_sessions[0]
        events = _dashboard_events(
            context,
            widgets,
            widget_ranges=widget_ranges,
            current_before_ms=now_ms + 1,
            active_session_id=active_session_id,
        )
        output_events = _dashboard_output_events(
            context,
            widgets,
            widget_ranges=widget_ranges,
        )
        items: list[dict[str, object]] = []
        for widget in widgets:
            reset = resets[widget.id]
            fixed_calendar_range = fixed_calendar_ranges.get(widget.id)
            try:
                value = evaluate_widget(
                    widget,
                    events=events,
                    sessions=sessions,
                    now_ms=now_ms,
                    start_ms=start_ms,
                    period_end_ms=period_end_ms,
                    fixed_period_range=fixed_calendar_range,
                    active_session_id=active_session_id,
                    now_mono_ns=now_mono_ns,
                    current_inputs=current_inputs,
                    current_input_since_ns=current_input_since_ns,
                    disabled_channel_ids=frozenset(disabled_channel_ids),
                    output_events=output_events,
                    carried_count=reset.carried_count if reset else 0,
                    after_event_id=reset.after_event_id if reset else None,
                    reset_at_ms=reset.reset_at_ms if reset else None,
                    reset_count=reset_counts.get(widget.id),
                    period_count=period_counts.get(widget.id),
                    stored_count=stored_counts.get(widget.id),
                )
            except ValueError as error:
                value = {
                    "widget_id": widget.id,
                    "type": widget.widget_type.value,
                    "title": widget.title,
                    "error": str(error),
                }
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
            items.append(value)
        if (
            _dashboard_timeline_interval_count(items)
            > _DASHBOARD_TIMELINE_MAX_INTERVALS
        ):
            return _ok(
                _dashboard_timeline_limit_warning(
                    start_ms=start_ms,
                    end_ms=period_end_ms,
                    period=period or "custom",
                    timezone_name=timezone_name,
                )
            )
        data = {
            "from_ms": start_ms,
            "to_ms": period_end_ms,
            "period": period or "custom",
            "timezone": timezone_name,
            "items": items,
        }
        response = _ok(data)
        if len(response.get_data()) > _DASHBOARD_TIMELINE_MAX_JSON_BYTES:
            return _ok(
                _dashboard_timeline_limit_warning(
                    start_ms=start_ms,
                    end_ms=period_end_ms,
                    period=period or "custom",
                    timezone_name=timezone_name,
                )
            )
        return response


