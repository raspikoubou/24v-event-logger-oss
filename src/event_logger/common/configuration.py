"""loggerdが適用する設定をWebと照合するためのfingerprint。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from event_logger.common.models import ChannelConfig
from event_logger.domain.rules import OutputRule


def configuration_fingerprint(
    channels: Sequence[ChannelConfig],
    rules: Sequence[OutputRule],
    heartbeat: Mapping[str, object],
) -> str:
    """loggerd動作に関係する設定を正規化してSHA-256化する。

    SQLiteのupdated_atなど運用監査用フィールドは含めず、 daemonが実際に
    読み込んだ入力・条件出力・Heartbeatの値だけを比較する。

    Args:
        channels: 入力channel設定列。 daemonの比較ではchannel ID順に正規化される。
        rules: 保存または評価する条件rule列。 IDと条件値を使って識別する。
        heartbeat: Heartbeat設定mapping。 daemon動作に関係するkeyをfingerprintへ含める。

    Returns:
        canonical JSONから算出したSHA-256 digestの64桁lowercase hex。
    """
    configuration = {
        "channels": [
            {
                "id": channel.id,
                "enabled": channel.enabled,
                "name": channel.name,
                "debounce_ms": channel.debounce_ms,
                "on_label": channel.on_label,
                "off_label": channel.off_label,
            }
            for channel in sorted(channels, key=lambda item: item.id)
        ],
        "rules": [
            {
                "id": rule.id,
                "name": rule.name,
                "input_channel": rule.condition.input_channel,
                "output_channel": rule.condition.output_channel,
                "condition": rule.condition.rule_type.value,
                "duration_ms": rule.condition.duration_ms,
                "enabled": rule.condition.enabled,
                "value": rule.condition.expected_value,
                "edge": rule.condition.edge.value,
            }
            for rule in sorted(rules, key=lambda item: item.id)
        ],
        "heartbeat": {
            "enabled": heartbeat.get("enabled", False),
            "channel_id": heartbeat.get("channel_id"),
            "interval_ms": heartbeat.get("interval_ms", 5_000),
        },
    }
    canonical = json.dumps(
        configuration,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
