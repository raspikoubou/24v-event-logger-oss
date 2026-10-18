"""widget_repository：SQLiteの永続化処理を機能単位で管理する。"""

from __future__ import annotations

import json
import sqlite3

from event_logger.database.output_rule_codec import (
    _output_rule_config,
    _output_rule_from_row,
)
from event_logger.database.repository_helpers import _widget_from_row
from event_logger.domain.rules import OutputRule
from event_logger.domain.widgets import Widget


# 画面のウィジェット定義を個別IDで管理し、表示順と設定内容を永続化する。
class WidgetRepository:
    """Dashboard widget設定のCRUDとauditを提供する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。接続のclose責務は呼び出し側にある。"""
        self._connection = connection

    def list_all(self, *, include_disabled: bool = True) -> list[Widget]:
        """保存順のwidgetを返す。"""
        query = "SELECT * FROM widgets"
        parameters: tuple[object, ...] = ()
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY sort_order, id"
        rows = self._connection.execute(query, parameters).fetchall()
        return [_widget_from_row(row) for row in rows]

    def get(self, widget_id: str) -> Widget | None:
        """IDでwidgetを取得する。"""
        row = self._connection.execute(
            "SELECT * FROM widgets WHERE id = ?", (widget_id,)
        ).fetchone()
        return _widget_from_row(row) if row is not None else None

    # 既存ウィジェットと重複しない識別子を採番する。
    def next_id(self) -> str:
        """人が読める連番widget IDを返す。"""
        rows = self._connection.execute(
            "SELECT id FROM widgets WHERE id LIKE 'widget_%'"
        ).fetchall()
        used: set[int] = set()
        for row in rows:
            suffix = str(row[0]).removeprefix("widget_")
            if suffix.isdigit():
                used.add(int(suffix))
        candidate = 1
        while candidate in used:
            candidate += 1
        return f"widget_{candidate:03d}"

    def create(self, widget: Widget, *, audit_action: str = "widget_created") -> Widget:
        """widgetをINSERTし、設定変更auditを同一transactionで保存する。"""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                """
                INSERT INTO widgets (
                    id, type, title, config_json, size, sort_order, enabled,
                    updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    widget.id,
                    widget.widget_type.value,
                    widget.title,
                    json.dumps(
                        widget.config, ensure_ascii=False, separators=(",", ":")
                    ),
                    widget.size.value,
                    widget.sort_order,
                    int(widget.enabled),
                    widget.updated_at_ms,
                ),
            )
            self._record_audit(widget.updated_at_ms, audit_action, widget.id)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return widget

    # 条件ルールの更新ではルールIDの連続性を維持する。
    def update(self, widget: Widget, *, audit_action: str = "widget_updated") -> Widget:
        """widgetを更新し、旧設定を上書きする。"""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                """
                UPDATE widgets
                SET type = ?, title = ?, config_json = ?, size = ?,
                    sort_order = ?, enabled = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    widget.widget_type.value,
                    widget.title,
                    json.dumps(
                        widget.config, ensure_ascii=False, separators=(",", ":")
                    ),
                    widget.size.value,
                    widget.sort_order,
                    int(widget.enabled),
                    widget.updated_at_ms,
                    widget.id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"widget not found: {widget.id}")
            self._record_audit(widget.updated_at_ms, audit_action, widget.id)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return widget

    # ウィジェット設定変更とカウンタresetの境界を同じトランザクションで確定する。
    def update_with_counter_reset(
        self,
        widget: Widget,
        *,
        reset_at_ms: int,
        audit_action: str = "widget_updated",
    ) -> Widget:
        """widget更新とcounter reset基準を同一transactionで保存する。"""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                """
                UPDATE widgets
                SET type = ?, title = ?, config_json = ?, size = ?,
                    sort_order = ?, enabled = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    widget.widget_type.value,
                    widget.title,
                    json.dumps(
                        widget.config, ensure_ascii=False, separators=(",", ":")
                    ),
                    widget.size.value,
                    widget.sort_order,
                    int(widget.enabled),
                    widget.updated_at_ms,
                    widget.id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"widget not found: {widget.id}")
            self._record_audit(widget.updated_at_ms, audit_action, widget.id)
            # widget更新と同じBEGIN IMMEDIATE内で境界IDを確定する。別commitに
            # すると、widgetだけ新設定になった途中状態をWebが観測できる。
            row = self._connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM events"
            ).fetchone()
            after_event_id = int(row[0])
            self._connection.execute(
                """
                INSERT INTO counter_resets (
                    widget_id, reset_at_ms, after_event_id, carried_count
                ) VALUES (?, ?, ?, 0)
                """,
                (widget.id, reset_at_ms, after_event_id),
            )
            self._connection.execute(
                """
                INSERT INTO audit_log (ts_utc_ms, action, details_json)
                VALUES (?, ?, ?)
                """,
                (
                    reset_at_ms,
                    "counter_reset",
                    json.dumps({"widget_id": widget.id}, separators=(",", ":")),
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return widget

    def delete(self, widget_id: str, *, ts_utc_ms: int) -> None:
        """widgetと関連counter resetを削除する。生イベントは削除しない。"""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                "DELETE FROM widgets WHERE id = ?", (widget_id,)
            )
            if cursor.rowcount != 1:
                raise ValueError(f"widget not found: {widget_id}")
            self._record_audit(ts_utc_ms, "widget_deleted", widget_id)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _record_audit(self, ts_utc_ms: int, action: str, widget_id: str) -> None:
        """widget設定変更の監査行を作る。"""
        self._connection.execute(
            """
            INSERT INTO audit_log (ts_utc_ms, action, details_json)
            VALUES (?, ?, ?)
            """,
            (ts_utc_ms, action, json.dumps({"widget_id": widget_id})),
        )


# 出力条件を保存する前に競合・対象チャンネルの整合性を確認する。
class OutputRuleRepository:
    """output_rulesの固定条件JSONをDomain ruleへ変換する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。"""
        self._connection = connection

    def list_all(self) -> list[OutputRule]:
        """保存順の条件ルールを返す。"""
        rows = self._connection.execute(
            "SELECT * FROM output_rules ORDER BY id"
        ).fetchall()
        return [_output_rule_from_row(row) for row in rows]

    def get(self, rule_id: int) -> OutputRule | None:
        """IDで条件ルールを取得する。"""
        row = self._connection.execute(
            "SELECT * FROM output_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        return _output_rule_from_row(row) if row is not None else None

    def next_id(self) -> int:
        """新規ルールへ割り当てるIDを返す。"""
        row = self._connection.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM output_rules"
        ).fetchone()
        return int(row[0])

    def create(self, rule: OutputRule) -> OutputRule:
        """固定スキーマの条件ルールをINSERTする。"""
        config_json = json.dumps(
            _output_rule_config(rule.condition),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            self._connection.execute(
                """
                INSERT INTO output_rules (
                    id, name, enabled, rule_type, config_json, output_channel,
                    updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.id,
                    rule.name,
                    int(rule.condition.enabled),
                    rule.condition.rule_type.value,
                    config_json,
                    rule.condition.output_channel,
                    rule.updated_at_ms,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO audit_log (ts_utc_ms, action, details_json)
                VALUES (?, ?, ?)
                """,
                (
                    rule.updated_at_ms,
                    "output_rule_created",
                    json.dumps({"rule_id": rule.id}, separators=(",", ":")),
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return rule

    def update(self, rule: OutputRule) -> OutputRule:
        """固定スキーマの条件ルールを更新する。"""
        config_json = json.dumps(
            _output_rule_config(rule.condition),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                """
                UPDATE output_rules
                SET name = ?, enabled = ?, rule_type = ?, config_json = ?,
                    output_channel = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    rule.name,
                    int(rule.condition.enabled),
                    rule.condition.rule_type.value,
                    config_json,
                    rule.condition.output_channel,
                    rule.updated_at_ms,
                    rule.id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"output rule not found: {rule.id}")
            self._connection.execute(
                """
                INSERT INTO audit_log (ts_utc_ms, action, details_json)
                VALUES (?, ?, ?)
                """,
                (
                    rule.updated_at_ms,
                    "output_rule_updated",
                    json.dumps({"rule_id": rule.id}, separators=(",", ":")),
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return rule

    def delete(self, rule_id: int, *, ts_utc_ms: int = 0) -> None:
        """ルール設定だけを削除し、過去output_eventsは保持する。"""
        try:
            self._connection.execute(
                "DELETE FROM output_rules WHERE id = ?", (rule_id,)
            )
            self._connection.execute(
                """
                INSERT INTO audit_log (ts_utc_ms, action, details_json)
                VALUES (?, ?, ?)
                """,
                (ts_utc_ms, "output_rule_deleted", json.dumps({"rule_id": rule_id})),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise


