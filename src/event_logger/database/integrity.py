"""SQLite schema・整合性の読み取り専用検査。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from event_logger.database.connection import LATEST_SCHEMA_VERSION, connect_database

_REQUIRED_TABLES = frozenset(
    {
        "sessions",
        "events",
        "output_events",
        "channels",
        "widgets",
        "counter_resets",
        "output_rules",
        "app_settings",
        "audit_log",
    }
)


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """SQLite integrity検査の結果。"""

    ok: bool
    schema_version: int
    integrity_messages: tuple[str, ...]
    foreign_key_violations: tuple[str, ...]
    missing_tables: tuple[str, ...]


class DatabaseIntegrityError(RuntimeError):
    """安全にretentionを続行できないDB不整合。"""

    def __init__(self, report: IntegrityReport) -> None:
        """検査結果を保持して例外を作る。

        Args:
            report: SQLite integrity検査結果を持つIntegrityReport。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self.report = report
        super().__init__(format_integrity_error(report))


def check_database(database_path: str | Path) -> IntegrityReport:
    """SQLite fileを開いて整合性を検査する。

    Args:
        database_path: 読み書きまたは検査対象のSQLite database file path。

    Returns:
        check_database/check_connectionが集めたintegrity結果値。
    """
    with closing(connect_database(database_path)) as connection:
        with connection:
            return check_connection(connection)


def check_connection(connection: sqlite3.Connection) -> IntegrityReport:
    """開いたSQLite接続のschema・integrity・foreign keyを検査する。

    Args:
        connection: 呼出元管理のSQLite接続。 SQL実行後もcloseせず呼出元へ残す。

    Returns:
        check_database/check_connectionが集めたintegrity結果値。
    """
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    integrity_messages = tuple(
        str(row[0])
        for row in connection.execute("PRAGMA integrity_check")
        if str(row[0]).lower() != "ok"
    )
    foreign_key_violations = tuple(
        ":".join(str(value) for value in row)
        for row in connection.execute("PRAGMA foreign_key_check")
    )
    tables = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
    }
    missing_tables = tuple(sorted(_REQUIRED_TABLES - tables))
    schema_errors: tuple[str, ...] = ()
    if schema_version != LATEST_SCHEMA_VERSION:
        schema_errors = (
            f"unsupported schema version {schema_version}; "
            f"expected {LATEST_SCHEMA_VERSION}",
        )
    return IntegrityReport(
        ok=not (
            integrity_messages
            or foreign_key_violations
            or missing_tables
            or schema_errors
        ),
        schema_version=schema_version,
        integrity_messages=integrity_messages + schema_errors,
        foreign_key_violations=foreign_key_violations,
        missing_tables=missing_tables,
    )


def format_integrity_error(report: IntegrityReport) -> str:
    """IntegrityReportをログ向けの短い文字列へ変換する。

    Args:
        report: SQLite integrity検査結果を持つIntegrityReport。

    Returns:
        指定timezoneのoffset付き表示文字列。
    """
    details = [*report.integrity_messages, *report.foreign_key_violations]
    if report.missing_tables:
        details.append(f"missing tables: {', '.join(report.missing_tables)}")
    return "; ".join(details) or "database integrity check failed"


__all__ = [
    "DatabaseIntegrityError",
    "IntegrityReport",
    "check_connection",
    "check_database",
    "format_integrity_error",
]
