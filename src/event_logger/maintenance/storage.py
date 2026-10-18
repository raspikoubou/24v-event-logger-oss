"""DBとファイルシステムの容量状態を判定するadapter。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class StorageStatus(StrEnum):
    """ユーザー画面へ表示する容量状態。"""

    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    """ファイルシステムとDBの容量測定結果。"""

    total_bytes: int
    used_bytes: int
    free_bytes: int
    db_bytes: int
    status: StorageStatus

    @property
    def free_ratio(self) -> float:
        """空き容量の割合を0〜1で返す。

        Args:
            なし。

        Returns:
            filesystem総容量に対する空き容量比（0〜1）。総容量0なら0.0。
        """
        if self.total_bytes <= 0:
            return 0.0
        return self.free_bytes / self.total_bytes


class StorageMonitor:
    """空き容量閾値を仕様固定値として判定する。"""

    WARNING_FREE_RATIO = 0.30
    CRITICAL_FREE_RATIO = 0.20
    MAX_DATABASE_RATIO = 0.40

    def inspect(
        self,
        db_path: str | Path,
        *,
        disk_path: str | Path | None = None,
    ) -> StorageSnapshot:
        """DB配置先のfilesystemとDBファイルサイズを読む。

        Args:
            db_path: 対象SQLite databaseのpath。
            disk_path: 容量を測るfilesystem上のpath。

        Returns:
            filesystem、SQLite関連fileサイズ、容量状態のStorageSnapshot。
        """
        database_path = Path(db_path)
        if str(db_path) == ":memory:" and disk_path is None:
            usage_path = Path.cwd()
        else:
            usage_path = Path(disk_path) if disk_path is not None else database_path
        usage = shutil.disk_usage(usage_path)
        db_bytes = _database_bytes(database_path)
        free_ratio = usage.free / usage.total if usage.total else 0.0
        if free_ratio < self.CRITICAL_FREE_RATIO:
            status = StorageStatus.CRITICAL
        elif free_ratio < self.WARNING_FREE_RATIO:
            status = StorageStatus.WARNING
        else:
            status = StorageStatus.NORMAL
        return StorageSnapshot(
            total_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
            db_bytes=db_bytes,
            status=status,
        )

    def needs_capacity_protection(self, snapshot: StorageSnapshot) -> bool:
        """仕様のDB比率または空き容量閾値に達したか返す。

        Args:
            snapshot: filesystemとDB容量の測定値を持つStorageSnapshot。

        Returns:
            容量危険状態またはDB比率上限到達時にTrue。
        """
        database_ratio = (
            snapshot.db_bytes / snapshot.total_bytes if snapshot.total_bytes else 1.0
        )
        return snapshot.status is StorageStatus.CRITICAL or (
            database_ratio >= self.MAX_DATABASE_RATIO
        )


def _database_bytes(database_path: Path) -> int:
    """WAL/SHMを含むSQLite関連ファイルの容量を合算する。

    Args:
        database_path: SQLite databaseのfilesystem path。

    Returns:
        DB本体、WAL、SHM fileの合計サイズ（byte）。
    """
    total = 0
    for path in (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    ):
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total
