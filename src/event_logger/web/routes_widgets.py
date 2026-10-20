"""出力ルール・ウィジェット設定用HTTPルート。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from flask import Flask, Response, request

from event_logger.domain.widgets import WidgetType
from event_logger.loggerd.ipc import IpcProtocolError, IpcRemoteError
from event_logger.web.config_payloads import (
    _build_output_rule,
    _build_widget,
    _counter_definition_changed,
    _output_rule_payload,
    _widget_payload,
)
from event_logger.web.control_service import (
    _reload_after_rollback,
    _request_config_reload,
    _save_counter_reset,
)
from event_logger.web.http_responses import _error, _ok
from event_logger.web.request_context import WebContext


def _register_output_rule_routes(app: Flask, context: WebContext) -> None:
    """条件出力ルールのCRUD routeをappへ登録する。

        Args:
            app: Flask application。
            context: requestごとのRepository、Clock、設定値を保持するWebContext。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    @app.post("/api/output-rules")
    def create_output_rule() -> Response:
        """固定スキーマの条件ルールを保存し、loggerd reloadを確認する。

            Args:
                なし。

            Returns:
                保存・reload成功時は作成rule payloadをJSON 200で返す。
                    body/schema/DB制約不正は400、
                    loggerd拒否は409、IPC不通は503。
                        reload失敗時はDBのruleを削除して戻す。
        """
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
            context.output_rules.delete(rule.id, ts_utc_ms=context.clock.now_utc_ms())
            _reload_after_rollback(context)
            return _error("LOGGERD_UNAVAILABLE", str(error), 503)
        return _ok(_output_rule_payload(rule))

    @app.put("/api/output-rules/<int:rule_id>")
    def update_output_rule(rule_id: int) -> Response:
        """条件ルールを編集・無効化し、loggerd reloadを確認する。

            Args:
                rule_id: 対象条件出力ルールの永続ID。

            Returns:
                更新後のrule payloadをJSON 200で返す。未存在は404、
                    body/schema/DB制約不正は400、
                    loggerd拒否は409、IPC不通は503。reload失敗時は以前のruleへ復元する。
        """
        current = context.output_rules.get(rule_id)
        if current is None:
            return _error("NOT_FOUND", "output rule was not found", 404)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        # PUTは部分fieldだけを受けても既存設定を失わないようにcurrent値へmergeし、
        # 共通builderで再検証してからDBへ保存する。
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
        # DB保存後にloggerd ACKを待つ。拒否/通信失敗は後続節で前のruleへ復元し、
        # Webとdaemonで適用済み設定が異なる状態を残さない。
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
        """保存済み条件ルールを設定画面へ返す。

            Args:
                なし。

            Returns:
                保存済みrule payload列をitemsに格納したJSON 200を返す。
        """
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
        """ルールを削除し、reload失敗時は設定を復元する。

            Args:
                rule_id: 対象条件出力ルールの永続ID。

            Returns:
                削除・reload成功時は削除結果をJSON 200で返す。未存在は404、
                    loggerd拒否は409、IPC不通は503。
                    reload失敗時はruleを復元する。
        """
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
    """Dashboard widgetのCRUD routeをappへ登録する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    @app.get("/api/widgets")
    def list_widgets() -> Response:
        """保存済みDashboard widget設定を返す。

            Args:
                なし。

            Returns:
                保存済みwidget設定列をitemsとしてJSON 200で返す。
        """
        return _ok(
            {
                "items": [
                    _widget_payload(widget) for widget in context.widgets.list_all()
                ]
            }
        )

    @app.post("/api/widgets")
    def create_widget() -> Response:
        """固定種別のDashboard widgetを作成する。

            Args:
                なし。

            Returns:
                新規widget payloadをJSON 201で返す。bodyやwidget schema不正、
                    ID・SQLite範囲違反はINVALID_REQUEST/400。
        """
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
        """Dashboard widgetを更新する。

            Args:
                widget_id: 対象Dashboard widgetの永続ID。

            Returns:
                変更後widget payloadをJSON 200で返す。未存在は404、bodyや設定値不正、
                    SQLite範囲外は400。
        """
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
                _save_counter_reset(context, widget_id, widget=widget)
            else:
                context.widgets.update(widget)
        except (TypeError, ValueError, sqlite3.IntegrityError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        except (OSError, IpcProtocolError, IpcRemoteError, RuntimeError) as error:
            return _error("COUNTER_RESET_UNAVAILABLE", str(error), 503)
        return _ok(_widget_payload(widget))

    @app.delete("/api/widgets/<widget_id>")
    def delete_widget(widget_id: str) -> Response:
        """Dashboard widget設定だけを削除する。生イベントは保持する。

            Args:
                widget_id: 対象Dashboard widgetの永続ID。

            Returns:
                削除したwidget IDを含むJSON 200を返す。widget不存在は404。
                    過去eventsは削除しない。
        """
        if context.widgets.get(widget_id) is None:
            return _error("NOT_FOUND", "widget was not found", 404)
        context.widgets.delete(widget_id, ts_utc_ms=context.clock.now_utc_ms())
        return _ok({"deleted": widget_id})

    @app.post("/api/widgets/<widget_id>/duplicate")
    def duplicate_widget(widget_id: str) -> Response:
        """widgetを複製して新しいID・表示順を割り当てる。

            Args:
                widget_id: 対象Dashboard widgetの永続ID。

            Returns:
                複製した新ID・表示順のwidget payloadをJSON 201で返す。
                    元widget不存在は404、複製可能数超過・設定不正は400。
        """
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
        """リセット後カウントwidgetの基準event IDを保存する。

            Args:
                widget_id: 対象Dashboard widgetの永続ID。

            Returns:
                新しいafter_event_id境界とcounter値を含むJSON 200を返す。
                    widget不存在・種類違い・入力不正は400/404、
                    制御要求失敗は該当409/503。
        """
        widget = context.widgets.get(widget_id)
        if widget is None:
            return _error("NOT_FOUND", "widget was not found", 404)
        if widget.widget_type is not WidgetType.EVENT_COUNT:
            return _error("INVALID_REQUEST", "widget is not an event counter", 400)
        if widget.config.get("count_mode", "period") != "reset":
            return _error("INVALID_REQUEST", "widget is not reset-based", 400)
        try:
            reset = _save_counter_reset(context, widget.id)
        except (OSError, IpcProtocolError, IpcRemoteError, RuntimeError) as error:
            return _error("COUNTER_RESET_UNAVAILABLE", str(error), 503)
        return _ok(
            {
                "id": reset.id,
                "widget_id": reset.widget_id,
                "reset_at_ms": reset.reset_at_ms,
                "after_event_id": reset.after_event_id,
                "carried_count": reset.carried_count,
            }
        )
