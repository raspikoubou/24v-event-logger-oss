"""SQLite Repository間で共有する行変換・入力検証・SQL補助関数。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Literal, cast

from event_logger.common.models import (
    ChannelConfig,
    CounterReset,
    EventKind,
    InputEvent,
    InputState,
    LogKind,
    LogRecord,
    LogRecordType,
    OutputEvent,
    OutputSource,
)
from event_logger.database.log_query import _build_log_query as _build_log_query
from event_logger.database.log_query import _channel_clause as _channel_clause
from event_logger.database.log_query import _split_signals as _split_signals
from event_logger.database.output_rule_codec import (
    _output_rule_config as _output_rule_config,
)
from event_logger.database.output_rule_codec import (
    _output_rule_from_row as _output_rule_from_row,
)
from event_logger.domain.widgets import Widget, WidgetSize, WidgetType


def _counter_reset_from_row(row: sqlite3.Row) -> CounterReset:
    """SQLite rowをCounterResetへ変換する。

    Args:
        row: SQLite row mapping。 column値を検査してdomain valueへ移す。

    Returns:
        CounterReset型の結果。 戻り値の型に応じ、
            該当なしはNoneまたは空collectionとなる。
    """
    return CounterReset(
        id=int(row["id"]),
        widget_id=str(row["widget_id"]),
        reset_at_ms=int(row["reset_at_ms"]),
        after_event_id=int(row["after_event_id"]),
        carried_count=int(row["carried_count"]),
    )


def _widget_from_row(row: sqlite3.Row) -> Widget:
    """SQLite rowを検証付きWidgetへ変換する。

    Args:
        row: SQLite row mapping。 column値を検査してdomain valueへ移す。

    Returns:
        Widget型の結果。 戻り値の型に応じ、 該当なしはNoneまたは空collectionとなる。

    Raises:
        ValueError: 「widget config must be an object」に該当すると判定した場合。
    """
    try:
        config = json.loads(str(row["config_json"]))
        if not isinstance(config, dict):
            raise ValueError("widget config must be an object")
        widget_type = WidgetType(str(row["type"]))
        size = WidgetSize(str(row["size"]))
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"invalid widget configuration: {row['id']}") from error
    return Widget(
        id=str(row["id"]),
        widget_type=widget_type,
        title=str(row["title"]),
        config=config,
        size=size,
        sort_order=int(row["sort_order"]),
        enabled=bool(row["enabled"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _event_from_row(row: sqlite3.Row) -> InputEvent:
    """SQLite rowをInputEventへ変換する。

    Args:
        row: SQLite row mapping。 column値を検査してdomain valueへ移す。

    Returns:
        InputEvent型の結果。 戻り値の型に応じ、 該当なしはNoneまたは空collectionとなる。
    """
    return InputEvent(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        ts_utc_ms=int(row["ts_utc_ms"]),
        mono_ns=int(row["mono_ns"]),
        channel_id=int(row["channel_id"]),
        value=_input_state(row["value"]),
        kind=EventKind(int(row["kind"])),
        clock_synced=bool(row["clock_synced"]),
    )


def _output_event_from_row(row: sqlite3.Row) -> OutputEvent:
    """SQLite rowをOutputEventへ変換する。

    Args:
        row: SQLite row mapping。 column値を検査してdomain valueへ移す。

    Returns:
        OutputEvent型の結果。 戻り値の型に応じ、
            該当なしはNoneまたは空collectionとなる。
    """
    return OutputEvent(
        id=int(row["id"]),
        session_id=(int(row["session_id"]) if row["session_id"] is not None else None),
        ts_utc_ms=int(row["ts_utc_ms"]),
        mono_ns=int(row["mono_ns"]),
        output_channel=int(row["output_channel"]),
        value=_input_state(row["value"]),
        source=OutputSource(str(row["source"])),
        clock_synced=bool(row["clock_synced"]),
    )


def _channel_from_row(row: sqlite3.Row) -> ChannelConfig:
    """SQLite rowをChannelConfigへ変換する。

    Args:
        row: SQLite row mapping。 column値を検査してdomain valueへ移す。

    Returns:
        ChannelConfig型の結果。 戻り値の型に応じ、
            該当なしはNoneまたは空collectionとなる。
    """
    return ChannelConfig(
        id=int(row["id"]),
        enabled=bool(row["enabled"]),
        name=str(row["name"]),
        debounce_ms=int(row["debounce_ms"]),
        on_label=str(row["on_label"]),
        off_label=str(row["off_label"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _log_record_from_row(row: sqlite3.Row) -> LogRecord:
    """UNION queryのSQLite rowを統合ログモデルへ変換する。

    Args:
        row: SQLite row mapping。 column値を検査してdomain valueへ移す。

    Returns:
        LogRecord型の結果。 戻り値の型に応じ、 該当なしはNoneまたは空collectionとなる。

    Raises:
        ValueError: 「stored log record type is invalid」に該当すると判定した場合。
            「stored log kind is invalid」に該当すると判定した場合。
    """
    raw_record_type = row["record_type"]
    raw_kind = row["kind"]
    raw_source = row["source"]
    if raw_record_type not in ("input", "output"):
        raise ValueError("stored log record type is invalid")
    if raw_kind not in ("edge", "snapshot", "output"):
        raise ValueError("stored log kind is invalid")
    source = OutputSource(str(raw_source)) if raw_source is not None else None
    return LogRecord(
        id=int(row["id"]),
        record_type=cast(LogRecordType, raw_record_type),
        ts_utc_ms=int(row["ts_utc_ms"]),
        mono_ns=int(row["mono_ns"]),
        channel_id=int(row["channel_id"]),
        value=_input_state(row["value"]),
        kind=cast(LogKind, raw_kind),
        source=source,
        clock_synced=bool(row["clock_synced"]),
    )


def _validate_channel_ids(channel_ids: Sequence[int]) -> None:
    """チャンネルID列を検証する。

    Args:
        channel_ids: 対象IN番号の列。 各番号を1〜6に制限し、 query filterへ渡す。

    Returns:
        None。 制約を検証するだけで、 永続状態や入力値は変更しない。

    Raises:
        ValueError: channel_idが1〜6の範囲外の場合。
    """
    if any(not 1 <= channel_id <= 6 for channel_id in channel_ids):
        raise ValueError("channel_id must be between 1 and 6")


def _validate_channel_text(value: str, field_name: str) -> None:
    """設定画面から保存する名称を検証する。

    Args:
        value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。
        field_name: validation errorに示すfield名。

    Returns:
        None。 制約を検証するだけで、 永続状態や入力値は変更しない。

    Raises:
        ValueError: deviceまたはSQLite操作で失敗が返された場合。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > 100:
        raise ValueError(f"{field_name} must be at most 100 characters")


