"""出力ルール・ウィジェット設定用HTTPルート。"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from flask import Flask

if TYPE_CHECKING:
    from event_logger.web.app import WebContext


def _register_output_rule_routes(app: Flask, context: WebContext) -> None:
    """条件出力ルールのCRUD routeをappへ登録する。"""

    from event_logger.web.app import (
        IpcProtocolError,
        IpcRemoteError,
        Response,
        _build_output_rule,
        _error,
        _ok,
        _output_rule_payload,
        _reload_after_rollback,
        _request_config_reload,
        request,
        sqlite3,
    )


    @app.post("/api/output-rules")
    def create_output_rule() -> Response:
        """固定スキーマの条件ルールを保存し、loggerd reloadを確認する。"""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        try:
            rule = _build_output_rule(context, payload, context.output_rules.next_id())
            context.output_rules.create(rule)
        except (TypeError, ValueError, sqlite3.IntegrityError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        try:
            if context.control_client is None:
                raise RuntimeError("loggerd reload was not acknowledged")
            _request_config_reload(context)
        except IpcRemoteError as error:
            context.output_rules.delete(
                rule.id,
                ts_utc_ms=context.clock.now_utc_ms(),
            )
            _reload_after_rollback(context)
            return _error(error.code, error.message, 409)
        except (OSError, IpcProtocolError, RuntimeError) as error:
            context.output_rules.delete(
                rule.id, ts_utc_ms=context.clock.now_utc_ms()
            )
            _reload_after_rollback(context)
            return _error("LOGGERD_UNAVAILABLE", str(error), 503)
        return _ok(_output_rule_payload(rule))

    @app.put("/api/output-rules/<int:rule_id>")
    def update_output_rule(rule_id: int) -> Response:
        """条件ルールを編集・無効化し、loggerd reloadを確認する。"""
        current = context.output_rules.get(rule_id)
        if current is None:
            return _error("NOT_FOUND", "output rule was not found", 404)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        condition = current.condition
        merged: dict[str, object] = {
            "name": current.name,
            "condition": condition.rule_type.value,
            "input_channel": condition.input_channel,
            "output_channel": condition.output_channel,
            "duration_ms": condition.duration_ms,
            "value": condition.expected_value,
            "edge": condition.edge.value,
            "enabled": condition.enabled,
        }
        merged.update(payload)
        try:
            updated = _build_output_rule(
                context,
                merged,
                rule_id,
                exclude_rule_id=rule_id,
            )
            context.output_rules.update(updated)
        except (TypeError, ValueError, sqlite3.IntegrityError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        try:
            if context.control_client is None:
                raise RuntimeError("loggerd reload was not acknowledged")
            _request_config_reload(context)
        except IpcRemoteError as error:
            context.output_rules.update(current)
            _reload_after_rollback(context)
            return _error(error.code, error.message, 409)
        except (OSError, IpcProtocolError, RuntimeError) as error:
            context.output_rules.update(current)
            _reload_after_rollback(context)
            return _error("LOGGERD_UNAVAILABLE", str(error), 503)
        return _ok(_output_rule_payload(updated))

    @app.get("/api/output-rules")
    def list_output_rules() -> Response:
        """保存済み条件ルールを設定画面へ返す。"""
        return _ok(
            {
                "items": [
                    _output_rule_payload(rule)
                    for rule in context.output_rules.list_all()
                ]
            }
        )

    @app.delete("/api/output-rules/<int:rule_id>")
    def delete_output_rule(rule_id: int) -> Response:
        """ルールを削除し、reload失敗時は設定を復元する。"""
        rule = context.output_rules.get(rule_id)
        if rule is None:
            return _error("NOT_FOUND", "output rule was not found", 404)
        context.output_rules.delete(
            rule_id,
            ts_utc_ms=context.clock.now_utc_ms(),
        )
        try:
            if context.control_client is None:
                raise RuntimeError("loggerd reload was not acknowledged")
            _request_config_reload(context)
        except IpcRemoteError as error:
            context.output_rules.create(rule)
            _reload_after_rollback(context)
            return _error(error.code, error.message, 409)
        except (OSError, IpcProtocolError, RuntimeError) as error:
            context.output_rules.create(rule)
            _reload_after_rollback(context)
            return _error("LOGGERD_UNAVAILABLE", str(error), 503)
        return _ok({"deleted": rule_id})


def _register_widget_routes(app: Flask, context: WebContext) -> None:
    """Dashboard widgetのCRUD routeをappへ登録する。"""

    from event_logger.web.app import (
        Response,
        WidgetType,
        _build_widget,
        _counter_definition_changed,
        _error,
        _ok,
        _widget_payload,
        request,
        sqlite3,
    )


    @app.get("/api/widgets")
    def list_widgets() -> Response:
        """保存済みDashboard widget設定を返す。"""
        return _ok(
            {
                "items": [
                    _widget_payload(widget) for widget in context.widgets.list_all()
                ]
            }
        )

    @app.post("/api/widgets")
    def create_widget() -> Response:
        """固定種別のDashboard widgetを作成する。"""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        try:
            widget = _build_widget(context, payload, widget_id=None)
            context.widgets.create(widget)
        except (TypeError, ValueError, sqlite3.IntegrityError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        return _ok(_widget_payload(widget))

    @app.put("/api/widgets/<widget_id>")
    def update_widget(widget_id: str) -> Response:
        """Dashboard widgetを更新する。"""
        current = context.widgets.get(widget_id)
        if current is None:
            return _error("NOT_FOUND", "widget was not found", 404)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        try:
            widget = _build_widget(context, payload, widget_id=widget_id, base=current)
            counter_definition_changed = (
                widget.widget_type is WidgetType.EVENT_COUNT
                and widget.config.get("count_mode", "period") == "reset"
                and _counter_definition_changed(current, widget)
            )
            if counter_definition_changed:
                # retention済みの旧条件のcarry値は、新しい対象信号の累計へ
                # 流用できない。対象条件を変えた時点で新しいreset基準を作り、
                # 過去eventと派生carryを混在させない。
                context.widgets.update_with_counter_reset(
                    widget,
                    reset_at_ms=context.clock.now_utc_ms(),
                )
            else:
                context.widgets.update(widget)
        except (TypeError, ValueError, sqlite3.IntegrityError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        return _ok(_widget_payload(widget))

    @app.delete("/api/widgets/<widget_id>")
    def delete_widget(widget_id: str) -> Response:
        """Dashboard widget設定だけを削除する。生イベントは保持する。"""
        if context.widgets.get(widget_id) is None:
            return _error("NOT_FOUND", "widget was not found", 404)
        context.widgets.delete(widget_id, ts_utc_ms=context.clock.now_utc_ms())
        return _ok({"deleted": widget_id})

    @app.post("/api/widgets/<widget_id>/duplicate")
    def duplicate_widget(widget_id: str) -> Response:
        """widgetを複製して新しいID・表示順を割り当てる。"""
        current = context.widgets.get(widget_id)
        if current is None:
            return _error("NOT_FOUND", "widget was not found", 404)
        try:
            duplicate = replace(
                current,
                id=context.widgets.next_id(),
                sort_order=len(context.widgets.list_all()),
                updated_at_ms=context.clock.now_utc_ms(),
            )
            context.widgets.create(duplicate, audit_action="widget_duplicated")
        except (TypeError, ValueError, sqlite3.IntegrityError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        return _ok(_widget_payload(duplicate))

    @app.post("/api/widgets/<widget_id>/reset")
    def reset_widget_counter(widget_id: str) -> Response:
        """リセット後カウントwidgetの基準event IDを保存する。"""
        widget = context.widgets.get(widget_id)
        if widget is None:
            return _error("NOT_FOUND", "widget was not found", 404)
        if widget.widget_type is not WidgetType.EVENT_COUNT:
            return _error("INVALID_REQUEST", "widget is not an event counter", 400)
        if widget.config.get("count_mode", "period") != "reset":
            return _error("INVALID_REQUEST", "widget is not reset-based", 400)
        reset = context.counter_resets.create(
            widget_id=widget.id,
            reset_at_ms=context.clock.now_utc_ms(),
        )
        return _ok(
            {
                "id": reset.id,
                "widget_id": reset.widget_id,
                "reset_at_ms": reset.reset_at_ms,
                "after_event_id": reset.after_event_id,
                "carried_count": reset.carried_count,
            }
        )


