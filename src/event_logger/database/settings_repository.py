"""製品設定と操作監査のSQLite Repository。

設定の一括保存は同じSQLite transactionで監査行とともに確定する。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping


# 設定値をJSONとして保存し、更新時には同じDB内に変更履歴を残す。
class SettingsRepository:
    """app_settingsのJSON値と設定変更auditを扱う。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。"""
        self._connection = connection

    def get_json(self, key: str, default: object = None) -> object:
        """設定値をJSONから復元し、未設定時はdefaultを返す。"""
        row = self._connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(str(row["value_json"]))
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid app setting JSON: {key}") from error

    # 設定本体と変更監査は一つの永続化処理として扱う。
    def set_json(
        self,
        *,
        key: str,
        value: object,
        updated_at_ms: int,
        audit_action: str = "setting_updated",
    ) -> None:
        """設定値とaudit_logを同一transactionでupsertする。"""
        self.set_many_json(
            values={key: value},
            updated_at_ms=updated_at_ms,
            audit_action=audit_action,
        )

    # 関連設定をまとめて更新し、途中失敗で一部だけ有効にならないようにする。
    def set_many_json(
        self,
        *,
        values: Mapping[str, object],
        updated_at_ms: int,
        audit_action: str = "setting_updated",
    ) -> None:
        """複数設定値とauditを一つのtransactionでupsertする。"""
        if not values:
            return
        encoded_values: list[tuple[str, str]] = []
        for key, value in values.items():
            if not key or len(key) > 100:
                raise ValueError("setting key must be 1..100 characters")
            encoded_values.append(
                (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for key, encoded in encoded_values:
                self._connection.execute(
                    """
                    INSERT INTO app_settings (key, value_json, updated_at_ms)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (key, encoded, updated_at_ms),
                )
                self._connection.execute(
                    """
                    INSERT INTO audit_log (ts_utc_ms, action, details_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        updated_at_ms,
                        audit_action,
                        json.dumps({"key": key}, separators=(",", ":")),
                    ),
                )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise


# 操作・設定変更を後から確認できる監査ログを扱う。
class AuditLogRepository:
    """運用操作のaudit_log書込みを一箇所へ閉じ込める。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。"""
        self._connection = connection

    def record(self, *, action: str, details_json: str, ts_utc_ms: int) -> None:
        """操作auditを1件commitする。"""
        self._connection.execute(
            """
            INSERT INTO audit_log (ts_utc_ms, action, details_json)
            VALUES (?, ?, ?)
            """,
            (ts_utc_ms, action, details_json),
        )
        self._connection.commit()


