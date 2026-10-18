"""監視セッションに関するSQLite永続化操作。"""

from __future__ import annotations

import sqlite3

from event_logger.common.models import Session


class SessionRepository:
    """監視sessionの作成、終了、検索を提供する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """接続を受け取る。接続のclose責務は呼び出し側にある。

        Args:
            connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._connection = connection

    def create(
        self,
        *,
        boot_id: str,
        started_at_ms: int,
        app_version: str,
        clock_synced: bool,
    ) -> Session:
        """新しい監視sessionをcommitして返す。

        Args:
            boot_id: このsessionを動かすOS bootの識別子。
            started_at_ms: session開始時刻（UTC epoch ms）。
            app_version: session作成時に記録するapplication version。
            clock_synced: event記録時のsystem clock同期状態。

        Returns:
            Session型の結果。 戻り値の型に応じ、
                該当なしはNoneまたは空collectionとなる。
        """
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
        """正常終了したsessionの終了時刻をcommitする。

        Args:
            session_id: 対象sessionのDB ID。 session外eventが混ざらないようqueryで絞る。
            ended_at_ms: session終了時刻（UTC epoch ms）。 Noneは未終了。

        Returns:
            None。 渡されたSQLite接続へ変更を適用し、 接続のcloseは呼出元に残す。

        Raises:
            ValueError: deviceまたはSQLite操作で失敗が返された場合。
        """
        cursor = self._connection.execute(
            "UPDATE sessions SET ended_at_ms = ? WHERE id = ? AND ended_at_ms IS NULL",
            (ended_at_ms, session_id),
        )
        if cursor.rowcount != 1:
            self._connection.rollback()
            raise ValueError(f"active session not found: {session_id}")
        self._connection.commit()

    def get(self, session_id: int) -> Session | None:
        """sessionをIDで取得する。

        Args:
            session_id: 対象sessionのDB ID。 session外eventが混ざらないようqueryで絞る。

        Returns:
            指定IDのSession。 未登録ならNone。
        """
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return _session_from_row(row) if row is not None else None

    def list_all(self) -> list[Session]:
        """作成順に全sessionを返す。

        Args:
            なし。

        Returns:
            session ID順の監視session列。
        """
        rows = self._connection.execute("SELECT * FROM sessions ORDER BY id").fetchall()
        return [_session_from_row(row) for row in rows]


def _session_from_row(row: sqlite3.Row) -> Session:
    """SQLite rowをSessionへ変換する。

    Args:
        row: SQLite row mapping。 column値を検査してdomain valueへ移す。

    Returns:
        Session型の結果。 戻り値の型に応じ、 該当なしはNoneまたは空collectionとなる。
    """
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
    """INSERT直後のrowidを整数として取得する。

    Args:
        cursor: SQL statementを実行したSQLite cursor。 lastrowid取得に使う。

    Returns:
        保存先で次に使う、 または直近に割り当てられた整数ID。

    Raises:
        RuntimeError: 「SQLite did not return a row id」に該当すると判定した場合。
    """
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return a row id")
    return int(cursor.lastrowid)
