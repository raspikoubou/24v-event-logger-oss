"""IN/OUT統合ログの条件検証とSQL生成。

query生成はSQLite接続やtransactionの管理から独立させる。
UNION ALLと索引順のORDER BYを変更せず、過去の大量ログ検索性能を維持する。
"""

from __future__ import annotations

from collections.abc import Sequence


def _build_log_query(
    *,
    start_ms: int,
    end_ms: int,
    signals: Sequence[str] | None,
    values: Sequence[int] | None,
    kinds: Sequence[str] | None,
    sources: Sequence[str] | None,
    limit: int | None,
    offset: int,
    descending: bool | None,
) -> tuple[str, list[object]]:
    """IN/OUTを索引順に結合し、安全なbind parameter付きqueryを作る。

    Args:
        start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
        end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。
        signals: 対象信号名列。 INn/OUTn表記からsignal種別と番号を判定する。
        values: queryに含める論理値の列。 値は0または1で、 未指定なら両状態を含める。
        kinds: queryに含めるevent kindの集合。
        sources: queryに含める出力変更元の集合。
        limit: query結果の最大row数。
        offset: query結果の先頭から除外するrow数。
        descending: Trueなら新しい記録を先にする並び順指定。

    Returns:
        parameterized SQL文字列と、 そのplaceholderへ渡す順序付きbind値列。

    Raises:
        ValueError: 期間開始が終了未満でない場合。 「values must contain only 0 or
            1」に該当すると判定した場合。 「kinds must contain edge, snapshot, or
            output」に該当すると判定した場合。 「sources contains an unsupported output
            source」に該当すると判定した場合。
    """
    if start_ms >= end_ms:
        raise ValueError("start_ms must be before end_ms")
    input_channels, output_channels = _split_signals(signals)

    if values and any(
        isinstance(value, bool) or value not in (0, 1) for value in values
    ):
        raise ValueError("values must contain only 0 or 1")
    allowed_kinds = {"edge", "snapshot", "output"}
    if kinds and any(kind not in allowed_kinds for kind in kinds):
        raise ValueError("kinds must contain edge, snapshot, or output")
    allowed_sources = {"rule", "heartbeat", "test", "startup"}
    if sources and any(source not in allowed_sources for source in sources):
        raise ValueError("sources contains an unsupported output source")

    selected_kinds = set(kinds) if kinds else None
    selects: list[str] = []
    parameters: list[object] = []
    include_input = (
        (signals is None or bool(input_channels))
        and not sources
        and (selected_kinds is None or bool(selected_kinds & {"edge", "snapshot"}))
    )
    if include_input:
        input_clause, input_params = _channel_clause("channel_id", input_channels)
        if values:
            placeholders = ", ".join("?" for _ in values)
            input_clause += f" AND value IN ({placeholders})"
            input_params.extend(values)
        if selected_kinds is not None:
            selected_input_kinds = [
                int(kind == "snapshot")
                for kind in ("edge", "snapshot")
                if kind in selected_kinds
            ]
            placeholders = ", ".join("?" for _ in selected_input_kinds)
            input_clause += f" AND kind IN ({placeholders})"
            input_params.extend(selected_input_kinds)
        selects.append(
            """
            SELECT id, 'input' AS record_type, ts_utc_ms, mono_ns,
                   channel_id, value,
                   CASE kind WHEN 0 THEN 'edge' WHEN 1 THEN 'snapshot' END
                       AS kind,
                   NULL AS source, clock_synced
            FROM events
            WHERE ts_utc_ms >= ? AND ts_utc_ms < ?
            """
            + input_clause
        )
        parameters.extend([start_ms, end_ms, *input_params])
    include_output = (signals is None or bool(output_channels)) and (
        selected_kinds is None or "output" in selected_kinds
    )
    if include_output:
        output_clause, output_params = _channel_clause(
            "output_channel", output_channels
        )
        if values:
            placeholders = ", ".join("?" for _ in values)
            output_clause += f" AND value IN ({placeholders})"
            output_params.extend(values)
        if sources:
            placeholders = ", ".join("?" for _ in sources)
            output_clause += f" AND source IN ({placeholders})"
            output_params.extend(sources)
        selects.append(
            """
            SELECT id, 'output' AS record_type, ts_utc_ms, mono_ns,
                   output_channel AS channel_id, value,
                   'output' AS kind, source, clock_synced
            FROM output_events
            WHERE ts_utc_ms >= ? AND ts_utc_ms < ?
            """
            + output_clause
        )
        parameters.extend([start_ms, end_ms, *output_params])
    if not selects:
        return (
            "SELECT id, 'input' AS record_type, ts_utc_ms, mono_ns, channel_id, "
            "value, 'edge' AS kind, NULL AS source, clock_synced FROM events WHERE 0",
            [],
        )
    # UNION ALLを外側のSELECTで包むとSQLiteが全件を一時B-treeへ並べ替える。
    # filterを各表へ押し込み、compound SELECTを直接mergeすれば、通常pageは
    # 時刻indexからlimit分だけ読み、100万件全体のsortを避けられる。
    query = " UNION ALL ".join(selects)
    if descending is not None:
        order = "DESC" if descending else "ASC"
        query += f" ORDER BY ts_utc_ms {order}, record_type {order}, id {order}"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        parameters.extend([limit, offset])
    return query, parameters


def _split_signals(signals: Sequence[str] | None) -> tuple[list[int], list[int]]:
    """IN1/OUT1表記を検証し、入力・出力チャンネルへ分離する。

    Args:
        signals: 対象信号名列。 INn/OUTn表記からsignal種別と番号を判定する。

    Returns:
        注釈に示す順序の集計値tuple。

    Raises:
        ValueError: 「signals must contain IN1..IN6 or
            OUT1..OUT6」に該当すると判定した場合。 signal channelが1〜6の範囲外の場合。
            「signal channel must be numeric」に該当すると判定した場合。
    """
    if signals is None:
        return [], []
    input_channels: list[int] = []
    output_channels: list[int] = []
    for signal in signals:
        if not signal or signal[0] not in ("I", "O"):
            raise ValueError("signals must contain IN1..IN6 or OUT1..OUT6")
        if signal.startswith("IN"):
            channel_text = signal[2:]
            target = input_channels
        elif signal.startswith("OUT"):
            channel_text = signal[3:]
            target = output_channels
        else:
            raise ValueError("signals must contain IN1..IN6 or OUT1..OUT6")
        if not channel_text:
            raise ValueError("signals must contain IN1..IN6 or OUT1..OUT6")
        try:
            channel_id = int(channel_text)
        except ValueError as error:
            raise ValueError("signal channel must be numeric") from error
        if not 1 <= channel_id <= 6:
            raise ValueError("signal channel must be between 1 and 6")
        if channel_id not in target:
            target.append(channel_id)
    return input_channels, output_channels


def _channel_clause(column: str, channels: Sequence[int]) -> tuple[str, list[object]]:
    """チャンネル絞り込みSQLをbind parameterで作成する。

    Args:
        column: 内部queryで参照する固定column名。
        channels: 入力channel設定列。 daemonの比較ではchannel ID順に正規化される。

    Returns:
        parameterized SQL文字列と、 そのplaceholderへ渡す順序付きbind値列。
    """
    if not channels:
        return "", []
    placeholders = ", ".join("?" for _ in channels)
    return f" AND {column} IN ({placeholders})", list(channels)
