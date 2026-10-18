"""channel_repository：SQLiteの永続化処理を機能単位で管理する。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence

from event_logger.common.models import ChannelConfig
from event_logger.database.repository_helpers import (
    _channel_from_row,
    _mapping_int,
    _mapping_text,
    _validate_channel_ids,
    _validate_channel_text,
)


class ChannelRepository:
    """入力チャンネル設定を読み書きする。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。接続のclose責務は呼び出し側にある。

        Args:
            connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._connection = connection

    def list_all(self) -> list[ChannelConfig]:
        """IN1〜IN6をチャンネル番号順に返す。

        Args:
            なし。

        Returns:
            channel ID順の設定値列。
        """
        rows = self._connection.execute("SELECT * FROM channels ORDER BY id").fetchall()
        return [_channel_from_row(row) for row in rows]

    def get(self, channel_id: int) -> ChannelConfig | None:
        """指定入力チャンネルを取得する。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。

        Returns:
            指定channelのChannelConfig。 未登録ならNone。
        """
        _validate_channel_ids([channel_id])
        row = self._connection.execute(
            "SELECT * FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()
        return _channel_from_row(row) if row is not None else None

    def update(
        self,
        *,
        channel_id: int,
        enabled: bool,
        name: str,
        debounce_ms: int,
        on_label: str,
        off_label: str,
        updated_at_ms: int,
        audit_action: str = "channel_updated",
    ) -> ChannelConfig:
        """入力設定とaudit_logを同一transactionで保存する。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
            enabled: channel/rule/widgetを有効にするbool値。
            name: channel/rule/widgetの表示名。
            debounce_ms: 論理入力を確定するまで連続安定が必要な時間（ms）。
            on_label: 入力ON時に画面表示するuser label。
            off_label: 入力OFF時に画面表示するuser label。
            updated_at_ms: 設定の更新時刻（UTC epoch ms）。
            audit_action: 設定変更と合わせて監査記録へ保存するaction名。

        Returns:
            ChannelConfig型の結果。 戻り値の型に応じ、
                該当なしはNoneまたは空collectionとなる。

        Raises:
            ValueError: debounce_msが0〜5000の範囲外の場合。
            RuntimeError: 「updated channel disappeared」に該当すると判定した場合。
        """
        _validate_channel_ids([channel_id])
        _validate_channel_text(name, "name")
        _validate_channel_text(on_label, "on_label")
        _validate_channel_text(off_label, "off_label")
        if not 0 <= debounce_ms <= 5000:
            raise ValueError("debounce_ms must be between 0 and 5000")
        # channel更新とauditを同じtransactionに含め、設定変更だけが残る状態を防ぐ。
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                """
                UPDATE channels
                SET enabled = ?, name = ?, debounce_ms = ?, on_label = ?,
                    off_label = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    int(enabled),
                    name,
                    debounce_ms,
                    on_label,
                    off_label,
                    updated_at_ms,
                    channel_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"channel not found: {channel_id}")
            self._connection.execute(
                """
                INSERT INTO audit_log (ts_utc_ms, action, details_json)
                VALUES (?, ?, ?)
                """,
                (
                    updated_at_ms,
                    audit_action,
                    json.dumps({"channel_id": channel_id}, separators=(",", ":")),
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        updated = self.get(channel_id)
        if updated is None:
            raise RuntimeError("updated channel disappeared")
        return updated

    def update_many(
        self,
        updates: Sequence[Mapping[str, object]],
        *,
        updated_at_ms: int,
        audit_action: str = "channel_updated",
    ) -> list[ChannelConfig]:
        """複数入力設定を一つのtransactionで保存する。

        初回設定では6入力をまとめて反映する必要がある。 1件ずつcommitすると
        途中のvalidation・SQLite障害で、 再起動後に設定の組み合わせが壊れる。
        呼び出し側が渡す値をここで全件検証してから書き込む。

        Args:
            updates: channelごとの更新field mapping群。 一括更新transactionに渡す。
            updated_at_ms: 設定の更新時刻（UTC epoch ms）。
            audit_action: 設定変更と合わせて監査記録へ保存するaction名。

        Returns:
            channel ID順の設定値列。

        Raises:
            ValueError: boolean設定値がbool型でない場合。
                debounce_msが0〜5000の範囲外の場合。
        """
        # 全件を先に正規化・検証し、一件でも不正なら書込みを始めない。
        normalized: list[tuple[int, bool, str, int, str, str]] = []
        seen: set[int] = set()
        for update in updates:
            channel_id = _mapping_int(update, "channel_id")
            if channel_id in seen:
                raise ValueError(f"duplicate channel: {channel_id}")
            seen.add(channel_id)
            enabled = update.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be boolean")
            name = _mapping_text(update, "name")
            debounce_ms = _mapping_int(update, "debounce_ms")
            on_label = _mapping_text(update, "on_label")
            off_label = _mapping_text(update, "off_label")
            _validate_channel_ids([channel_id])
            _validate_channel_text(name, "name")
            _validate_channel_text(on_label, "on_label")
            _validate_channel_text(off_label, "off_label")
            if not 0 <= debounce_ms <= 5000:
                raise ValueError("debounce_ms must be between 0 and 5000")
            normalized.append(
                (channel_id, enabled, name, debounce_ms, on_label, off_label)
            )
        # 検証済み変更と各監査行をまとめてcommitし、部分更新を見せない。
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for (
                channel_id,
                enabled,
                name,
                debounce_ms,
                on_label,
                off_label,
            ) in normalized:
                cursor = self._connection.execute(
                    """
                    UPDATE channels
                    SET enabled = ?, name = ?, debounce_ms = ?, on_label = ?,
                        off_label = ?, updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (
                        int(enabled),
                        name,
                        debounce_ms,
                        on_label,
                        off_label,
                        updated_at_ms,
                        channel_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"channel not found: {channel_id}")
                self._connection.execute(
                    """
                    INSERT INTO audit_log (ts_utc_ms, action, details_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        updated_at_ms,
                        audit_action,
                        json.dumps({"channel_id": channel_id}, separators=(",", ":")),
                    ),
                )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return [
            channel
            for channel in (self.get(channel_id) for channel_id, *_ in normalized)
            if channel is not None
        ]


