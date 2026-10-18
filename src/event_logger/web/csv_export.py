"""入力・出力イベントをCSVへ逐次変換する表示アダプター。

HTTP Response生成とDB接続のライフサイクルは呼び出し側が管理する。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator, Mapping

from event_logger.common.models import ChannelConfig, LogRecord, OutputSource
from event_logger.common.timeutil import format_timestamp


# 長い履歴をCSV化するときはログ取得順と表記規則を一定に保つ。
def generate_csv(
    events: Iterable[LogRecord],
    channel_map: Mapping[int, ChannelConfig],
    rule_names: Mapping[int, str],
    timezone_name: str,
    *,
    rows_per_chunk: int = 1_000,
) -> Iterator[str]:
    """既存のCSV列順とBOMを維持したまま、一定行数ごとに出力する。"""
    if rows_per_chunk < 1:
        raise ValueError("rows_per_chunk must be positive")
    yield "\ufeff"
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(
        [
            "timestamp",
            "signal",
            "name",
            "value",
            "kind",
            "source",
            "clock_synced",
        ]
    )
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)
    rows_since_yield = 0
    for event in events:
        channel = channel_map[event.channel_id]
        if event.record_type == "input":
            signal = f"IN{event.channel_id}"
            name = channel.name
        else:
            signal = f"OUT{event.channel_id}"
            name = _output_name(event, rule_names)
        writer.writerow(
            [
                format_timestamp(event.ts_utc_ms, timezone_name),
                signal,
                _csv_text(name),
                "ON" if event.value else "OFF",
                event.kind,
                event.source.value if event.source is not None else "",
                str(event.clock_synced).lower(),
            ]
        )
        rows_since_yield += 1
        if rows_since_yield >= rows_per_chunk:
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)
            rows_since_yield = 0
    if rows_since_yield:
        yield output.getvalue()


# 改行・引用符・区切り文字を含む値がCSVの列境界を壊さないようエスケープする。
def _csv_text(value: str) -> str:
    """表計算ソフトが利用者の名称を式として扱わないよう文字列にする。"""
    # 引用符で囲むだけでは式解釈を防げない。制御文字や空白の後に式開始文字が
    # ある場合も対象にし、生ログ・保存済み名称そのものは変更しない。
    inspected = value.lstrip()
    if inspected.startswith(("=", "+", "-", "@")) or value.startswith(
        ("\t", "\r", "\n")
    ):
        return "'" + value
    return value


# OUTの表示名は関連ルール名を参照して利用者が判別できる形にする。
def _output_name(event: LogRecord, rule_names: Mapping[int, str]) -> str:
    """初期版でDBに保存するOUT用途名を返す。"""
    names = {
        OutputSource.HEARTBEAT: "Heartbeat",
        OutputSource.TEST: "Output test",
        OutputSource.STARTUP: "Startup",
        OutputSource.RULE: f"OUT{event.channel_id} rule",
    }
    if event.source is None:
        raise ValueError("output log record must have a source")
    if event.source is OutputSource.RULE and event.channel_id in rule_names:
        return rule_names[event.channel_id]
    return names[event.source]
