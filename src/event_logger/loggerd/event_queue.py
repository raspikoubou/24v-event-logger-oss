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


# 待機中だけでなくDB書込み中の項目も未保存容量へ数える。
class EventQueue:
    """GPIO callback・出力loopとSQLite writerの間の有界Queue。"""

    def __init__(self, maxsize: int = 10_000) -> None:
        """指定容量のQueueを作成する。"""
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
        """commit中を含む未保存eventの保持上限を返す。"""
        return self._maxsize

    def put(self, event: PendingDatabaseEvent) -> None:
        """eventを待たずにQueueへ入れる。

        SQLiteの同期書込みが遅くてもGPIO callbackとHeartbeat loopを止めない。
        未commit eventを有界に保ち、満杯時は明示的にCriticalへ遷移させる。
        """
        with self._condition:
            if len(self._queue) + self._in_flight >= self._maxsize:
                raise EventQueueFull("event queue is full")
            self._queue.append(event)
            self._queued_at_ns.append(monotonic_ns())
            self._condition.notify_all()

    def get(self) -> PendingDatabaseEvent:
        """先頭eventを待たずに取り出し、未commit数へ含める。"""
        with self._condition:
            if not self._queue:
                raise IndexError("event queue is empty")
            return self._take_locked()

    def wait_get(self, timeout_s: float) -> PendingDatabaseEvent | None:
        """eventを待って取り出す。timeout経過時はNoneを返す。"""
        with self._condition:
            if not self._queue:
                self._condition.wait(timeout_s)
            if not self._queue:
                return None
            return self._take_locked()

    # 一定件数をまとめてcommitし、平常時に意図的なバッチ待ち時間を追加しない。
    def wait_get_batch(
        self,
        *,
        max_count: int,
        timeout_s: float,
    ) -> list[PendingDatabaseEvent]:
        """先頭eventを待ち、現在Queueにある範囲をまとめて取り出す。

        新しいeventを待つ追加delayは設けない。SD同期が遅れてQueueに溜まった
        eventだけを一 transactionへまとめ、通常時の保存待ちを増やさない。
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
        """正常commitしたeventを未commit数から除く。"""
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
        """保存失敗したin-flight eventを先頭へ戻す。"""
        self.requeue_front_many((event,))

    # 保存失敗後は元のFIFO順序と投入時刻を両方戻し、待ち時間の監視を維持する。
    def requeue_front_many(self, events: Sequence[PendingDatabaseEvent]) -> None:
        """失敗したbatch全体を元のFIFO順でQueue先頭へ戻す。"""
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

    # Queueが空でもcommit中の項目が残る間は完了とみなさない。
    def wait_until_idle(self, timeout_s: float | None = None) -> bool:
        """Queueとwriter処理中eventがなくなるまで待つ。"""
        deadline = None if timeout_s is None else monotonic() + timeout_s
        with self._condition:
            while self._queue or self._in_flight:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def wake_waiters(self) -> None:
        """writer終了要求を反映するため、待機threadを起こす。"""
        with self._condition:
            self._condition.notify_all()

    def __len__(self) -> int:
        """Queue内とcommit中を合わせた未commit event数を返す。"""
        with self._condition:
            return len(self._queue) + self._in_flight

    def oldest_pending_ms(self) -> int:
        """commit中を含め、最古の未保存eventの待ち時間を返す。"""
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
        """Condition lock保持中に先頭eventを未commit処理へ移す。"""
        event = self._queue.popleft()
        self._in_flight_at_ns.append(self._queued_at_ns.popleft())
        self._in_flight += 1
        return event
