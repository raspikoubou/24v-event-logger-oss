"""監視セッションに関するSQLite永続化操作。"""

from __future__ import annotations

import sqlite3

from event_logger.common.models import Session


class SessionRepository:
    """監視sessionの作成、終了、検索を提供する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。接続のclose責務は呼び出し側にある。"""
        self._connection = connection

    def create(
        self,
        *,
        boot_id: str,
        started_at_ms: int,
        app_version: str,
        clock_synced: bool,
    ) -> Session:
        """新しい監視sessionをcommitして返す。"""
        cursor = self._connection.execute(
            """
            INSERT INTO sessions (
                boot_id, started_at_ms, app_version, clock_synced
            ) VALUES (?, ?, ?, ?)
            """,
            (boot_id, started_at_ms, app_version, int(clock_synced)),
        )
        self._connection.commit()
        return Session(
            id=_lastrowid(cursor),
            boot_id=boot_id,
            started_at_ms=started_at_ms,
            ended_at_ms=None,
            app_version=app_version,
            clock_synced=clock_synced,
        )

    def end(self, session_id: int, ended_at_ms: int) -> None:
        """正常終了したsessionの終了時刻をcommitする。"""
        cursor = self._connection.execute(
            "UPDATE sessions SET ended_at_ms = ? WHERE id = ? AND ended_at_ms IS NULL",
            (ended_at_ms, session_id),
        )
        if cursor.rowcount != 1:
            self._connection.rollback()
            raise ValueError(f"active session not found: {session_id}")
        self._connection.commit()

    def get(self, session_id: int) -> Session | None:
        """sessionをIDで取得する。"""
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return _session_from_row(row) if row is not None else None

    def list_all(self) -> list[Session]:
        """作成順に全sessionを返す。"""
        rows = self._connection.execute("SELECT * FROM sessions ORDER BY id").fetchall()
        return [_session_from_row(row) for row in rows]


def _session_from_row(row: sqlite3.Row) -> Session:
    """SQLite rowをSessionへ変換する。"""
    return Session(
        id=int(row["id"]),
        boot_id=str(row["boot_id"]),
        started_at_ms=int(row["started_at_ms"]),
        ended_at_ms=(
            int(row["ended_at_ms"]) if row["ended_at_ms"] is not None else None
        ),
        app_version=str(row["app_version"]),
        clock_synced=bool(row["clock_synced"]),
    )


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    """INSERT直後のrowidを整数として取得する。"""
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return a row id")
    return int(cursor.lastrowid)
