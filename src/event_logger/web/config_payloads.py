"""Web設定の入力検証、Domainモデル生成、表示用payloadへの変換。

ここではHTTP route登録を行わず、必要なRepositoryはWebContext経由で受け取る。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from event_logger.domain.rules import ConditionRule, EdgeDirection, OutputRule, RuleType
from event_logger.domain.widgets import (
    Widget,
    WidgetSize,
    WidgetType,
    normalize_widget_config,
)

if TYPE_CHECKING:
    from event_logger.web.app import WebContext


def _json_int(value: object, field_name: str) -> int:
    """JSON payloadの整数を検証する。boolを整数として受理しない。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if not -(2**63) <= value < 2**63:
        raise ValueError(f"{field_name} is outside the supported integer range")
    return value


def _json_bool(value: object, field_name: str) -> bool:
    """JSON booleanを検証する。0/1や文字列を暗黙変換しない。"""
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _json_text(value: object, field_name: str) -> str:
    """JSON文字列を設定Repositoryへ渡す。"""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _build_output_rule(
    context: WebContext,
    payload: Mapping[str, object],
    rule_id: int,
    *,
    exclude_rule_id: int | None = None,
) -> OutputRule:
    """HTTP payloadを固定ルールDomainへ変換する。"""
    name = _json_text(payload.get("name"), "name")
    input_channel = _json_int(payload.get("input_channel"), "input_channel")
    output_channel = _json_int(payload.get("output_channel"), "output_channel")
    if not 1 <= input_channel <= 6 or not 1 <= output_channel <= 6:
        raise ValueError("input_channel and output_channel must be between 1 and 6")
    rule_type_text = _json_text(payload.get("condition"), "condition")
    try:
        rule_type = RuleType(rule_type_text)
    except ValueError as error:
        raise ValueError("unsupported condition") from error
    enabled = _json_bool(payload.get("enabled", True), "enabled")
    channel = context.channels.get(input_channel)
    if channel is None:
        raise ValueError("input channel was not found")
    if enabled and not channel.enabled:
        raise ValueError("input channel must be enabled before enabling a rule")
    duration_ms = _json_int(payload.get("duration_ms"), "duration_ms")
    if duration_ms < 1:
        raise ValueError("duration_ms must be positive")
    expected_value = payload.get("value", False)
    if not isinstance(expected_value, bool):
        raise ValueError("value must be a boolean")
    edge_text = _json_text(payload.get("edge", EdgeDirection.RISING.value), "edge")
    try:
        edge = EdgeDirection(edge_text)
    except ValueError as error:
        raise ValueError("unsupported edge") from error
    condition = ConditionRule(
        id=rule_id,
        input_channel=input_channel,
        output_channel=output_channel,
        rule_type=rule_type,
        duration_ms=duration_ms,
        enabled=enabled,
        expected_value=expected_value,
        edge=edge,
    )
    # 無効ルールも設定上のOUT ownerを保持するため、enabledの値に関係なく
    # 既存ルール・Heartbeatとの重複を拒否する。無効ルールを別用途へ再利用
    # すると、後から再有効化した時点で1 OUT = 1用途の境界を壊してしまう。
    if any(
        existing.id != exclude_rule_id
        and existing.condition.output_channel == output_channel
        for existing in context.output_rules.list_all()
    ):
        raise ValueError("output channel is already assigned to another rule")
    heartbeat = context.settings.get_json(
        "heartbeat",
        {"enabled": False, "channel_id": None},
    )
    if isinstance(heartbeat, dict) and heartbeat.get("enabled") is True:
        if heartbeat.get("channel_id") == output_channel:
            raise ValueError("output channel is already assigned to Heartbeat")
    return OutputRule(
        id=rule_id,
        name=name,
        condition=condition,
        updated_at_ms=context.clock.now_utc_ms(),
    )


def _build_widget(
    context: WebContext,
    payload: Mapping[str, object],
    *,
    widget_id: str | None,
    base: Widget | None = None,
) -> Widget:
    """HTTP payloadを固定widget Domainへ変換する。"""
    raw_type = payload.get("type", base.widget_type.value if base else None)
    widget_type_text = _json_text(raw_type, "type")
    try:
        widget_type = WidgetType(widget_type_text)
    except ValueError as error:
        raise ValueError("unsupported widget type") from error
    title = _json_text(payload.get("title", base.title if base else None), "title")
    raw_config = payload.get("config")
    if raw_config is None:
        config: Mapping[str, object] = dict(base.config) if base is not None else {}
        reserved = {"id", "type", "title", "config", "size", "sort_order", "enabled"}
        config = {
            **config,
            **{key: value for key, value in payload.items() if key not in reserved},
        }
    elif not isinstance(raw_config, Mapping):
        raise ValueError("config must be an object")
    else:
        config = raw_config
    normalized_config = normalize_widget_config(config)
    resolved_id = widget_id
    if resolved_id is None:
        raw_id = payload.get("id")
        resolved_id = (
            _json_text(raw_id, "id")
            if raw_id is not None
            else context.widgets.next_id()
        )
    raw_size = payload.get(
        "size",
        base.size.value
        if base is not None
        else WidgetSize.LARGE.value
        if widget_type is WidgetType.TIMELINE
        else WidgetSize.SMALL.value,
    )
    size_text = _json_text(raw_size, "size")
    try:
        size = WidgetSize(size_text)
    except ValueError as error:
        raise ValueError("size must be small, medium, or large") from error
    sort_order = _json_int(
        payload.get(
            "sort_order",
            base.sort_order if base is not None else len(context.widgets.list_all()),
        ),
        "sort_order",
    )
    enabled = _json_bool(
        payload.get("enabled", base.enabled if base is not None else True),
        "enabled",
    )
    return Widget(
        id=resolved_id,
        widget_type=widget_type,
        title=title,
        config=normalized_config,
        size=size,
        sort_order=sort_order,
        enabled=enabled,
        updated_at_ms=context.clock.now_utc_ms(),
    )


def _counter_definition_changed(before: Widget, after: Widget) -> bool:
    """reset counterの対象条件が変わったか返す。"""
    if before.widget_type is not WidgetType.EVENT_COUNT:
        return after.widget_type is WidgetType.EVENT_COUNT
    before_mode = before.config.get("count_mode", "period")
    after_mode = after.config.get("count_mode", "period")
    if before_mode != after_mode:
        return True
    if after_mode != "reset":
        return False
    return before.config.get("channels") != after.config.get(
        "channels"
    ) or before.config.get("edge", "rising") != after.config.get("edge", "rising")


def _output_rule_payload(rule: OutputRule) -> dict[str, Any]:
    """OutputRuleを内部Web responseへ変換する。"""
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.condition.enabled,
        "condition": rule.condition.rule_type.value,
        "input_channel": rule.condition.input_channel,
        "output_channel": rule.condition.output_channel,
        "duration_ms": rule.condition.duration_ms,
        "value": rule.condition.expected_value,
        "edge": rule.condition.edge.value,
    }


def _widget_payload(widget: Widget) -> dict[str, Any]:
    """WidgetをJSON responseへ変換する。"""
    return {
        "id": widget.id,
        "type": widget.widget_type.value,
        "title": widget.title,
        "config": dict(widget.config),
        "size": widget.size.value,
        "sort_order": widget.sort_order,
        "enabled": widget.enabled,
        "updated_at_ms": widget.updated_at_ms,
    }
