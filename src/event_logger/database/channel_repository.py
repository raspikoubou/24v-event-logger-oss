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


# 入力チャンネルの設定保存を集中管理し、外部で直接SQL更新させない。
class ChannelRepository:
    """入力チャンネル設定を読み書きする。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。接続のclose責務は呼び出し側にある。"""
        self._connection = connection

    def list_all(self) -> list[ChannelConfig]:
        """IN1〜IN6をチャンネル番号順に返す。"""
        rows = self._connection.execute("SELECT * FROM channels ORDER BY id").fetchall()
        return [_channel_from_row(row) for row in rows]

    def get(self, channel_id: int) -> ChannelConfig | None:
        """指定入力チャンネルを取得する。"""
        _validate_channel_ids([channel_id])
        row = self._connection.execute(
            "SELECT * FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()
        return _channel_from_row(row) if row is not None else None

    # 個別更新でも値を検証し、監査履歴と設定の整合性を保つ。
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
        """入力設定とaudit_logを同一transactionで保存する。"""
        _validate_channel_ids([channel_id])
        _validate_channel_text(name, "name")
        _validate_channel_text(on_label, "on_label")
        _validate_channel_text(off_label, "off_label")
        if not 0 <= debounce_ms <= 5000:
            raise ValueError("debounce_ms must be between 0 and 5000")
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

    # 初回設定の複数チャンネルをまとめて反映するための更新経路。
    def update_many(
        self,
        updates: Sequence[Mapping[str, object]],
        *,
        updated_at_ms: int,
        audit_action: str = "channel_updated",
    ) -> list[ChannelConfig]:
        """複数入力設定を一つのtransactionで保存する。

        初回設定では6入力をまとめて反映する必要がある。1件ずつcommitすると
        途中のvalidation・SQLite障害で、再起動後に設定の組み合わせが壊れる。
        呼び出し側が渡す値をここで全件検証してから書き込む。
        """
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


