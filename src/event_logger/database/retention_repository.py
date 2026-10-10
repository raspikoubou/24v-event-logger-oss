"""カウンター基準と保存期限管理のSQLite Repository。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from event_logger.common.models import CounterReset
from event_logger.database.repository_helpers import (
    _counter_config,
    _counter_event_matches,
    _counter_reset_from_row,
    _delete_ids,
    _lastrowid,
)


class CounterResetRepository:
    """ウィジェット別counter resetの基準を保存・取得する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。"""
        self._connection = connection

    def create(
        self,
        *,
        widget_id: str,
        reset_at_ms: int,
        after_event_id: int | None = None,
    ) -> CounterReset:
        """最新event IDを基準にcounter resetをcommitする。"""
        if not widget_id:
            raise ValueError("widget_id must not be empty")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            # MAX(id)の取得をtransaction開始後に行う。先にSELECTすると、
            # loggerdがその直後に保存したeventをreset後として数えてしまう。
            if after_event_id is None:
                row = self._connection.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM events"
                ).fetchone()
                after_event_id = int(row[0])
            if after_event_id < 0:
                raise ValueError("after_event_id must not be negative")
            cursor = self._connection.execute(
                """
                INSERT INTO counter_resets (
                    widget_id, reset_at_ms, after_event_id, carried_count
                ) VALUES (?, ?, ?, 0)
                """,
                (widget_id, reset_at_ms, after_event_id),
            )
            self._connection.execute(
                """
                INSERT INTO audit_log (ts_utc_ms, action, details_json)
                VALUES (?, ?, ?)
                """,
                (
                    reset_at_ms,
                    "counter_reset",
                    json.dumps({"widget_id": widget_id}, separators=(",", ":")),
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return CounterReset(
            id=_lastrowid(cursor),
            widget_id=widget_id,
            reset_at_ms=reset_at_ms,
            after_event_id=after_event_id,
            carried_count=0,
        )

    def latest(self, widget_id: str) -> CounterReset | None:
        """ウィジェットの最新resetを返す。"""
        row = self._connection.execute(
            """
            SELECT * FROM counter_resets
            WHERE widget_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (widget_id,),
        ).fetchone()
        return _counter_reset_from_row(row) if row is not None else None


class RetentionRepository:
    """retentionのcarry + DELETE + vacuumをtransaction境界へ閉じ込める。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。"""
        self._connection = connection

    def delete_old_batch(
        self,
        *,
        cutoff_ms: int | None,
        batch_size: int,
    ) -> tuple[int, int, int] | None:
        """古いIN/OUTを最大batch件ずつ削除する。"""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if cutoff_ms is None:
                event_rows = self._connection.execute(
                    "SELECT * FROM events ORDER BY ts_utc_ms, id LIMIT ?",
                    (batch_size,),
                ).fetchall()
                output_rows = self._connection.execute(
                    "SELECT id FROM output_events ORDER BY ts_utc_ms, id LIMIT ?",
                    (batch_size,),
                ).fetchall()
            else:
                event_rows = self._connection.execute(
                    """
                    SELECT * FROM events
                    WHERE ts_utc_ms < ?
                    ORDER BY ts_utc_ms, id
                    LIMIT ?
                    """,
                    (cutoff_ms, batch_size),
                ).fetchall()
                output_rows = self._connection.execute(
                    """
                    SELECT id FROM output_events
                    WHERE ts_utc_ms < ?
                    ORDER BY ts_utc_ms, id
                    LIMIT ?
                    """,
                    (cutoff_ms, batch_size),
                ).fetchall()
            if not event_rows and not output_rows:
                self._connection.commit()
                return None
            carried_by_reset = self._counter_carry(event_rows)
            for reset_id, count in carried_by_reset.items():
                self._connection.execute(
                    """
                    UPDATE counter_resets
                    SET carried_count = carried_count + ?
                    WHERE id = ?
                    """,
                    (count, reset_id),
                )
            _delete_ids(self._connection, "events", event_rows)
            _delete_ids(self._connection, "output_events", output_rows)
            self._connection.commit()
            return (
                len(event_rows),
                len(output_rows),
                sum(carried_by_reset.values()),
            )
        except Exception:
            self._connection.rollback()
            raise

    def incremental_vacuum(self) -> bool:
        """空きpageとWALを可能な範囲で段階的に返す。

        容量保護はDB本体だけでなくWAL/SHMも監視するため、vacuumだけでは
        観測対象のファイルサイズが下がらない。checkpointを同じ保守境界で
        行い、loggerdが短時間書込みを止めている間にWALを縮小する。
        """
        mode_row = self._connection.execute("PRAGMA auto_vacuum").fetchone()
        mode = int(mode_row[0]) if mode_row is not None else 0
        if mode != 2:
            # NONEのDBへincremental_vacuumを繰り返すとfreelist_countが減らず、
            # 容量保護が全eventを削除するまでループしてしまう。旧DBは明示
            # migrationでVACUUMしてからretentionを実行する。
            raise RuntimeError(
                "incremental auto_vacuum is not enabled; run database migration"
            )
        # 引数なしのincremental_vacuumはSQLite仕様上1 pageだけを回収する。
        # 容量保護では削除したbatch全体を回収できないと、freelistが残った
        # まま次のDELETEへ進み、必要以上に一次eventを削除する危険がある。
        # 1000 page単位でfreelist_countが0になるまで明示的に進める。
        while True:
            row = self._connection.execute("PRAGMA freelist_count").fetchone()
            free_pages = int(row[0]) if row is not None else 0
            if free_pages <= 0:
                break
            self._connection.execute("PRAGMA incremental_vacuum(1000)")
            # SQLiteは単一writerなので、各ページchunk後にcommitしてloggerdへ
            # write lockを返す。retention全体を一つのvacuum transactionで
            # 包むと、入力event writerがbusy timeoutを越えて停止し得る。
            self._connection.commit()
        try:
            row = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error:
            # loggerdの読取りが残っている場合などはcheckpointできない。
            # 次回maintenanceで再試行する。容量保護側は、この結果を受けて
            # 追加の一次event削除を止め、retentionで全eventを失わない。
            self._connection.rollback()
            return False
        if row is None or int(row[0]) != 0:
            # PRAGMAの先頭列はbusyフラグであり、1のときreader等がWALを
            # 縮小できない。freelistだけ回収しても監視対象bytesは減らない。
            return False
        self._connection.commit()
        return True

    def record_audit(self, *, action: str, details_json: str, ts_utc_ms: int) -> None:
        """maintenance結果をaudit_logへ保存する。"""
        self._connection.execute(
            """
            INSERT INTO audit_log (ts_utc_ms, action, details_json)
            VALUES (?, ?, ?)
            """,
            (ts_utc_ms, action, details_json),
        )
        self._connection.commit()

    def _counter_carry(self, event_rows: Sequence[sqlite3.Row]) -> dict[int, int]:
        """削除対象eventを各widgetの条件へ照合してcarry件数を求める。"""
        rows = self._connection.execute(
            """
            SELECT counter_resets.*, widgets.config_json
            FROM counter_resets
            JOIN widgets ON widgets.id = counter_resets.widget_id
            """
        ).fetchall()
        result: dict[int, int] = {}
        for reset_row in rows:
            config = _counter_config(reset_row["config_json"])
            if config is None:
                continue
            after_event_id = int(reset_row["after_event_id"])
            count = sum(
                1
                for event_row in event_rows
                if int(event_row["id"]) > after_event_id
                and _counter_event_matches(event_row, config)
            )
            if count:
                result[int(reset_row["id"])] = count
        return result


