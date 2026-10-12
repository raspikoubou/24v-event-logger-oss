"""SQLite event Queueとsingle writer実装。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from time import monotonic_ns
from typing import Protocol

from event_logger.database.connection import connect_database
from event_logger.database.repositories import (
    EventBatchRepository,
    EventRepository,
    EventWrite,
    InputEventWrite,
    OutputEventRepository,
    OutputEventWrite,
)
from event_logger.loggerd.event_queue import (
    EventQueue,
    PendingDatabaseEvent,
    PendingInputEvent,
    PendingOutputEvent,
)

_LOGGER = logging.getLogger(__name__)
_SLOW_COMMIT_NS = 250_000_000
_WRITE_DELAY_NS = 500_000_000
_WRITE_DELAY_WINDOW_NS = 300_000_000_000
_WARNING_RECOVERY_NS = 10_000_000_000
# 通常時にflushを待たせず、既に溜まったqueueだけをまとめる上限。
_MAX_EVENT_BATCH_SIZE = 32


@dataclass(frozen=True, slots=True)
class WriteLatencySnapshot:
    """直近5分のcommit遅延と現在の警告状態。"""

    delayed_count: int
    max_delay_ms: int
    last_commit_ms: int | None
    in_progress_ms: int
    warning: bool
    severity: str


class WriteLatencyMonitor:
    """SQLite commit完了時間の直近5分だけを記録する。"""

    def __init__(self) -> None:
        """イベント時刻ではなくmonotonic時計で監視窓を管理する。"""
        self._delays: deque[tuple[int, int]] = deque()
        self._last_commit_ms: int | None = None
        self._warning = False
        self._healthy_since_ns: int | None = None

    def record(self, *, completed_ns: int, elapsed_ns: int) -> None:
        """500ms以上のcommitを記録し、古い測定値を破棄する。"""
        self._last_commit_ms = elapsed_ns // 1_000_000
        self._expire(completed_ns)
        if elapsed_ns >= _WRITE_DELAY_NS:
            self._delays.append((completed_ns, elapsed_ns))

    def snapshot(
        self,
        *,
        now_ns: int,
        in_progress_ms: int = 0,
        oldest_pending_ms: int = 0,
        pending_count: int = 0,
        capacity: int = 10_000,
        failed: bool = False,
    ) -> WriteLatencySnapshot:
        """未保存の危険度を判定し、回復10秒で警告を解除する。"""
        self._expire(now_ns)
        # 5秒は正常負荷の最大保存追従時間。容量80%では残り20%を
        # 回復余地として残す。commit回数だけでは欠損とは判断しない。
        unhealthy = (
            failed
            or in_progress_ms >= 5_000
            or oldest_pending_ms >= 5_000
            or pending_count * 5 >= capacity * 4
        )
        recovered = (
            not failed
            and in_progress_ms < 1_000
            and oldest_pending_ms < 1_000
            and pending_count * 2 < capacity
        )
        if unhealthy:
            self._warning = True
            self._healthy_since_ns = None
        elif self._warning:
            if not recovered:
                self._healthy_since_ns = None
            elif self._healthy_since_ns is None:
                self._healthy_since_ns = now_ns
            elif now_ns - self._healthy_since_ns >= _WARNING_RECOVERY_NS:
                self._warning = False
                self._healthy_since_ns = None
        severity = (
            "warning"
            if self._warning
            else (
                "attention"
                if self._delays or in_progress_ms >= 500 or oldest_pending_ms >= 500
                else "normal"
            )
        )
        return WriteLatencySnapshot(
            delayed_count=len(self._delays),
            max_delay_ms=max((delay for _, delay in self._delays), default=0)
            // 1_000_000,
            last_commit_ms=self._last_commit_ms,
            in_progress_ms=in_progress_ms,
            warning=self._warning,
            severity=severity,
        )

    def _expire(self, now_ns: int) -> None:
        """表示と警告を過去のカード状態へ固定しない。"""
        while self._delays and now_ns - self._delays[0][0] >= _WRITE_DELAY_WINDOW_NS:
            self._delays.popleft()


class EventWriterPort(Protocol):
    """loggerdがeventを投入・flushするwriter境界。"""

    def enqueue_input(self, event: PendingInputEvent) -> None:
        """入力eventをcommit Queueへ入れる。"""

    def drain_all(self) -> int:
        """writerの状態を確認し、完了済み件数を返す。"""

    def wait_until_drained(self) -> None:
        """Queue内のeventがcommitされるまで待つ。"""


class EventWriter:
    """Queue内のeventを同期的に保存するwriter。"""

    def __init__(
        self,
        event_repository: EventRepository,
        event_queue: EventQueue,
        output_repository: OutputEventRepository | None = None,
    ) -> None:
        """RepositoryとQueueを受け取る。

        LoggerServiceの単体テストで接続を直接注入するときに使う。
        実daemonはBackgroundEventWriterを使い、制御loopからDB I/Oを分離する。
        """
        self._event_repository = event_repository
        self._event_queue = event_queue
        self._output_repository = output_repository

    def enqueue_input(self, event: PendingInputEvent) -> None:
        """入力eventをQueueへ投入する。"""
        self._event_queue.put(event)

    def enqueue_output(self, event: PendingOutputEvent) -> None:
        """出力eventをQueueへ投入する。"""
        self._event_queue.put(event)

    def drain_once(self) -> bool:
        """Queue先頭のeventを1件保存する。

        Returns:
            eventを1件処理した場合はTrue、Queueが空ならFalse。

        Raises:
            sqlite3.Error: eventの保存に失敗した場合。呼び出し側がCriticalへ
                遷移し、Heartbeatを停止する責務を持つ。
        """
        try:
            pending = self._event_queue.get()
        except IndexError:
            return False
        try:
            self.append_pending(pending)
        except Exception:
            self._event_queue.requeue_front(pending)
            raise
        self._event_queue.task_done()
        return True

    def drain_all(self) -> int:
        """現在Queueにあるeventを順番に保存する。"""
        count = 0
        while self.drain_once():
            count += 1
        return count

    def wait_until_drained(self) -> None:
        """同期writerではQueueをその場で空にする。"""
        self.drain_all()

    def append_pending(self, pending: PendingDatabaseEvent) -> None:
        """event種別に対応するRepositoryへ1件commitする。"""
        if isinstance(pending, PendingInputEvent):
            self._event_repository.append(
                session_id=pending.session_id,
                ts_utc_ms=pending.ts_utc_ms,
                mono_ns=pending.mono_ns,
                channel_id=pending.channel_id,
                value=pending.value,
                kind=pending.kind,
                clock_synced=pending.clock_synced,
            )
            return
        if self._output_repository is None:
            raise RuntimeError("output event repository is not configured")
        self._output_repository.append(
            session_id=pending.session_id,
            ts_utc_ms=pending.ts_utc_ms,
            mono_ns=pending.mono_ns,
            output_channel=pending.channel_id,
            value=pending.value,
            source=pending.source,
            clock_synced=pending.clock_synced,
        )


class BackgroundEventWriter:
    """制御loopと別threadでWAL/FULL event commitを直列実行する。"""

    def __init__(
        self,
        *,
        database_path: str | Path,
        event_queue: EventQueue,
    ) -> None:
        """DB pathと共有Queueを受け取る。threadはstart()まで作成しない。"""
        self._database_path = str(database_path)
        self._event_queue = event_queue
        self._stop_requested = threading.Event()
        self._ready = threading.Event()
        self._state_lock = threading.Lock()
        self._failure: Exception | None = None
        self._started = False
        self._thread: threading.Thread | None = None
        self._completed = 0
        self._latency_monitor = WriteLatencyMonitor()
        self._commit_started_ns: int | None = None

    def latency_snapshot(self) -> WriteLatencySnapshot:
        """writer threadと共有する遅延測定値を安全に返す。"""
        with self._state_lock:
            now_ns = monotonic_ns()
            in_progress_ms = (
                0
                if self._commit_started_ns is None
                else (now_ns - self._commit_started_ns) // 1_000_000
            )
            return self._latency_monitor.snapshot(
                now_ns=now_ns,
                in_progress_ms=in_progress_ms,
                oldest_pending_ms=self._event_queue.oldest_pending_ms(),
                pending_count=len(self._event_queue),
                capacity=self._event_queue.capacity,
                failed=self._failure is not None,
            )

    def start(self) -> None:
        """thread内でSQLite接続を作ってからwriterを有効にする。"""
        with self._state_lock:
            if self._started:
                raise RuntimeError("event writer is already started")
            self._started = True
            self._thread = threading.Thread(
                target=self._run,
                name="event-logger-db-writer",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=6.0):
            raise TimeoutError("SQLite event writer did not become ready")
        self.raise_if_failed()

    def enqueue_input(self, event: PendingInputEvent) -> None:
        """入力eventを待たずにQueueへ追加する。"""
        self.raise_if_failed()
        self._event_queue.put(event)

    def enqueue_output(self, event: PendingOutputEvent) -> None:
        """出力eventを待たずにQueueへ追加する。"""
        self.raise_if_failed()
        self._event_queue.put(event)

    def drain_all(self) -> int:
        """失敗状態を確認し、直近にcommitした件数を返す。

        このmethodはQueueを待たない。loggerd loopから呼んでもSQLite latencyで
        Heartbeatの周期を止めないための境界である。
        """
        self.raise_if_failed()
        with self._state_lock:
            completed = self._completed
            self._completed = 0
        return completed

    def wait_until_drained(self) -> None:
        """停止処理向けに全eventのcommitを待つ。"""
        while not self._event_queue.wait_until_idle(timeout_s=0.1):
            self.raise_if_failed()
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        """workerが保存不能になった場合、loggerdへ失敗を返す。"""
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise RuntimeError("SQLite event writer failed") from failure

    def stop(self) -> None:
        """残eventを処理してworkerを終了する。"""
        with self._state_lock:
            thread = self._thread
        if thread is None:
            return
        self._stop_requested.set()
        self._event_queue.wake_waiters()
        thread.join()
        with self._state_lock:
            self._thread = None
        self.raise_if_failed()

    def _run(self) -> None:
        """thread所有のSQLite接続でeventをFIFO保存する。"""
        connection: sqlite3.Connection | None = None
        try:
            connection = connect_database(self._database_path)
            batch_repository = EventBatchRepository(connection)
        except Exception as error:
            self._set_failure(error)
            self._ready.set()
            if connection is not None:
                connection.close()
            return
        self._ready.set()
        try:
            while True:
                pending_events = self._event_queue.wait_get_batch(
                    max_count=_MAX_EVENT_BATCH_SIZE,
                    timeout_s=0.1,
                )
                if not pending_events:
                    if self._stop_requested.is_set() and len(self._event_queue) == 0:
                        return
                    continue
                try:
                    commit_started_ns = monotonic_ns()
                    with self._state_lock:
                        self._commit_started_ns = commit_started_ns
                    batch_repository.append_many(
                        [_event_write(pending) for pending in pending_events]
                    )
                except Exception as error:
                    with self._state_lock:
                        self._commit_started_ns = None
                    self._event_queue.requeue_front_many(pending_events)
                    self._set_failure(error)
                    return
                commit_elapsed_ns = monotonic_ns() - commit_started_ns
                if commit_elapsed_ns >= _SLOW_COMMIT_NS:
                    _log_slow_commit(
                        pending_events[0],
                        commit_elapsed_ns,
                        pending_count=len(self._event_queue),
                        batch_count=len(pending_events),
                    )
                self._event_queue.task_done(len(pending_events))
                with self._state_lock:
                    self._commit_started_ns = None
                    self._completed += len(pending_events)
                    self._latency_monitor.record(
                        completed_ns=monotonic_ns(), elapsed_ns=commit_elapsed_ns
                    )
        finally:
            connection.close()

    def _set_failure(self, error: Exception) -> None:
        """失敗を保持し、daemon側の次回loopでCriticalへ伝える。"""
        with self._state_lock:
            if self._failure is None:
                self._failure = error
        _LOGGER.exception("SQLite event writer stopped after a persistence error")


def _log_slow_commit(
    event: PendingDatabaseEvent,
    elapsed_ns: int,
    *,
    pending_count: int,
    batch_count: int = 1,
) -> None:
    """DB workerで遅れたcommitを記録する。"""
    if isinstance(event, PendingInputEvent):
        event_kind = "input"
        channel_id = event.channel_id
        source = event.kind.name.lower()
    else:
        event_kind = "output"
        channel_id = event.channel_id
        source = event.source.value
    _LOGGER.warning(
        "slow database event commit: kind=%s channel=%s value=%s source=%s "
        "elapsed_ms=%s pending_events=%s batch_events=%s",
        event_kind,
        channel_id,
        event.value,
        source,
        elapsed_ns // 1_000_000,
        pending_count,
        batch_count,
    )


def _event_write(event: PendingDatabaseEvent) -> EventWrite:
    """Queue eventをDB writer向けの不変値へ変換する。"""
    if isinstance(event, PendingInputEvent):
        return InputEventWrite(
            session_id=event.session_id,
            ts_utc_ms=event.ts_utc_ms,
            mono_ns=event.mono_ns,
            channel_id=event.channel_id,
            value=event.value,
            kind=event.kind,
            clock_synced=event.clock_synced,
        )
    return OutputEventWrite(
        session_id=event.session_id,
        ts_utc_ms=event.ts_utc_ms,
        mono_ns=event.mono_ns,
        output_channel=event.channel_id,
        value=event.value,
        source=event.source,
        clock_synced=event.clock_synced,
    )
