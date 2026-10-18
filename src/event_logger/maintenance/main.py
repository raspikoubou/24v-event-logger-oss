"""event-logger-maintenance.timerから呼ぶ定期保守の入口。"""

from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from event_logger.adapters.clock import Clock, SystemClock
from event_logger.database.connection import connect_database, initialize_connection
from event_logger.database.integrity import (
    DatabaseIntegrityError,
    IntegrityReport,
    check_connection,
)
from event_logger.database.repositories import RetentionRepository, SettingsRepository
from event_logger.maintenance.retention import RetentionResult, RetentionService
from event_logger.maintenance.storage import StorageMonitor, StorageSnapshot

RETENTION_CHOICES = (30, 90, 180, 365, 730, 1825)


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    """1回の保守処理結果。"""

    retention: RetentionResult
    storage: StorageSnapshot
    integrity: IntegrityReport


def run_once(
    *,
    database_path: str | Path,
    retention_days: int | None = 365,
    clock: Clock | None = None,
    disk_path: str | Path | None = None,
    use_stored_retention: bool = True,
) -> MaintenanceResult:
    """retentionと容量測定を1回実行する。

    通常のtimerはDBに保存した保存期間を使い、明示CLIの`--no-retention`だけが
    `use_stored_retention=False`を指定してretentionを無効化する。

    Args:
        database_path: SQLite databaseのfilesystem path。
        retention_days: 保存期間（日）。Noneなら通常retentionを行わない。
        clock: UTC/monotonic時刻と同期状態を提供するClock。
        disk_path: 容量を測るfilesystem上のpath。
        use_stored_retention: DB保存期間設定を採用するか。明示CLI指定ではFalseを許す。

    Returns:
        1回分のretention、容量保護、DB integrity結果。

    Raises:
        DatabaseIntegrityError: database schema/integrity検査が失敗した場合。
        ValueError: 引数の形式・範囲が仕様外、または現在
            の状態では要求を適用できない場合。
    """
    _validate_retention_days(retention_days)
    current_clock = clock or SystemClock()
    with closing(connect_database(database_path)) as connection:
        with connection:
            initialize_connection(connection)
            integrity = check_connection(connection)
            if not integrity.ok:
                raise DatabaseIntegrityError(integrity)
            selected_retention_days = retention_days
            if use_stored_retention:
                stored = SettingsRepository(connection).get_json(
                    "retention_days", retention_days
                )
                if stored is not None and (
                    isinstance(stored, bool) or not isinstance(stored, int)
                ):
                    raise ValueError("stored retention_days must be an integer or null")
                selected_retention_days = stored
            _validate_retention_days(selected_retention_days)
            repository = RetentionRepository(connection)
            retention_service = RetentionService(repository)
            if selected_retention_days is None:
                retention_result = RetentionResult(0, 0, 0)
            else:
                cutoff_ms = current_clock.now_utc_ms() - (
                    selected_retention_days * 24 * 60 * 60 * 1000
                )
                retention_result = retention_service.run(cutoff_ms=cutoff_ms)
            monitor = StorageMonitor()
            storage = monitor.inspect(database_path, disk_path=disk_path)
            if monitor.needs_capacity_protection(storage):
                capacity_result = retention_service.run_capacity_protection(
                    should_continue=lambda: monitor.needs_capacity_protection(
                        monitor.inspect(database_path, disk_path=disk_path)
                    )
                )
                retention_result = _merge_results(retention_result, capacity_result)
                if (
                    capacity_result.deleted_input_events
                    or capacity_result.deleted_output_events
                ):
                    repository.record_audit(
                        action="capacity_retention",
                        details_json=json.dumps(
                            {
                                "deleted_input_events": (
                                    capacity_result.deleted_input_events
                                ),
                                "deleted_output_events": (
                                    capacity_result.deleted_output_events
                                ),
                            },
                            separators=(",", ":"),
                        ),
                        ts_utc_ms=current_clock.now_utc_ms(),
                    )
                storage = monitor.inspect(database_path, disk_path=disk_path)
    return MaintenanceResult(
        retention=retention_result,
        storage=storage,
        integrity=integrity,
    )


def main() -> None:
    """環境変数・CLIから定期保守を実行する。

    Args:
        なし。

    Returns:
        None。
    """
    parser = argparse.ArgumentParser(description="event logger maintenance")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
    )
    parser.add_argument("--no-retention", action="store_true")
    args = parser.parse_args()
    database_path = os.environ.get("EVENT_LOGGER_DB", "/var/lib/event-logger/logger.db")
    configured_days = os.environ.get("EVENT_LOGGER_RETENTION_DAYS")
    explicit_days = args.retention_days
    if explicit_days is None and configured_days is not None:
        explicit_days = int(configured_days)
    if args.no_retention:
        retention_days = None
    elif explicit_days is None:
        retention_days = 365
    else:
        retention_days = explicit_days
    result = run_once(
        database_path=database_path,
        retention_days=retention_days,
        use_stored_retention=not args.no_retention and explicit_days is None,
    )
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info(
        "retention input=%d output=%d carried=%d storage=%s",
        result.retention.deleted_input_events,
        result.retention.deleted_output_events,
        result.retention.carried_counter_events,
        result.storage.status.value,
    )


def _validate_retention_days(retention_days: int | None) -> None:
    """仕様で定義した保存期間だけを受け付ける。

    Args:
        retention_days: 保存期間（日）。Noneなら通常retentionを行わない。

    Returns:
        None。

    Raises:
        ValueError: 引数の形式・範囲が仕様外、または現在
            の状態では要求を適用できない場合。
    """
    if retention_days is not None and retention_days not in RETENTION_CHOICES:
        raise ValueError(f"retention_days must be one of {RETENTION_CHOICES}")


def _merge_results(first: RetentionResult, second: RetentionResult) -> RetentionResult:
    """通常retentionと容量保護の結果を合算する。

    Args:
        first: 先行するretention処理結果。
        second: 後続する容量保護処理結果。

    Returns:
        2つのretention結果の削除数とvacuum所要時間を合算した値。
    """
    return RetentionResult(
        deleted_input_events=(first.deleted_input_events + second.deleted_input_events),
        deleted_output_events=(
            first.deleted_output_events + second.deleted_output_events
        ),
        carried_counter_events=(
            first.carried_counter_events + second.carried_counter_events
        ),
    )


if __name__ == "__main__":
    main()
