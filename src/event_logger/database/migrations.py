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


# DBスキーマの変更は更新処理から明示的に起動する。通常サービス起動とは分ける。
def migrate_database(database_path: str | Path) -> MigrationResult:
    """DBを明示的に最新schemaへ更新する。

    通常のloggerd/Web起動からは呼び出さず、更新処理や保守CLIの明示操作から
    呼び出す。未知のテーブルや将来versionを破壊的に解釈しない。
    """
    with closing(connect_database(database_path)) as connection:
        with connection:
            return migrate_connection(connection)


# 既存DBのバージョンと必要な差分だけを確認して段階的に更新する。
def migrate_connection(connection: sqlite3.Connection) -> MigrationResult:
    """開いた接続を最新schemaへ更新する。"""
    old_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if old_version > LATEST_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema {old_version} is newer than supported "
            f"{LATEST_SCHEMA_VERSION}"
        )
    if old_version == LATEST_SCHEMA_VERSION:
        changed = _ensure_events_autoincrement(connection)
        changed = _ensure_incremental_vacuum(connection) or changed
        changed = _ensure_query_indexes(connection) or changed
        return MigrationResult(old_version, old_version, changed)
    if old_version == 1:
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

    _create_schema_latest(connection)
    return MigrationResult(old_version, LATEST_SCHEMA_VERSION, True)


# ID再利用を避けるテーブル作り直しでは、既存イベントと参照関係を維持する。
def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """event IDの再利用を防ぐevents tableへ再構築する。"""
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
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


# 既存バージョン番号だけでなく、実際のテーブル定義の差異も補正対象にする。
def _ensure_events_autoincrement(connection: sqlite3.Connection) -> bool:
    """v2でも旧定義のevents tableならAUTOINCREMENTへ再構築する。"""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise SchemaVersionError("schema v2 is missing the events table")
    if "AUTOINCREMENT" in row[0].upper():
        return False
    _migrate_v1_to_v2(connection)
    return True


# 古いDBへ容量回収設定を適用する処理は通常起動から分離する。
def _ensure_incremental_vacuum(connection: sqlite3.Connection) -> bool:
    """既存v2 DBのauto_vacuumを明示migrationで有効化する。"""
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
    """長期保存・Dashboard polling用の不足indexを既存DBへ追加する。"""
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
    if changed:
        connection.commit()
    return changed


__all__ = ["MigrationResult", "migrate_connection", "migrate_database"]
