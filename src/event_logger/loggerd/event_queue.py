"""GPIO callbackとSQLite writerの間の有界Queue。"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import Condition, Event
from time import monotonic, monotonic_ns

from event_logger.common.models import CounterReset, EventKind, OutputSource
from event_logger.domain.widgets import Widget


@dataclass(frozen=True, slots=True)
class PendingInputEvent:
    """DB writerが保存する確定入力event。"""

    session_id: int
    ts_utc_ms: int
    mono_ns: int
    channel_id: int
    value: int
    kind: EventKind = EventKind.EDGE
    clock_synced: bool = False


@dataclass(frozen=True, slots=True)
class PendingOutputEvent:
    """専用DB writerが保存する出力履歴event。"""

    session_id: int | None
    ts_utc_ms: int
    mono_ns: int
    channel_id: int
    value: int
    source: OutputSource
    clock_synced: bool


@dataclass(slots=True)
class PendingCounterReset:
    """先行eventのcommit後に保存するreset。Eventでworkerの結果を公開する。"""

    widget_id: str
    reset_at_ms: int
    widget: Widget | None = None
    completed: Event = field(default_factory=Event)
    result: CounterReset | None = None
    error: Exception | None = None


type PendingDatabaseEvent = PendingInputEvent | PendingOutputEvent | PendingCounterReset


class EventQueueFull(RuntimeError):
    """Queueが満杯でeventを保持できなかった。"""


class EventQueue:
    """GPIO callback・出力loopとSQLite writerの間の有界Queue。"""

    def __init__(self, maxsize: int = 10_000) -> None:
        """指定容量のQueueを作成する。

        Args:
            maxsize: Queueが保持できる未commit eventの最大数。

        Returns:
            None。Queueとin-flight時刻を初期化する。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self._maxsize = maxsize
        self._queue: deque[PendingDatabaseEvent] = deque()
        self._queued_at_ns: deque[int] = deque()
        self._in_flight = 0
        self._in_flight_at_ns: deque[int] = deque()
        self._condition = Condition()

    @property
    def capacity(self) -> int:
        """commit中を含む未保存eventの保持上限を返す。

        Args:
            なし。

        Returns:
            commit中を含む未保存eventの上限件数。
        """
        return self._maxsize

    def put(self, event: PendingDatabaseEvent) -> None:
        """eventを待たずにQueueへ入れる。

        SQLiteの同期書込みが遅くてもGPIO callbackとHeartbeat loopを止めない。
        未commit eventを有界に保ち、満杯時は明示的にCriticalへ遷移させる。

        Args:
            event: FIFOへ追加、保存、または型変換する確定event。

        Returns:
            None。eventとenqueue時刻をFIFOへ追加し、waiterを起こす。

        Raises:
            EventQueueFull: 未commit eventが有界Queueの容量を使い切った場合。
        """
        with self._condition:
            if len(self._queue) + self._in_flight >= self._maxsize:
                raise EventQueueFull("event queue is full")
            self._queue.append(event)
            self._queued_at_ns.append(monotonic_ns())
            self._condition.notify_all()

    def get(self) -> PendingDatabaseEvent:
        """先頭eventを待たずに取り出し、未commit数へ含める。

        Args:
            なし。

        Returns:
            FIFOの先頭から移した未commit event。Queueが空ならIndexErrorを送出する。

        Raises:
            IndexError: 空のQueueから即時取得しようとした場合。
        """
        with self._condition:
            if not self._queue:
                raise IndexError("event queue is empty")
            return self._take_locked()

    def wait_get(self, timeout_s: float) -> PendingDatabaseEvent | None:
        """eventを待って取り出す。timeout経過時はNoneを返す。

        Args:
            timeout_s: 最大待機時間（秒）。Noneは時間制限なし。

        Returns:
            FIFO先頭event。指定秒数内に届かなければNone。
        """
        with self._condition:
            if not self._queue:
                self._condition.wait(timeout_s)
            if not self._queue:
                return None
            return self._take_locked()

    def wait_get_batch(
        self,
        *,
        max_count: int,
        timeout_s: float,
    ) -> list[PendingDatabaseEvent]:
        """先頭eventを待ち、現在Queueにある範囲をまとめて取り出す。

        新しいeventを待つ追加delayは設けない。SD同期が遅れてQueueに溜まった
        eventだけを一 transactionへまとめ、通常時の保存待ちを増やさない。

        Args:
            max_count: 一回に取り出すevent数の上限。
            timeout_s: 最大待機時間（秒）。Noneは時間制限なし。

        Returns:
            FIFO順に取り出した最大max_count件のevent列。timeout時は空列の場合がある。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        if max_count < 1:
            raise ValueError("max_count must be positive")
        with self._condition:
            if not self._queue:
                self._condition.wait(timeout_s)
            batch: list[PendingDatabaseEvent] = []
            while self._queue and len(batch) < max_count:
                batch.append(self._queue.popleft())
                self._in_flight_at_ns.append(self._queued_at_ns.popleft())
            self._in_flight += len(batch)
            return batch

    def task_done(self, count: int = 1) -> None:
        """正常commitしたeventを未commit数から除く。

        Args:
            count: 処理済みとして通知するevent数。1以上を指定する。

        Returns:
            None。指定数のin-flight eventを完了扱いにしてidle waiterを起こす。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        with self._condition:
            if count < 1:
                raise ValueError("count must be positive")
            if self._in_flight < count:
                raise RuntimeError("event queue has no in-flight event")
            self._in_flight -= count
            for _ in range(count):
                self._in_flight_at_ns.popleft()
            self._condition.notify_all()

    def requeue_front(self, event: PendingDatabaseEvent) -> None:
        """保存失敗したin-flight eventを先頭へ戻す。

        Args:
            event: FIFOへ追加、保存、または型変換する確定event。

        Returns:
            None。失敗eventをFIFOの先頭へ戻す。
        """
        self.requeue_front_many((event,))

    def requeue_front_many(self, events: Sequence[PendingDatabaseEvent]) -> None:
        """失敗したbatch全体を元のFIFO順でQueue先頭へ戻す。

        Args:
            events: FIFO順で保存または差し戻す未commit event列。

        Returns:
            None。失敗batchとenqueue時刻を元の順序でFIFO先頭へ戻す。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        with self._condition:
            count = len(events)
            if count < 1:
                raise ValueError("events must not be empty")
            if self._in_flight < count:
                raise RuntimeError("event queue has no in-flight event")
            self._in_flight -= count
            for enqueued_at_ns in reversed(
                [self._in_flight_at_ns.popleft() for _ in range(count)]
            ):
                self._queued_at_ns.appendleft(enqueued_at_ns)
            self._queue.extendleft(reversed(events))
            self._condition.notify_all()

    def wait_until_idle(self, timeout_s: float | None = None) -> bool:
        """Queueとwriter処理中eventがなくなるまで待つ。

        Args:
            timeout_s: 最大待機時間（秒）。Noneは時間制限なし。

        Returns:
            Queueとin-flight eventが空ならTrue、期限までに空にならなければFalse。
        """
        deadline = None if timeout_s is None else monotonic() + timeout_s
        with self._condition:
            while self._queue or self._in_flight:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def wake_waiters(self) -> None:
        """writer終了要求を反映するため、待機threadを起こす。

        Args:
            なし。

        Returns:
            None。Conditionで待機しているwriter threadを起こす。
        """
        with self._condition:
            self._condition.notify_all()

    def __len__(self) -> int:
        """Queue内とcommit中を合わせた未commit event数を返す。

        Args:
            なし。

        Returns:
            Queue待機中とcommit中を合算した未commit event件数。
        """
        with self._condition:
            return len(self._queue) + self._in_flight

    def oldest_pending_ms(self) -> int:
        """commit中を含め、最古の未保存eventの待ち時間を返す。

        Args:
            なし。

        Returns:
            最古の未commit eventの待ち時間（ms）。eventなしなら0。
        """
        with self._condition:
            oldest = (
                self._in_flight_at_ns[0]
                if self._in_flight_at_ns
                else self._queued_at_ns[0]
                if self._queued_at_ns
                else None
            )
            if oldest is None:
                return 0
            return max(0, (monotonic_ns() - oldest) // 1_000_000)

    def _take_locked(self) -> PendingDatabaseEvent:
        """Condition lock保持中に先頭eventを未commit処理へ移す。

        Args:
            なし。

        Returns:
            FIFOの先頭から移した未commit event。Queueが空ならIndexErrorを送出する。
        """
        event = self._queue.popleft()
        self._in_flight_at_ns.append(self._queued_at_ns.popleft())
        self._in_flight += 1
        return event
