"""log_repository：SQLiteの永続化処理を機能単位で管理する。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from typing import cast

from event_logger.common.models import (
    InputState,
    LogRecord,
    OutputEvent,
    OutputSource,
)
from event_logger.database.log_query import _build_log_query
from event_logger.database.repository_helpers import (
    _lastrowid,
    _log_record_from_row,
    _output_event_from_row,
    _validate_channel_ids,
)


class OutputEventRepository:
    """OUT状態変化の履歴を保存・検索する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。接続のclose責務は呼び出し側にある。

        Args:
            connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._connection = connection

    def append(
        self,
        *,
        session_id: int | None,
        ts_utc_ms: int,
        mono_ns: int,
        output_channel: int,
        value: int,
        source: OutputSource,
        clock_synced: bool,
    ) -> OutputEvent:
        """OUT状態変化を1件だけINSERT + COMMITする。

        Args:
            session_id: 対象sessionのDB ID。 session外eventが混ざらないようqueryで絞る。
            ts_utc_ms: 保存時のUTC Unix epoch timestamp（ms）。
                画面表示/検索の時間軸に使う。
            mono_ns: event発生時のmonotonic timestamp（ns）。
                session内経過時間の基準にする。
            output_channel: 対象OUT番号（1〜6）。
                値は論理ON/OFFとして扱い極性を含まない。
            value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。
            source: OUTを変更したlogger内のOutputSource。
            clock_synced: event記録時のsystem clock同期状態。

        Returns:
            OutputEvent型の結果。 戻り値の型に応じ、
                該当なしはNoneまたは空collectionとなる。

        Raises:
            ValueError: output_channelが1〜6の範囲外の場合。
                論理値が0または1以外の場合。
        """
        if not 1 <= output_channel <= 6:
            raise ValueError("output_channel must be between 1 and 6")
        if value not in (0, 1):
            raise ValueError("value must be 0 or 1")
        cursor = self._connection.execute(
            """
            INSERT INTO output_events (
                session_id, ts_utc_ms, mono_ns, output_channel, value, source,
                clock_synced
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                ts_utc_ms,
                mono_ns,
                output_channel,
                value,
                source.value,
                int(clock_synced),
            ),
        )
        self._connection.commit()
        return OutputEvent(
            id=_lastrowid(cursor),
            session_id=session_id,
            ts_utc_ms=ts_utc_ms,
            mono_ns=mono_ns,
            output_channel=output_channel,
            value=cast(InputState, value),
            source=source,
            clock_synced=clock_synced,
        )

    def count_all(self) -> int:
        """保存されているOUT event件数を返す。

        Args:
            なし。

        Returns:
            条件に一致するevent/row件数。 該当がない場合は0。
        """
        row = self._connection.execute("SELECT COUNT(*) FROM output_events").fetchone()
        return int(row[0])

    def count_unsynchronized(self) -> int:
        """時計未同期時に保存されたOUT event件数を返す。

        Args:
            なし。

        Returns:
            条件に一致するevent/row件数。 該当がない場合は0。
        """
        row = self._connection.execute(
            "SELECT COUNT(*) FROM output_events WHERE clock_synced = 0"
        ).fetchone()
        return int(row[0])

    def list_between(
        self,
        *,
        start_ms: int,
        end_ms: int,
        output_channels: Sequence[int] | None = None,
        limit: int | None = None,
        descending: bool = False,
    ) -> list[OutputEvent]:
        """期間内のOUT eventを返す。既定は時刻・IDの昇順。

        Args:
            start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
            end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。
            output_channels: query対象とするOUT番号列。
            limit: query結果の最大row数。
            descending: Trueなら新しい記録を先にする並び順指定。

        Returns:
            期間とOUT番号に一致するOutputEvent列。 queryの時刻/ID順を保ち、
                該当なしは空list。

        Raises:
            ValueError: 期間開始が終了未満でない場合。 limitが0以下の場合。
        """
        if start_ms >= end_ms:
            raise ValueError("start_ms must be before end_ms")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        clauses = ["ts_utc_ms >= ?", "ts_utc_ms < ?"]
        parameters: list[object] = [start_ms, end_ms]
        if output_channels:
            _validate_channel_ids(output_channels)
            placeholders = ", ".join("?" for _ in output_channels)
            clauses.append(f"output_channel IN ({placeholders})")
            parameters.extend(output_channels)
        order = "DESC" if descending else "ASC"
        query = (
            "SELECT * FROM output_events WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY ts_utc_ms {order}, id {order}"
        )
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        rows = self._connection.execute(query, parameters).fetchall()
        return [_output_event_from_row(row) for row in rows]

    def count_between(
        self,
        *,
        start_ms: int,
        end_ms: int,
        output_channel: int,
    ) -> int:
        """指定出力の期間内event数を行データを展開せずに数える。

        Args:
            start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
            end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。
            output_channel: 対象OUT番号（1〜6）。
                値は論理ON/OFFとして扱い極性を含まない。

        Returns:
            条件に一致するevent/row件数。 該当がない場合は0。

        Raises:
            ValueError: 期間開始が終了未満でない場合。
        """
        if start_ms >= end_ms:
            raise ValueError("start_ms must be before end_ms")
        _validate_channel_ids([output_channel])
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM output_events
            WHERE output_channel = ? AND ts_utc_ms >= ? AND ts_utc_ms < ?
            """,
            (output_channel, start_ms, end_ms),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def count_between_limited(
        self,
        *,
        start_ms: int,
        end_ms: int,
        output_channel: int,
        limit: int,
    ) -> int:
        """期間内OUT eventを上限件数までだけ数える。

        Args:
            start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
            end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。
            output_channel: 対象OUT番号（1〜6）。
                値は論理ON/OFFとして扱い極性を含まない。
            limit: query結果の最大row数。

        Returns:
            条件に一致するevent/row件数。 該当がない場合は0。

        Raises:
            ValueError: 期間開始が終了未満でない場合。 limitが0以下の場合。
        """
        if start_ms >= end_ms:
            raise ValueError("start_ms must be before end_ms")
        if limit < 1:
            raise ValueError("limit must be positive")
        _validate_channel_ids([output_channel])
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT 1 FROM output_events
                WHERE output_channel = ? AND ts_utc_ms >= ? AND ts_utc_ms < ?
                LIMIT ?
            )
            """,
            (output_channel, start_ms, end_ms, limit),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def last_before(
        self,
        *,
        output_channel: int,
        before_ms: int,
    ) -> OutputEvent | None:
        """指定時刻より前のOUT状態変更を1件返す。

        Args:
            output_channel: 対象OUT番号（1〜6）。
                値は論理ON/OFFとして扱い極性を含まない。
            before_ms: 指定時刻より前のeventを検索するUTC epoch ms境界。

        Returns:
            条件に一致する最新OutputEvent。 該当eventがなければNone。
        """
        _validate_channel_ids([output_channel])
        row = self._connection.execute(
            """
            SELECT * FROM output_events
            WHERE output_channel = ? AND ts_utc_ms < ?
            ORDER BY ts_utc_ms DESC, id DESC
            LIMIT 1
            """,
            (output_channel, before_ms),
        ).fetchone()
        return _output_event_from_row(row) if row is not None else None

    def list_for_session(
        self,
        *,
        session_id: int,
        output_channels: Sequence[int] | None = None,
    ) -> list[OutputEvent]:
        """session境界を正としてOUT eventをevent ID順で返す。

        Args:
            session_id: 対象sessionのDB ID。 session外eventが混ざらないようqueryで絞る。
            output_channels: query対象とするOUT番号列。

        Returns:
            query条件に一致するOUT event。 該当なしは空list。

        Raises:
            ValueError: session_idが0以下の場合。
        """
        if session_id < 1:
            raise ValueError("session_id must be positive")
        clauses = ["session_id = ?"]
        parameters: list[object] = [session_id]
        if output_channels:
            _validate_channel_ids(output_channels)
            placeholders = ", ".join("?" for _ in output_channels)
            clauses.append(f"output_channel IN ({placeholders})")
            parameters.extend(output_channels)
        rows = self._connection.execute(
            "SELECT * FROM output_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id",
            parameters,
        ).fetchall()
        return [_output_event_from_row(row) for row in rows]


class LogRepository:
    """IN/OUTを同じ時間軸で検索する読み取り専用Repository。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。書込みは各一次Repositoryへ閉じ込める。

        Args:
            connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._connection = connection

    def list_between(
        self,
        *,
        start_ms: int,
        end_ms: int,
        signals: Sequence[str] | None = None,
        values: Sequence[int] | None = None,
        kinds: Sequence[str] | None = None,
        sources: Sequence[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[LogRecord]:
        """期間内のIN/OUTを新しい順にページングして返す。

        Args:
            start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
            end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。
            signals: 対象信号名列。 INn/OUTn表記からsignal種別と番号を判定する。
            values: queryに含める論理値の列。 値は0または1で、
                未指定なら両状態を含める。
            kinds: queryに含めるevent kindの集合。
            sources: queryに含める出力変更元の集合。
            limit: query結果の最大row数。
            offset: query結果の先頭から除外するrow数。

        Returns:
            期間とfilterに一致するLogRecord列。 ts_utc_ms降順で、
                同一時刻はqueryのtype/ID順。 limit/offset適用後の値で、
                該当なしは空list。

        Raises:
            ValueError: limitが0以下の場合。 offsetが負の場合。
        """
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must not be negative")
        query, parameters = _build_log_query(
            start_ms=start_ms,
            end_ms=end_ms,
            signals=signals,
            values=values,
            kinds=kinds,
            sources=sources,
            limit=limit,
            offset=offset,
            descending=True,
        )
        rows = self._connection.execute(query, parameters).fetchall()
        return [_log_record_from_row(row) for row in rows]

    def count_between(
        self,
        *,
        start_ms: int,
        end_ms: int,
        signals: Sequence[str] | None = None,
        values: Sequence[int] | None = None,
        kinds: Sequence[str] | None = None,
        sources: Sequence[str] | None = None,
    ) -> int:
        """同じIN/OUT filterに一致するevent数を返す。

        Args:
            start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
            end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。
            signals: 対象信号名列。 INn/OUTn表記からsignal種別と番号を判定する。
            values: queryに含める論理値の列。 値は0または1で、
                未指定なら両状態を含める。
            kinds: queryに含めるevent kindの集合。
            sources: queryに含める出力変更元の集合。

        Returns:
            条件に一致するevent/row件数。 該当がない場合は0。
        """
        query, parameters = _build_log_query(
            start_ms=start_ms,
            end_ms=end_ms,
            signals=signals,
            values=values,
            kinds=kinds,
            sources=sources,
            limit=None,
            offset=0,
            descending=None,
        )
        row = self._connection.execute(
            f"SELECT COUNT(*) FROM ({query})", parameters
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def iter_between(
        self,
        *,
        start_ms: int,
        end_ms: int,
        signals: Sequence[str] | None = None,
        values: Sequence[int] | None = None,
        kinds: Sequence[str] | None = None,
        sources: Sequence[str] | None = None,
    ) -> Iterator[LogRecord]:
        """期間内のIN/OUTを全件、DB cursorから順次返す。

        Args:
            start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
            end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。
            signals: 対象信号名列。 INn/OUTn表記からsignal種別と番号を判定する。
            values: queryに含める論理値の列。 値は0または1で、
                未指定なら両状態を含める。
            kinds: queryに含めるevent kindの集合。
            sources: queryに含める出力変更元の集合。

        Yields:
            LogRecordを1件ずつyieldする。 ts_utc_ms昇順、
                同一timestampではrecord_type昇順・数値ID昇順。 対象期間はstart_ms以上、
                end_ms未満。
        """
        query, parameters = _build_log_query(
            start_ms=start_ms,
            end_ms=end_ms,
            signals=signals,
            values=values,
            kinds=kinds,
            sources=sources,
            limit=None,
            offset=0,
            descending=False,
        )
        cursor = self._connection.execute(query, parameters)
        for row in cursor:
            yield _log_record_from_row(row)
