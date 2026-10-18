"""SQLite動作中DBの安全なbackup処理。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from event_logger.database.connection import connect_database


# 稼働中のSQLiteを単純コピーせず、バックアップAPIで整合したスナップショットを作る。
def backup_database(source_path: str | Path, destination_path: str | Path) -> None:
    """SQLite Connection.backupでsource DBをdestinationへ複製する。"""
    source = connect_database(source_path)
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


# 復元前などにDB全体を検査し、異常内容を呼び出し側が判断できる形で返す。
def integrity_check(database_path: str | Path) -> tuple[bool, list[str]]:
    """integrity_checkとforeign_key_checkの結果を返す。"""
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
