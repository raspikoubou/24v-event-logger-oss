"""カウンター基準と保存期限管理のSQLite Repository。"""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from collections.abc import Sequence

from event_logger.common.models import CounterReset, EventKind
from event_logger.database.repository_helpers import (
    _counter_config,
    _counter_event_matches,
    _counter_reset_from_row,
    _delete_ids,
    _lastrowid,
    _validate_channel_ids,
)


class CounterResetRepository:
    """ウィジェット別counter resetの基準を保存・取得する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。

        Args:
            connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._connection = connection

    def create(
        self,
        *,
        widget_id: str,
        reset_at_ms: int,
        after_event_id: int | None = None,
    ) -> CounterReset:
        """最新event IDを基準にcounter resetをcommitする。

        Args:
            widget_id: 対象widgetの永続ID。 関連データと監査記録のkeyに使う。
            reset_at_ms: counter reset記録の時刻（UTC epoch ms）。
            after_event_id: このIDより大きいeventだけを数える排他的reset境界。

        Returns:
            CounterReset型の結果。 戻り値の型に応じ、
                該当なしはNoneまたは空collectionとなる。

        Raises:
            ValueError: widget_idが空の場合。 after_event_idが負の場合。
        """
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
            # reset境界と監査行を同時に保存し、両者が食い違う記録を残さない。
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
        """ウィジェットの最新resetを返す。

        Args:
            widget_id: 対象widgetの永続ID。 関連データと監査記録のkeyに使う。

        Returns:
            widgetの最新CounterReset。 未登録ならNone。
        """
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

    def count_total(self, *, reset_id: int, channel_id: int, value: int) -> int:
        """同じ読取りsnapshotでcarryと残存edgeを合算する。

        Args:
            reset_id: counter reset rowの永続ID。
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
            value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。

        Returns:
            条件に一致するevent/row件数。 該当がない場合は0。

        Raises:
            ValueError: 「reset id or edge value is invalid」に該当すると判定した場合。
        """
        _validate_channel_ids([channel_id])
        if reset_id < 1 or value not in (0, 1):
            raise ValueError("reset id or edge value is invalid")
        # 別々のSELECTでは、途中のretention commitにより削除前のcarryと
        # 削除後の残存数を足してしまう。単一SQLにして表示の一時的な減少を防ぐ。
        # reset IDは要求が取得した基準に固定し、並行する手動resetとも混ぜない。
        row = self._connection.execute(
            """
            SELECT carried_count + (
                SELECT COUNT(*) FROM events
                WHERE channel_id = ? AND value = ? AND kind = ?
                  AND id > counter_resets.after_event_id
            ) AS total_count
            FROM counter_resets WHERE id = ?
            """,
            (channel_id, value, int(EventKind.EDGE), reset_id),
        ).fetchone()
        return int(row[0]) if row is not None else 0


class RetentionRepository:
    """retentionのcarry + DELETE + vacuumをtransaction境界へ閉じ込める。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。

        Args:
            connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._connection = connection

    def delete_old_batch(
        self,
        *,
        cutoff_ms: int | None,
        batch_size: int,
    ) -> tuple[int, int, int] | None:
        """古いIN/OUTを最大batch件ずつ削除する。

        Args:
            cutoff_ms: retentionで削除対象とする時刻のUTC epoch ms境界。
            batch_size: 一回のretention deleteで処理する最大row件数。

        Returns:
            削除対象なしならNone。 それ以外は(IN削除row数, OUT削除row数,
                counterへ繰越加算したedge合計数)。 batch_sizeはIN/OUT各々の上限。
                cutoff_ms=Noneなら時刻制限なし、 指定時はts_utc_ms <
                cutoff_msを古い順に削除する。

        Raises:
            ValueError: batch_sizeが0以下の場合。
        """
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        # 対象選択からcarry加算・削除までを同一writer transactionに閉じ込める。
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            # 期限なしなら最古rowを取り、期限指定時は境界より古いrowだけに限定する。
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
            # 削除で失われるreset後edge数を先にcarryへ移す。
            # 履歴を消した後もcounter totalを保つため。
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
            # carry更新と両履歴の削除がそろってからcommitし、途中失敗時は全体を戻す。
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

        容量保護はDB本体だけでなくWAL/SHMも監視するため、 vacuumだけでは
        観測対象のファイルサイズが下がらない。 checkpointを同じ保守境界で
        行い、 loggerdが短時間書込みを止めている間にWALを縮小する。

        Args:
            なし。

        Returns:
            判定対象が条件を満たす場合True、 その他はFalse。

        Raises:
            RuntimeError: 「incremental auto_vacuum is not enabled; run database
                migration」に該当すると判定した場合。
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
        """maintenance結果をaudit_logへ保存する。

        Args:
            action: 監査記録に保存する操作名。
            details_json: 監査記録へ保存するdetails JSON文字列。
            ts_utc_ms: 保存時のUTC Unix epoch timestamp（ms）。
                画面表示/検索の時間軸に使う。

        Returns:
            None。 渡されたSQLite接続へ変更を適用し、 接続のcloseは呼出元に残す。
        """
        self._connection.execute(
            """
            INSERT INTO audit_log (ts_utc_ms, action, details_json)
            VALUES (?, ?, ?)
            """,
            (ts_utc_ms, action, details_json),
        )
        self._connection.commit()

    def _counter_carry(self, event_rows: Sequence[sqlite3.Row]) -> dict[int, int]:
        """削除対象eventを各widgetの条件へ照合してcarry件数を求める。

        Args:
            event_rows: retention対象となる入力event row列。
                counter繰越値の算出にも使う。

        Returns:
            結果値を識別keyへ対応させたdict。 入力mappingを変更しない。
        """
        # resetごとの設定を読み、同じ条件のresetは削除batch内で一度だけ照合する。
        rows = self._connection.execute(
            """
            SELECT counter_resets.*, widgets.config_json
            FROM counter_resets
            JOIN widgets ON widgets.id = counter_resets.widget_id
            """
        ).fetchall()
        result: dict[int, int] = {}
        matching_ids: dict[tuple[int, str], list[int]] = {}
        for reset_row in rows:
            config = _counter_config(reset_row["config_json"])
            if config is None:
                continue
            channel_id = config.get("channel_id")
            edge = config.get("edge", "rising")
            if (
                isinstance(channel_id, bool)
                or not isinstance(channel_id, int)
                or not 1 <= channel_id <= 6
                or not isinstance(edge, str)
                or edge not in {"rising", "falling", "both", "any"}
            ):
                continue
            key = (channel_id, "both" if edge == "any" else edge)
            if key not in matching_ids:
                # timestamp順に削除するbatchでも、reset基準はevent ID順である。
                # 同じ信号条件の照合を一度だけ行い、全reset履歴を二分探索する。
                # 履歴数×batch件数の総当たりでwriterの5秒待ちを使い切らない。
                matching_ids[key] = sorted(
                    int(event_row["id"])
                    for event_row in event_rows
                    if _counter_event_matches(event_row, config)
                )
            # 削除対象IDのうちreset境界より後にある分だけを、二分探索でcarryへ数える。
            after_event_id = int(reset_row["after_event_id"])
            ids = matching_ids[key]
            count = len(ids) - bisect_right(ids, after_event_id)
            if count:
                result[int(reset_row["id"])] = count
        return result
