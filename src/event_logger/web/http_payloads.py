"""Repository modelからWeb応答へ変換するpayload builder。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from event_logger.common.models import ChannelConfig, LogRecord
from event_logger.common.timeutil import format_timestamp
from event_logger.web.csv_export import _output_name


def _channel_payload(channel: ChannelConfig) -> dict[str, Any]:
    """入力チャンネルの表示可能な設定だけをAPI payloadへ変換する。

    Args:
        channel: 表示対象チャンネル。

    Returns:
        id/enabled/name/debounce/labelを含むJSON mapping。GPIO定義は含めない。
    """
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
    """統合ログレコードをtimezone付きWeb payloadへ変換する。

    Args:
        event: IN/OUT repositoryから返されたログ記録。
        channel_map: 入力番号とchannel設定の対応表。
        rule_names: 出力rule IDと表示名の対応表。
        timezone_name: 表示時刻へ使うIANA timezone名。

    Returns:
        epoch値と表示文字列、論理signal/valueを持つJSON mapping。
    """
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
