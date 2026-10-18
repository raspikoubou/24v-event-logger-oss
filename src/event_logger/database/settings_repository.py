"""製品設定と操作監査のSQLite Repository。

設定の一括保存は同じSQLite transactionで監査行とともに確定する。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping


class SettingsRepository:
    """app_settingsのJSON値と設定変更auditを扱う。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。

        Args:
            connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._connection = connection

    def get_json(self, key: str, default: object = None) -> object:
        """設定値をJSONから復元し、未設定時はdefaultを返す。

        Args:
            key: 設定項目名。 保存値の取得または更新対象を特定する。
            default: 保存keyが存在しない場合に返す呼出元指定の既定値。

        Returns:
            object型の結果。 戻り値の型に応じ、 該当なしはNoneまたは空collectionとなる。

        Raises:
            ValueError: deviceまたはSQLite操作で失敗が返された場合。
        """
        row = self._connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(str(row["value_json"]))
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid app setting JSON: {key}") from error

    def set_json(
        self,
        *,
        key: str,
        value: object,
        updated_at_ms: int,
        audit_action: str = "setting_updated",
    ) -> None:
        """設定値とaudit_logを同一transactionでupsertする。

        Args:
            key: 設定項目名。 保存値の取得または更新対象を特定する。
            value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。
            updated_at_ms: 設定の更新時刻（UTC epoch ms）。
            audit_action: 設定変更と合わせて監査記録へ保存するaction名。

        Returns:
            None。 渡されたSQLite接続へ変更を適用し、 接続のcloseは呼出元に残す。
        """
        self.set_many_json(
            values={key: value},
            updated_at_ms=updated_at_ms,
            audit_action=audit_action,
        )

    def set_many_json(
        self,
        *,
        values: Mapping[str, object],
        updated_at_ms: int,
        audit_action: str = "setting_updated",
    ) -> None:
        """複数設定値とauditを一つのtransactionでupsertする。

        Args:
            values: 複数設定のkey/value群。 一括更新時に一つのtransactionへまとめる。
            updated_at_ms: 設定の更新時刻（UTC epoch ms）。
            audit_action: 設定変更と合わせて監査記録へ保存するaction名。

        Returns:
            None。 渡されたSQLite接続へ変更を適用し、 接続のcloseは呼出元に残す。

        Raises:
            ValueError: 「setting key must be 1..100
                characters」に該当すると判定した場合。
        """
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


class AuditLogRepository:
    """運用操作のaudit_log書込みを一箇所へ閉じ込める。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。

        Args:
            connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._connection = connection

    def record(self, *, action: str, details_json: str, ts_utc_ms: int) -> None:
        """操作auditを1件commitする。

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


