"""一次イベントを壊さずに段階削除するretention処理。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from event_logger.database.repositories import RetentionRepository

_WRITER_YIELD_SECONDS = 0.1


def _yield_for_database_writer() -> None:
    """段階削除のtransaction間でloggerd writerへDB取得機会を渡す。

    Args:
        なし。

    Returns:
        None。
    """
    # SQLiteは複数writerを直列化する。maintenanceが10,000件batchを
    # 連続取得すると、待機中のloggerdが次batchに負け続けてbusy timeoutへ
    # 達するPi実測があった。commit後に短いscheduler yieldを入れ、一次入力の
    # writerが各batch境界でDBを取得できるようにする。
    time.sleep(_WRITER_YIELD_SECONDS)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """1回のretention実行結果。"""

    deleted_input_events: int
    deleted_output_events: int
    carried_counter_events: int


class RetentionService:
    """10,000件以下のbatchを繰り返して古い一次eventを削除する。"""

    def __init__(
        self,
        repository: RetentionRepository,
        *,
        batch_size: int = 10_000,
    ) -> None:
        """retention repositoryとbatch sizeを受け取る。

        Args:
            repository: この処理が利用する保存・照会Repository。
            batch_size: 一度のretention transactionで削除する最大row数。

        Returns:
            None。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._repository = repository
        self._batch_size = batch_size

    def run(self, *, cutoff_ms: int) -> RetentionResult:
        """cutoffより古いIN/OUT eventを削除し、最後にincremental vacuumする。

        Args:
            cutoff_ms: これより古いeventを削除対象にするUTC epoch境界（ms）。

        Returns:
            cutoff以前の削除数とvacuum所要時間を示すRetentionResult。
        """
        deleted_input = 0
        deleted_output = 0
        carried = 0
        # 一batchずつ削除してDB writerへ譲る。返却された削除数を積み上げ、
        # 途中batch失敗はrepository例外のまま呼出元へ伝播させる。
        while True:
            result = self._repository.delete_old_batch(
                cutoff_ms=cutoff_ms,
                batch_size=self._batch_size,
            )
            if result is None:
                break
            input_count, output_count, carried_count = result
            deleted_input += input_count
            deleted_output += output_count
            carried += carried_count
            # counter carry-forwardだけが起きたbatchで無限loopしないよう、
            # event削除が0件なら追加batchを要求せず終える。
            if input_count == 0 and output_count == 0:
                break
            _yield_for_database_writer()
        # 全削除後の最後にvacuumを試し、削除数集計とvacuum時間を返す。
        self._repository.incremental_vacuum()
        return RetentionResult(
            deleted_input_events=deleted_input,
            deleted_output_events=deleted_output,
            carried_counter_events=carried,
        )

    def run_capacity_protection(
        self,
        *,
        should_continue: Callable[[], bool],
    ) -> RetentionResult:
        """容量閾値を回復するまで古いeventを追加でbatch削除する。

        Args:
            should_continue: 容量保護の次batchを続けるか判定する関数。

        Returns:
            容量条件解消まで削除したevent数のRetentionResult。
        """
        deleted_input = 0
        deleted_output = 0
        carried = 0
        while should_continue():
            result = self._repository.delete_old_batch(
                cutoff_ms=None,
                batch_size=self._batch_size,
            )
            if result is None:
                break
            input_count, output_count, carried_count = result
            deleted_input += input_count
            deleted_output += output_count
            carried += carried_count
            _yield_for_database_writer()
            # 容量保護では次のbatch判定前に、削除で生じたfreelistとWALを
            # 可能な範囲で実ファイルへ反映する。最後に一度だけvacuumすると
            # DBサイズが縮まず、同じ閾値のまま全eventを削除する危険がある。
            if not self._repository.incremental_vacuum():
                # readerがWALを保持している間は、削除しても監視対象の容量が
                # 回復しない。再実行でcheckpointできるまで一次eventを追加で
                # 消さず、全event削除を防ぐ。
                break
        self._repository.incremental_vacuum()
        return RetentionResult(
            deleted_input_events=deleted_input,
            deleted_output_events=deleted_output,
            carried_counter_events=carried,
        )
