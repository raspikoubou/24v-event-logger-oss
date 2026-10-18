"""SQLite接続と明示的なschema初期化。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

LATEST_SCHEMA_VERSION = 2
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class SchemaVersionError(RuntimeError):
    """サポートできないschema versionを検出した。"""


def connect_database(
    database_path: str | Path,
    *,
    allow_empty_create: bool = False,
) -> sqlite3.Connection:
    """SQLiteへ接続し、製品共通のPRAGMAを設定する。

    Args:
        database_path: 接続するDBのパス。 `:memory:`も指定できる。
        allow_empty_create: 明示的に新規DBを作る呼出元だけTrueにする。

    Returns:
        共通PRAGMA適用済みのSQLite接続。

    Raises:
        SchemaVersionError: 既存の0 byte fileを初期DBと誤認しそうな場合。
        sqlite3.Error: DBを開く、 またはPRAGMAを設定できない場合。
    """
    path = str(database_path)
    if path != ":memory:":
        database_file = Path(path)
        if (
            not allow_empty_create
            and database_file.is_file()
            and database_file.stat().st_size == 0
        ):
            # SQLiteは0 byteの既存ファイルも新規DBとして扱う。運用DBの破損を
            # 初回DBと誤認すると空schemaを作り、欠損を見逃すので明示初期化だけ
            # が既存の空fileを受け入れる。
            raise SchemaVersionError(
                "existing database file is empty; refusing to initialize it"
            )
    connection = sqlite3.connect(path, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            # auto_vacuumはschema作成前だけ設定する。既存DBへのsetterを全request
            # で実行すると、値が既に2でもSQLiteがwrite lockを要求し、loggerdの
            # commitやretentionと競合して接続開始がSQLITE_BUSYになる。
            auto_vacuum_mode = int(
                connection.execute("PRAGMA auto_vacuum").fetchone()[0]
            )
            if auto_vacuum_mode != 2 and not _has_user_tables(connection):
                connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            connection.execute("PRAGMA journal_mode = WAL")
        else:
            # :memory:は接続ごとに空DBになるため、この接続だけで設定する。
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
        connection.execute("PRAGMA synchronous = FULL")
    except BaseException:
        connection.close()
        raise
    return connection


def initialize_database(database_path: str | Path) -> None:
    """新規DBへ最新schemaを作成する。

    既存DBへ通常起動時に黙ってschema変更を行わないよう、 既存テーブルと
    user_versionが混在する状態はエラーにする。 更新処理から呼ぶmigrationは
    将来この関数とは別の明示的な経路へ追加する。

    Args:
        database_path: 読み書きまたは検査対象のSQLite database file path。

    Returns:
        None。 最新schemaを新規DBへ作成し、 接続を閉じる。
    """
    # 新規DBを明示的に作る入口だけは、既存0 byte fileを許可する。
    with closing(
        connect_database(database_path, allow_empty_create=True)
    ) as connection:
        with connection:
            initialize_connection(connection)


def initialize_connection(connection: sqlite3.Connection) -> None:
    """開いた接続へ最新schemaを明示的に適用する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

    Returns:
        None。 渡されたSQLite接続へ変更を適用し、 接続のcloseは呼出元に残す。

    Raises:
        SchemaVersionError: 「existing tables have no schema version; explicit migration
            is required」に該当すると判定した場合。
    """
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    has_tables = _has_user_tables(connection)
    if current_version > LATEST_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema {current_version} is newer than supported "
            f"{LATEST_SCHEMA_VERSION}"
        )
    if current_version == 0 and has_tables:
        raise SchemaVersionError(
            "existing tables have no schema version; explicit migration is required"
        )
    if current_version == 0:
        _create_schema_latest(connection)
    elif current_version != LATEST_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"schema {current_version} requires an explicit migration"
        )


def _create_schema_latest(connection: sqlite3.Connection) -> None:
    """空のSQLite databaseへ最新schemaを作成する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

    Returns:
        None。 schema・channel seed・user_versionを一つの明示transactionで作成し、
            受け取ったconnectionは閉じない。
    """
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    seed_statements = "\n".join(
        "INSERT INTO channels (id, enabled, name, debounce_ms, updated_at_ms) "
        f"VALUES ({channel_id}, 0, 'IN{channel_id}', 20, 0);"
        for channel_id in range(1, 7)
    )
    # executescriptは呼び出し前の暗黙transactionをcommitするため、schema・
    # 初期channel・user_versionを一つの明示script transactionへまとめる。
    # 初期化途中で電源断しても「tablesはあるがuser_version=0」の壊れたDBを
    # 残さない。
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{schema}\n"
        f"{seed_statements}\n"
        f"PRAGMA user_version = {LATEST_SCHEMA_VERSION};\n"
        "COMMIT;"
    )
    try:
        connection.executescript(script)
    except Exception:
        connection.rollback()
        raise


def _has_user_tables(connection: sqlite3.Connection) -> bool:
    """SQLite内部テーブル以外が存在するか確認する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

    Returns:
        判定対象が条件を満たす場合True、 その他はFalse。
    """
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def _seed_channels(connection: sqlite3.Connection) -> None:
    """初回設定前のIN1〜IN6をすべて無効で作成する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

    Returns:
        None。 渡されたSQLite接続へ変更を適用し、 接続のcloseは呼出元に残す。
    """
    connection.executemany(
        """
        INSERT INTO channels (id, enabled, name, debounce_ms, updated_at_ms)
        VALUES (?, 0, ?, 20, 0)
        """,
        [(channel_id, f"IN{channel_id}") for channel_id in range(1, 7)],
    )
