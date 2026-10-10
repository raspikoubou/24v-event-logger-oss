"""event_repository：SQLiteの永続化処理を機能単位で管理する。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from event_logger.common.models import (
    EventKind,
    InputEvent,
    InputState,
    OutputSource,
)
from event_logger.database.repository_helpers import (
    _event_from_row,
    _lastrowid,
    _validate_channel_ids,
)


@dataclass(frozen=True, slots=True)
class InputEventWrite:
    """DBへ追加する入力eventの値。"""

    session_id: int
    ts_utc_ms: int
    mono_ns: int
    channel_id: int
    value: int
    kind: EventKind
    clock_synced: bool


@dataclass(frozen=True, slots=True)
class OutputEventWrite:
    """DBへ追加する出力eventの値。"""

    session_id: int | None
    ts_utc_ms: int
    mono_ns: int
    output_channel: int
    value: int
    source: OutputSource
    clock_synced: bool


type EventWrite = InputEventWrite | OutputEventWrite


class EventBatchRepository:
    """入力・出力eventを同一のSQLite transactionで保存する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """transaction対象の接続を受け取る。接続のclose責務は呼び出し側にある。"""
        self._connection = connection

    def append_many(self, events: Sequence[EventWrite]) -> None:
        """FIFO順のevent群を一括保存し、成功時に一度だけcommitする。

        Args:
            events: FIFO順に並べた入力・出力event。

        Raises:
            ValueError: channelまたはlogical valueが範囲外の場合。
            sqlite3.Error: INSERTまたはCOMMITに失敗した場合。
        """
        if not events:
            return

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for event in events:
                if isinstance(event, InputEventWrite):
                    _insert_input_event(self._connection, event)
                elif isinstance(event, OutputEventWrite):
                    _insert_output_event(self._connection, event)
                else:
                    raise TypeError(f"unsupported event write: {type(event)!r}")
            self._connection.commit()
        except Exception:
            # 混在batchの途中で失敗しても、一部だけ永続化した状態にしない。
            self._connection.rollback()
            raise


def _insert_input_event(
    connection: sqlite3.Connection,
    event: InputEventWrite,
) -> None:
    """入力eventをtransaction内へ追加する。"""
    if event.value not in (0, 1):
        raise ValueError("value must be 0 or 1")
    if not 1 <= event.channel_id <= 6:
        raise ValueError("channel_id must be between 1 and 6")
    connection.execute(
        """
        INSERT INTO events (
            session_id, ts_utc_ms, mono_ns, channel_id, value, kind, clock_synced
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.session_id,
            event.ts_utc_ms,
            event.mono_ns,
            event.channel_id,
            event.value,
            int(event.kind),
            int(event.clock_synced),
        ),
    )


def _insert_output_event(
    connection: sqlite3.Connection,
    event: OutputEventWrite,
) -> None:
    """出力eventをtransaction内へ追加する。"""
    if event.value not in (0, 1):
        raise ValueError("value must be 0 or 1")
    if not 1 <= event.output_channel <= 6:
        raise ValueError("output_channel must be between 1 and 6")
    connection.execute(
        """
        INSERT INTO output_events (
            session_id, ts_utc_ms, mono_ns, output_channel, value, source,
            clock_synced
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.session_id,
            event.ts_utc_ms,
            event.mono_ns,
            event.output_channel,
            event.value,
            event.source.value,
            int(event.clock_synced),
        ),
    )