def _mapping_int(values: Mapping[str, object], key: str) -> int:
    """設定mappingからboolを除外した整数を取得する。

    Args:
        values: 複数設定のkey/value群。 一括更新時に一つのtransactionへまとめる。
        key: 設定項目名。 保存値の取得または更新対象を特定する。

    Returns:
        条件に従って算出した整数値。

    Raises:
        ValueError: deviceまたはSQLite操作で失敗が返された場合。
    """
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _mapping_text(values: Mapping[str, object], key: str) -> str:
    """設定mappingから文字列を取得する。

    Args:
        values: 複数設定のkey/value群。 一括更新時に一つのtransactionへまとめる。
        key: 設定項目名。 保存値の取得または更新対象を特定する。

    Returns:
        検査または正規化を通過した文字列値。

    Raises:
        ValueError: deviceまたはSQLite操作で失敗が返された場合。
    """
    value = values.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    """INSERT直後のrowidを必ず整数として取得する。

    Args:
        cursor: SQL statementを実行したSQLite cursor。 lastrowid取得に使う。

    Returns:
        保存先で次に使う、 または直近に割り当てられた整数ID。

    Raises:
        RuntimeError: 「SQLite did not return a row id」に該当すると判定した場合。
    """
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return a row id")
    return int(cursor.lastrowid)


def _input_state(raw_value: object) -> InputState:
    """SQLite値を論理入力値へ検証付き変換する。

    Args:
        raw_value: SQLiteまたはJSON境界から復元した未検証値。

    Returns:
        InputState型の結果。 戻り値の型に応じ、 該当なしはNoneまたは空collectionとなる。

    Raises:
        ValueError: 「stored event value must be an integer」に該当すると判定した場合。
            論理値が0または1以外の場合。
    """
    if not isinstance(raw_value, int):
        raise ValueError("stored event value must be an integer")
    value = raw_value
    if value not in (0, 1):
        raise ValueError("stored event value must be 0 or 1")
    return cast(Literal[0, 1], value)


def _delete_ids(
    connection: sqlite3.Connection,
    table_name: str,
    rows: Sequence[sqlite3.Row],
) -> None:
    """許可済み一次event tableのIDだけをparameter bindingで削除する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。
        table_name: retention対象とする内部固定table名。 外部SQLとして扱わない。
        rows: 処理対象のSQLite row列。

    Returns:
        None。 渡されたSQLite接続へ変更を適用し、 接続のcloseは呼出元に残す。

    Raises:
        ValueError: 「unsupported retention table」に該当すると判定した場合。
    """
    if not rows:
        return
    if table_name not in {"events", "output_events"}:
        raise ValueError("unsupported retention table")
    placeholders = ", ".join("?" for _ in rows)
    connection.execute(
        f"DELETE FROM {table_name} WHERE id IN ({placeholders})",
        [int(row["id"]) for row in rows],
    )


def _counter_config(raw_config: object) -> dict[str, object] | None:
    """widget config JSONからcounter条件を安全に取り出す。

    Args:
        raw_config: DBに保存された未検証JSON config。

    Returns:
        widget APIへ返す表示field/value mapping。
    """
    if not isinstance(raw_config, str):
        return None
    try:
        config = json.loads(raw_config)
    except json.JSONDecodeError:
        return None
    return config if isinstance(config, dict) else None


def _counter_event_matches(
    event_row: sqlite3.Row,
    config: Mapping[str, object],
) -> bool:
    """初期版counterのchannel/edge条件へ1eventを照合する。

    Args:
        event_row: DB queryが返した単一event row。 格納値を検査してdomain値へ変換する。
        config: widgetまたはruleのJSON相当設定mapping。 式やSQLとして実行しない。

    Returns:
        判定対象が条件を満たす場合True、 その他はFalse。
    """
    channel_id = config.get("channel_id")
    if isinstance(channel_id, bool) or not isinstance(channel_id, int):
        return False
    if int(event_row["channel_id"]) != channel_id:
        return False
    if int(event_row["kind"]) != int(EventKind.EDGE):
        return False
    edge = config.get("edge", "rising")
    value = int(event_row["value"])
    if edge == "rising":
        return value == 1
    if edge == "falling":
        return value == 0
    if edge in ("both", "any"):
        return value in (0, 1)
    return False
