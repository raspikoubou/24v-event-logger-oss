"""SQLite動作中DBの安全なbackup処理。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from event_logger.database.connection import connect_database


def backup_database(source_path: str | Path, destination_path: str | Path) -> None:
    """SQLite Connection.backupでsource DBをdestinationへ複製する。

    Args:
        source_path: SQLite backup元databaseのpath。
        destination_path: SQLite backupを作成する出力path。

    Returns:
        None。 sourceのSQLite snapshotをdestination fileへbackupし、
            両connectionを閉じる。 sourceの内容は変更しない。
    """
    # sourceは通常接続設定で開き、SQLite backup APIにsnapshot整合性を委ねる。
    source = connect_database(source_path)
    # destination側のtransactionと両接続のcloseを、失敗経路も含めて対称に管理する。
    try:
        destination = sqlite3.connect(str(destination_path))
    except BaseException:
        source.close()
        raise
    try:
        with closing(destination):
            with destination:
                source.backup(destination)
    finally:
        source.close()


def integrity_check(database_path: str | Path) -> tuple[bool, list[str]]:
    """integrity_checkとforeign_key_checkの結果を返す。

    Args:
        database_path: 読み書きまたは検査対象のSQLite database file path。

    Returns:
        整合性検査の合否と検出したproblem文字列列。
    """
    with closing(connect_database(database_path)) as connection:
        with connection:
            result = [
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            ]
            foreign_keys = [
                ":".join(str(value) for value in row)
                for row in connection.execute("PRAGMA foreign_key_check")
            ]
    errors = [item for item in result if item.lower() != "ok"] + foreign_keys
    return not errors, errors