class EventRepository:
    """入力の一次eventを保存・検索する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。接続のclose責務は呼び出し側にある。"""
        self._connection = connection

    def append(
        self,
        *,
        session_id: int,
        ts_utc_ms: int,
        mono_ns: int,
        channel_id: int,
        value: int,
        kind: EventKind,
        clock_synced: bool,
    ) -> InputEvent:
        """入力eventを1件だけINSERT + COMMITする。"""
        if value not in (0, 1):
            raise ValueError("value must be 0 or 1")
        if not 1 <= channel_id <= 6:
            raise ValueError("channel_id must be between 1 and 6")
        cursor = self._connection.execute(
            """
            INSERT INTO events (
                session_id, ts_utc_ms, mono_ns, channel_id, value, kind, clock_synced
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                ts_utc_ms,
                mono_ns,
                channel_id,
                value,
                int(kind),
                int(clock_synced),
            ),
        )
        self._connection.commit()
        return InputEvent(
            id=_lastrowid(cursor),
            session_id=session_id,
            ts_utc_ms=ts_utc_ms,
            mono_ns=mono_ns,
            channel_id=channel_id,
            value=cast(InputState, value),
            kind=kind,
            clock_synced=clock_synced,
        )

    def list_between(
        self,
        *,
        start_ms: int,
        end_ms: int,
        channel_ids: Sequence[int] | None = None,
        kinds: Sequence[EventKind] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[InputEvent]:
        """期間 `[start_ms, end_ms)` のeventを古い順で返す。"""
        if start_ms >= end_ms:
            raise ValueError("start_ms must be before end_ms")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must not be negative")
        clauses = ["ts_utc_ms >= ?", "ts_utc_ms < ?"]
        parameters: list[int] = [start_ms, end_ms]
        if channel_ids:
            _validate_channel_ids(channel_ids)
            placeholders = ", ".join("?" for _ in channel_ids)
            clauses.append(f"channel_id IN ({placeholders})")
            parameters.extend(channel_ids)
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            parameters.extend(int(kind) for kind in kinds)
        query = (
            "SELECT * FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts_utc_ms, id"
        )
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters.extend([limit, offset])
        rows = self._connection.execute(query, parameters).fetchall()
        return [_event_from_row(row) for row in rows]

    def count_between(
        self,
        *,
        start_ms: int,
        end_ms: int,
        channel_id: int,
    ) -> int:
        """指定入力の期間内event数を行データを展開せずに数える。"""
        if start_ms >= end_ms:
            raise ValueError("start_ms must be before end_ms")
        _validate_channel_ids([channel_id])
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE channel_id = ? AND ts_utc_ms >= ? AND ts_utc_ms < ?
            """,
            (channel_id, start_ms, end_ms),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def count_between_limited(
        self,
        *,
        start_ms: int,
        end_ms: int,
        channel_id: int,
        limit: int,
    ) -> int:
        """期間内eventを上限件数までだけ数える。"""
        if start_ms >= end_ms:
            raise ValueError("start_ms must be before end_ms")
        if limit < 1:
            raise ValueError("limit must be positive")
        _validate_channel_ids([channel_id])
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT 1 FROM events
                WHERE channel_id = ? AND ts_utc_ms >= ? AND ts_utc_ms < ?
                LIMIT ?
            )
            """,
            (channel_id, start_ms, end_ms, limit),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def last_before(self, *, channel_id: int, before_ms: int) -> InputEvent | None:
        """指定時刻より前の最後のeventを返す。"""
        _validate_channel_ids([channel_id])
        row = self._connection.execute(
            """
            SELECT * FROM events
            WHERE channel_id = ? AND ts_utc_ms < ?
            ORDER BY ts_utc_ms DESC, id DESC
            LIMIT 1
            """,
            (channel_id, before_ms),
        ).fetchone()
        return _event_from_row(row) if row is not None else None

    def last_edge_before(
        self,
        *,
        channel_id: int,
        value: int,
        before_ms: int,
    ) -> InputEvent | None:
        """指定時刻以前で、event IDが最大のedgeを返す。"""
        _validate_channel_ids([channel_id])
        if value not in (0, 1):
            raise ValueError("value must be 0 or 1")
        # 古い履歴をID indexだけで後ろ向きに探すと、期間終端より未来の
        # 大量eventを一件ずつ除外する。channel内の時刻範囲の近い側から
        # 探すindexを選ぶ。どちらのqueryも最後はID降順なので、NTP逆行時の
        # 「境界を満たす中で最後に記録されたedge」という意味は変わらない。
        bounds = self._connection.execute(
            """
            SELECT
                (SELECT ts_utc_ms FROM events INDEXED BY idx_events_channel_ts
                 WHERE channel_id = ? ORDER BY ts_utc_ms ASC LIMIT 1),
                (SELECT ts_utc_ms FROM events INDEXED BY idx_events_channel_ts
                 WHERE channel_id = ? ORDER BY ts_utc_ms DESC LIMIT 1)
            """,
            (channel_id, channel_id),
        ).fetchone()
        use_timestamp_index = (
            bounds is not None
            and bounds[0] is not None
            and bounds[1] is not None
            and before_ms - int(bounds[0]) < int(bounds[1]) - before_ms
        )
        if use_timestamp_index:
            # 時刻境界の前にあるeventだけを探す。`SELECT *`で候補行を直接読むと、
            # 冷えたDBでは候補ごとにevent本体pageをrandom readしながらID順に
            # sortするため、1件返すだけでも全履歴に近いI/Oが起きる。値・種別・
            # 時刻を含むindexからIDだけをcovering readし、最後の1行だけ主キーで読む。
            # 時計が逆行して同時刻順とevent ID順が異なる場合も最大IDを保つ。
            id_row = self._connection.execute(
                """
                SELECT id FROM events INDEXED BY idx_events_edge_count
                WHERE channel_id = ? AND value = ? AND kind = ? AND ts_utc_ms < ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (channel_id, value, int(EventKind.EDGE), before_ms),
            ).fetchone()
            row = (
                self._connection.execute(
                    "SELECT * FROM events WHERE id = ?", (int(id_row[0]),)
                ).fetchone()
                if id_row is not None
                else None
            )
        else:
            row = self._connection.execute(
                """
                SELECT * FROM events INDEXED BY idx_events_counter
                WHERE channel_id = ? AND value = ? AND kind = ? AND ts_utc_ms < ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (channel_id, value, int(EventKind.EDGE), before_ms),
            ).fetchone()
        return _event_from_row(row) if row is not None else None

    def list_after_id(
        self,
        *,
        after_event_id: int,
        channel_ids: Sequence[int] | None = None,
    ) -> list[InputEvent]:
        """event ID基準でeventを古い順に返す。"""
        if after_event_id < 0:
            raise ValueError("after_event_id must not be negative")
        # reset後カウントの境界はevent IDが正本であり、wall clockの補正で
        # 件数が減らないよう時刻条件を混ぜない。
        clauses = ["id > ?"]
        parameters: list[int] = [after_event_id]
        if channel_ids:
            _validate_channel_ids(channel_ids)
            placeholders = ", ".join("?" for _ in channel_ids)
            clauses.append(f"channel_id IN ({placeholders})")
            parameters.extend(channel_ids)
        rows = self._connection.execute(
            "SELECT * FROM events WHERE " + " AND ".join(clauses) + " ORDER BY id",
            parameters,
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_for_session(
        self,
        *,
        session_id: int,
        channel_ids: Sequence[int] | None = None,
    ) -> list[InputEvent]:
        """session境界を正として入力eventをevent ID順で返す。"""
        if session_id < 1:
            raise ValueError("session_id must be positive")
        clauses = ["session_id = ?"]
        parameters: list[object] = [session_id]
        if channel_ids:
            _validate_channel_ids(channel_ids)
            placeholders = ", ".join("?" for _ in channel_ids)
            clauses.append(f"channel_id IN ({placeholders})")
            parameters.extend(channel_ids)
        rows = self._connection.execute(
            "SELECT * FROM events WHERE " + " AND ".join(clauses) + " ORDER BY id",
            parameters,
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def last_for_session_before(
        self,
        *,
        session_id: int,
        channel_id: int,
        before_ms: int,
    ) -> InputEvent | None:
        """session内で時刻境界を満たす最後のeventをID順で返す。

        NTP補正でwall clockが逆行しても発生順を保ちつつ、active session全件を
        Web応答のために読み出さない。時刻境界は未来時刻のeventを現在状態へ
        誤適用しないための限定条件である。
        """
        if session_id < 1:
            raise ValueError("session_id must be positive")
        _validate_channel_ids([channel_id])
        # session_id indexに走査範囲を限定する。timestamp indexを使うと
        # 別sessionの長期履歴まで読み、ID順sortが発生する。
        row = self._connection.execute(
            """
            SELECT * FROM events INDEXED BY idx_events_session_id
            WHERE session_id = ? AND channel_id = ? AND ts_utc_ms < ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id, channel_id, before_ms),
        ).fetchone()
        return _event_from_row(row) if row is not None else None

    def max_id(self) -> int:
        """現在保存されているeventの最大IDを返す。"""
        row = self._connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM events"
        ).fetchone()
        return int(row[0])

    def count_all(self) -> int:
        """保存されている入力event件数を返す。"""
        row = self._connection.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0])

    def count_unsynchronized(self) -> int:
        """時計未同期時に保存された入力event件数を返す。"""
        row = self._connection.execute(
            "SELECT COUNT(*) FROM events WHERE clock_synced = 0"
        ).fetchone()
        return int(row[0])

    def count_edges(
        self,
        *,
        channel_id: int,
        value: int,
        start_ms: int | None = None,
        end_ms: int | None = None,
        after_event_id: int | None = None,
    ) -> int:
        """指定条件のedge件数をSQL側で数える。"""
        _validate_channel_ids([channel_id])
        if value not in (0, 1):
            raise ValueError("value must be 0 or 1")
        clauses = ["channel_id = ?", "value = ?", "kind = ?"]
        parameters: list[int] = [channel_id, value, int(EventKind.EDGE)]
        if start_ms is not None:
            clauses.append("ts_utc_ms >= ?")
            parameters.append(start_ms)
        if end_ms is not None:
            clauses.append("ts_utc_ms < ?")
            parameters.append(end_ms)
        if after_event_id is not None:
            if after_event_id < 0:
                raise ValueError("after_event_id must not be negative")
            clauses.append("id > ?")
            parameters.append(after_event_id)
        row = self._connection.execute(
            "SELECT COUNT(*) FROM events WHERE " + " AND ".join(clauses),
            parameters,
        ).fetchone()
        return int(row[0])


