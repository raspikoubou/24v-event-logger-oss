"""更新処理からだけ呼び出す明示的なSQLite schema migration。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from event_logger.database.connection import (
    LATEST_SCHEMA_VERSION,
    SchemaVersionError,
    _create_schema_latest,
    _has_user_tables,
    connect_database,
)


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """1回の明示的migrationで移動したschema version。"""

    old_version: int
    new_version: int
    changed: bool


def migrate_database(database_path: str | Path) -> MigrationResult:
    """DBを明示的に最新schemaへ更新する。

    通常のloggerd/Web起動からは呼び出さず、 更新処理や保守CLIの明示操作から
    呼び出す。 未知のテーブルや将来versionを破壊的に解釈しない。

    Args:
        database_path: 読み書きまたは検査対象のSQLite database file path。

    Returns:
        MigrationResult型の結果。 戻り値の型に応じ、
            該当なしはNoneまたは空collectionとなる。
    """
    with closing(connect_database(database_path)) as connection:
        with connection:
            return migrate_connection(connection)


def migrate_connection(connection: sqlite3.Connection) -> MigrationResult:
    """開いた接続を最新schemaへ更新する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

    Returns:
        MigrationResult型の結果。 戻り値の型に応じ、
            該当なしはNoneまたは空collectionとなる。

    Raises:
        SchemaVersionError: 「existing tables have no schema version; refusing implicit
            conversion」に該当すると判定した場合。
    """
    # user_versionを起点に移行経路を選び、対応外schemaへは変更を加えない。
    old_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if old_version > LATEST_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema {old_version} is newer than supported "
            f"{LATEST_SCHEMA_VERSION}"
        )
    if old_version == LATEST_SCHEMA_VERSION:
        # versionが最新でも、旧定義で残り得るtable/index/auto_vacuumだけを補修する。
        changed = _ensure_events_autoincrement(connection)
        changed = _ensure_incremental_vacuum(connection) or changed
        changed = _ensure_query_indexes(connection) or changed
        return MigrationResult(old_version, old_version, changed)
    if old_version == 1:
        # v1のevent ID意味を保ったままv2 tableへ移し、追加の保守要件も続けて満たす。
        _migrate_v1_to_v2(connection)
        _ensure_incremental_vacuum(connection)
        _ensure_query_indexes(connection)
        return MigrationResult(old_version, LATEST_SCHEMA_VERSION, True)
    if old_version != 0:
        raise SchemaVersionError(
            f"no migration path from schema {old_version} to {LATEST_SCHEMA_VERSION}"
        )
    if _has_user_tables(connection):
        raise SchemaVersionError(
            "existing tables have no schema version; refusing implicit conversion"
        )

    # version 0の空DBだけを新規schemaとして初期化する。
    _create_schema_latest(connection)
    return MigrationResult(old_version, LATEST_SCHEMA_VERSION, True)


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """event IDの再利用を防ぐevents tableへ再構築する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

    Returns:
        None。 渡されたSQLite接続へ変更を適用し、 接続のcloseは呼出元に残す。
    """
    # table再作成中だけFK検査を止め、元の設定をfinallyで必ず戻す。
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        # 新tableへのcopy・旧table置換・index・version更新を一transactionにまとめる。
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE events_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                ts_utc_ms INTEGER NOT NULL,
                mono_ns INTEGER NOT NULL,
                channel_id INTEGER NOT NULL CHECK(channel_id BETWEEN 1 AND 6),
                value INTEGER NOT NULL CHECK(value IN (0, 1)),
                kind INTEGER NOT NULL CHECK(kind IN (0, 1)),
                clock_synced INTEGER NOT NULL DEFAULT 0 CHECK(clock_synced IN (0, 1)),
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO events_v2
                (
                    id, session_id, ts_utc_ms, mono_ns, channel_id, value, kind,
                    clock_synced
                )
            SELECT
                id, session_id, ts_utc_ms, mono_ns, channel_id, value, kind,
                clock_synced
            FROM events
            ORDER BY id
            """
        )
        connection.execute("DROP TABLE events")
        connection.execute("ALTER TABLE events_v2 RENAME TO events")
        connection.execute("CREATE INDEX idx_events_ts ON events(ts_utc_ms)")
        connection.execute(
            "CREATE INDEX idx_events_channel_ts ON events(channel_id, ts_utc_ms)"
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute(f"PRAGMA foreign_keys = {foreign_keys}")


def _ensure_events_autoincrement(connection: sqlite3.Connection) -> bool:
    """v2でも旧定義のevents tableならAUTOINCREMENTへ再構築する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

    Returns:
        条件が成立している場合True、 未成立または未設定の場合False。

    Raises:
        SchemaVersionError: 「schema v2 is missing the events
            table」に該当すると判定した場合。
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise SchemaVersionError("schema v2 is missing the events table")
    if "AUTOINCREMENT" in row[0].upper():
        return False
    _migrate_v1_to_v2(connection)
    return True


def _ensure_incremental_vacuum(connection: sqlite3.Connection) -> bool:
    """既存v2 DBのauto_vacuumを明示migrationで有効化する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

    Returns:
        条件が成立している場合True、 未成立または未設定の場合False。

    Raises:
        SchemaVersionError: 「failed to enable incremental
            auto_vacuum」に該当すると判定した場合。
    """
    row = connection.execute("PRAGMA auto_vacuum").fetchone()
    current = int(row[0]) if row is not None else 0
    if current == 2:
        return False
    # auto_vacuumの変更はVACUUMでファイル全体を書き直す必要がある。
    # 通常起動で暗黙に実行すると長時間のDBロックになるため、明示的な
    # maintenance migrationからだけ呼び出す。
    try:
        connection.commit()
        connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
        connection.commit()
        connection.execute("VACUUM")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    final_row = connection.execute("PRAGMA auto_vacuum").fetchone()
    if final_row is None or int(final_row[0]) != 2:
        raise SchemaVersionError("failed to enable incremental auto_vacuum")
    return True


def _ensure_query_indexes(connection: sqlite3.Connection) -> bool:
    """長期保存・Dashboard polling用の不足indexを既存DBへ追加する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

    Returns:
        条件が成立している場合True、 未成立または未設定の場合False。
    """
    indexes = (
        (
            "idx_events_counter",
            "events",
            "CREATE INDEX idx_events_counter ON events(channel_id, value, kind, id)",
        ),
        (
            "idx_events_edge_count",
            "events",
            "CREATE INDEX idx_events_edge_count "
            "ON events(channel_id, value, kind, ts_utc_ms)",
        ),
        (
            "idx_events_clock_synced",
            "events",
            "CREATE INDEX idx_events_clock_synced ON events(clock_synced)",
        ),
        (
            "idx_events_session_id",
            "events",
            "CREATE INDEX idx_events_session_id ON events(session_id, id)",
        ),
        (
            "idx_output_events_clock_synced",
            "output_events",
            "CREATE INDEX idx_output_events_clock_synced "
            "ON output_events(clock_synced)",
        ),
        (
            "idx_output_events_session_id",
            "output_events",
            "CREATE INDEX idx_output_events_session_id "
            "ON output_events(session_id, id)",
        ),
    )
    # 任意tableを持たない互換DBもあるため、table存在を確認してから不足indexを追加する。
    changed = False
    for name, table, statement in indexes:
        table_row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if table_row is None:
            # 最小構成の旧v2 DBをmigrationで壊さない。完全な製品schemaへ
            # 更新されたDBでは対象tableが必ず存在し、indexを作成する。
            continue
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
        if row is None:
            connection.execute(statement)
            changed = True
    # indexを追加したときだけcommitし、変更のないDBへ不要なwriteを発生させない。
    if changed:
        connection.commit()
    return changed


__all__ = ["MigrationResult", "migrate_connection", "migrate_database"]
