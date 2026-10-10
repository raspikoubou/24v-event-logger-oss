"""SQLite output_rules行とDomain条件ルールの相互変換。"""

from __future__ import annotations

import json
import sqlite3

from event_logger.domain.rules import (
    ConditionRule,
    EdgeDirection,
    OutputRule,
    RuleType,
)


def _output_rule_from_row(row: sqlite3.Row) -> OutputRule:
    """output_rules rowを検証付きDomain ruleへ変換する。"""
    try:
        config = json.loads(str(row["config_json"]))
        if not isinstance(config, dict):
            raise ValueError("rule config must be an object")
        rule_type = RuleType(str(row["rule_type"]))
        input_channel = config["channel_id"]
        duration_ms = config["duration_ms"]
        if (
            isinstance(input_channel, bool)
            or not isinstance(input_channel, int)
            or isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
        ):
            raise ValueError("rule channel and duration must be integers")
        raw_value = config.get("value", 0)
        if isinstance(raw_value, bool):
            expected_value = raw_value
        elif isinstance(raw_value, int) and raw_value in (0, 1):
            expected_value = bool(raw_value)
        else:
            raise ValueError("rule value must be boolean or 0/1")
        edge = EdgeDirection(str(config.get("edge", EdgeDirection.RISING.value)))
        condition = ConditionRule(
            id=int(row["id"]),
            input_channel=input_channel,
            output_channel=int(row["output_channel"]),
            rule_type=rule_type,
            duration_ms=duration_ms,
            enabled=bool(row["enabled"]),
            expected_value=expected_value,
            edge=edge,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid output rule: {row['id']}") from error
    return OutputRule(
        id=condition.id,
        name=str(row["name"]),
        condition=condition,
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _output_rule_config(condition: ConditionRule) -> dict[str, object]:
    """ConditionRuleを仕様書のconfig_jsonへ変換する。"""
    config: dict[str, object] = {
        "channel_id": condition.input_channel,
        "duration_ms": condition.duration_ms,
    }
    if condition.rule_type is RuleType.STATE_DURATION:
        config["condition"] = RuleType.STATE_DURATION.value
        config["value"] = int(condition.expected_value)
    else:
        config["condition"] = RuleType.EVENT_ABSENCE.value
        config["edge"] = condition.edge.value
    return config
