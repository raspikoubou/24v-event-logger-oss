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
    CounterResetRepository,
    EventBatchRepository,
    EventRepository,
    EventWrite,
    InputEventWrite,
    OutputEventRepository,
    OutputEventWrite,
    WidgetRepository,
)
from event_logger.loggerd.event_queue import (
    EventQueue,
    PendingCounterReset,
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
        """イベント時刻ではなくmonotonic時計で監視窓を管理する。

        Args:
            なし。

        Returns:
            None。測定履歴と警告状態を初期化する。
        """
        self._delays: deque[tuple[int, int]] = deque()
        self._last_commit_ms: int | None = None
        self._warning = False
        self._healthy_since_ns: int | None = None

    def record(self, *, completed_ns: int, elapsed_ns: int) -> None:
        """500ms以上のcommitを記録し、古い測定値を破棄する。

        Args:
            completed_ns: 測定完了時点のmonotonic
                時計値（ns）。遅延を直近時間窓へ登録する基準時刻。
            elapsed_ns: commitに要したmonotonic経過時間（ns）。

        Returns:
            None。直近commit時間を更新し、500ms以上のcommitだけを遅延履歴へ記録する。
        """
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
        """未保存の危険度を判定し、回復10秒で警告を解除する。

        Args:
            now_ns: 判定時点のmonotonic時計値（ns）。
            in_progress_ms: 現在commit中の経過時間（ms）。処理中でなければ0。
            oldest_pending_ms: 最古の未commit eventの待ち時間（ms）。
            pending_count: Queue待機中とcommit中を合わせたevent数。
            capacity: 未commit eventを保持する上限件数。
            failed: writerが保存不能状態かどうか。

        Returns:
            WriteLatencySnapshot。直近5分の遅延co
            mmit、最長遅延、直近commit、処理中時間、警
            告状態を表す。
        """
        # 古いdelay sampleを窓から外してから、処理中時間・最古待ち・容量・保存障害を
        # 組み合わせて危険状態を判定する。commit件数だけでは損失とみなさない。
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
        # 危険条件は即時warning、回復条件は継続時間を満たすまで解除を保留する。
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
        # warningが解除されてもdelay履歴または短時間遅延が残ればattentionを返す。
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
        """表示と警告を過去のカード状態へ固定しない。

        Args:
            now_ns: 判定時点のmonotonic時計値（ns）。

        Returns:
            None。5分より古い遅延測定を履歴先頭から除く。
        """
        while self._delays and now_ns - self._delays[0][0] >= _WRITE_DELAY_WINDOW_NS:
            self._delays.popleft()


class EventWriterPort(Protocol):
    """loggerdがeventを投入・flushするwriter境界。"""

    def enqueue_input(self, event: PendingInputEvent) -> None:
        """入力eventをcommit Queueへ入れる。

        Args:
            event: FIFOへ追加、保存、または型変換する確定event。

        Returns:
            None。入力eventを共有Queueへ追加する。
        """

    def raise_if_failed(self) -> None:
        """保存失敗があれば例外を送出する。同期writerは常に正常とみなす。"""

    def drain_all(self) -> int:
        """writerの状態を確認し、完了済み件数を返す。

        Args:
            なし。

        Returns:
            この呼出しでcommit済みとして回収したevent件数。
        """

    def wait_until_drained(self) -> None:
        """Queue内のeventがcommitされるまで待つ。

        Args:
            なし。

        Returns:
            None。Queueが空になりwriter処理中eventも完了するまで待つ。
        """


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

        Args:
            event_repository: 入力eventの保存先Repository。
            event_queue: producerとwriterで共有する有界FIFO Queue。
            output_repository: 出力eventの保存先
                Repository。未指定時は同期writerから
                出力保存できない。

        Returns:
            None。測定履歴と警告状態を初期化する。
        """
        self._event_repository = event_repository
        self._event_queue = event_queue
        self._output_repository = output_repository

    def enqueue_input(self, event: PendingInputEvent) -> None:
        """入力eventをQueueへ投入する。

        Args:
            event: FIFOへ追加、保存、または型変換する確定event。

        Returns:
            None。入力eventを共有Queueへ追加する。
        """
        self._event_queue.put(event)

    def enqueue_output(self, event: PendingOutputEvent) -> None:
        """出力eventをQueueへ投入する。

        Args:
            event: FIFOへ追加、保存、または型変換する確定event。

        Returns:
            None。出力eventを共有Queueへ追加する。
        """
        self._event_queue.put(event)

    def drain_once(self) -> bool:
        """Queue先頭のeventを1件保存する。

        Returns:
            eventを1件処理した場合はTrue、Queueが空ならFalse。

        Raises:
                sqlite3.Error: eventの保存に失敗した場合。呼び出し側がCriticalへ
                遷移し、Heartbeatを停止する責務を持つ。

        Args:
            なし。
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

    def raise_if_failed(self) -> None:
        """同期writerはエラーを保持せず、その場の書き込みで例外を返す。"""
        return

    def drain_all(self) -> int:
        """現在Queueにあるeventを順番に保存する。

        Args:
            なし。

        Returns:
            この呼出しでcommit済みとして回収したevent件数。
        """
        count = 0
        while self.drain_once():
            count += 1
        return count

    def wait_until_drained(self) -> None:
        """同期writerではQueueをその場で空にする。

        Args:
            なし。

        Returns:
            None。Queueが空になりwriter処理中eventも完了するまで待つ。
        """
        self.drain_all()

    def append_pending(self, pending: PendingDatabaseEvent) -> None:
        """event種別に対応するRepositoryへ1件commitする。

        Args:
            pending: Queue上の位置に保存する未commit event。

        Returns:
            None。event種別に対応するrepositoryへ1件保存する。

        Raises:
            TypeError: 受け取った値の型またはevent種別がこの処理の契約に合わない場合。
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        if isinstance(pending, PendingCounterReset):
            raise TypeError("counter reset requires the background writer")
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
        """DB pathと共有Queueを受け取る。threadはstart()まで作成しない。

        Args:
            database_path: SQLite databaseのfilesystem path。
            event_queue: producerとwriterで共有する有界FIFO Queue。

        Returns:
            None。測定履歴と警告状態を初期化する。
        """
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
        """writer threadと共有する遅延測定値を安全に返す。

        Args:
            なし。

        Returns:
            遅延、最古eventの滞留、未commit数を同時点で観測したWriteLatencySnapshot。
        """
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
        """thread内でSQLite接続を作ってからwriterを有効にする。

        Args:
            なし。

        Returns:
            None。SQLite接続準備を確認してからbackground writerを稼働状態にする。

        Raises:
            TimeoutError: 指定timeout内にworkerまたはserviceの準備が完了しない場合。
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
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
        """入力eventを待たずにQueueへ追加する。

        Args:
            event: FIFOへ追加、保存、または型変換する確定event。

        Returns:
            None。入力eventを共有Queueへ追加する。
        """
        self.raise_if_failed()
        self._event_queue.put(event)

    def enqueue_output(self, event: PendingOutputEvent) -> None:
        """出力eventを待たずにQueueへ追加する。

        Args:
            event: FIFOへ追加、保存、または型変換する確定event。

        Returns:
            None。出力eventを共有Queueへ追加する。
        """
        self.raise_if_failed()
        self._event_queue.put(event)

    def drain_all(self) -> int:
        """失敗状態を確認し、直近にcommitした件数を返す。

        このmethodはQueueを待たない。loggerd loopから呼んでもSQLite latencyで
        Heartbeatの周期を止めないための境界である。

        Args:
            なし。

        Returns:
            この呼出しでcommit済みとして回収したevent件数。
        """
        self.raise_if_failed()
        with self._state_lock:
            completed = self._completed
            self._completed = 0
        return completed

    def wait_until_drained(self) -> None:
        """停止処理向けに全eventのcommitを待つ。

        Args:
            なし。

        Returns:
            None。Queueが空になりwriter処理中eventも完了するまで待つ。
        """
        while not self._event_queue.wait_until_idle(timeout_s=0.1):
            self.raise_if_failed()
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        """workerが保存不能になった場合、loggerdへ失敗を返す。

        Args:
            なし。

        Returns:
            None。失敗がなければ復帰し、保存不能ならRuntimeErrorを送出する。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise RuntimeError("SQLite event writer failed") from failure

    def stop(self) -> None:
        """残eventを処理してworkerを終了する。

        Args:
            なし。

        Returns:
            None。残eventの処理完了後にwriter threadをjoinする。
        """
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
        """thread所有のSQLite接続でeventをFIFO保存する。

        Args:
            なし。

        Returns:
            None。専用connectionでFIFO batchをcommitし、障害時は失敗状態を保存する。
        """
        connection: sqlite3.Connection | None = None
        try:
            # 専用threadに接続を作る。SQLite connectionを別threadと共有せず、
            # writerの全DB操作を同じthreadへ閉じ込める。
            connection = connect_database(self._database_path)
            batch_repository = EventBatchRepository(connection)
        except Exception as error:
            # 失敗を記録してready待ちを解除し、startup callerに原因を伝える。
            self._set_failure(error)
            self._ready.set()
            if connection is not None:
                connection.close()
            return
        # DB準備後にreadyを通知し、以降の保存障害はfailureで知らせる。
        self._ready.set()
        try:
            while True:
                # 有限timeoutでstop flagを再確認し、wake通知を取りこぼしても
                # 終了可能にする。
                pending_events = self._event_queue.wait_get_batch(
                    max_count=_MAX_EVENT_BATCH_SIZE,
                    timeout_s=0.1,
                )
                if not pending_events:
                    # batchなしならqueue状態を見て停止を判定する。停止要求だけで
                    # 受付済みeventを捨てず、未emptyなら次のwaitへ戻る。
                    if self._stop_requested.is_set() and len(self._event_queue) == 0:
                        return
                    continue
                remaining = list(pending_events)
                try:
                    # resetを含むbatchはその前後で分割し、確定入力と同じFIFOの
                    # 位置へ基準を保存する。制御loopではDBの完了を待たない。
                    while remaining:
                        if isinstance(remaining[0], PendingCounterReset):
                            self._apply_counter_reset(connection, remaining[0])
                            self._event_queue.task_done()
                            remaining = remaining[1:]
                            continue
                        count = next(
                            (
                                index
                                for index, pending in enumerate(remaining)
                                if isinstance(pending, PendingCounterReset)
                            ),
                            len(remaining),
                        )
                        self._commit_event_batch(batch_repository, remaining[:count])
                        remaining = remaining[count:]
                except Exception as error:
                    # 未保存eventを元の順序で先頭へ戻してからwriter障害を記録し、
                    # 後続eventが失敗分を追い越さないよう、このthreadを終了する。
                    with self._state_lock:
                        self._commit_started_ns = None
                    self._event_queue.requeue_front_many(remaining)
                    self._set_failure(error)
                    return
        finally:
            # batch処理の正常return・commit失敗のどちらでもconnectionを閉じる。
            connection.close()

    def _commit_event_batch(
        self, repository: EventBatchRepository, events: list[PendingDatabaseEvent]
    ) -> None:
        """resetをまたがないevent batchを保存し、未commit数と遅延を更新する。

        Args:
            repository: この処理が利用する保存・照会Repository。
            events: FIFO順で保存または差し戻す未commit event列。

        Returns:
            None。batch commit後にQueue件数、遅延測定、完了件数を更新する。
        """
        started_ns = monotonic_ns()
        with self._state_lock:
            self._commit_started_ns = started_ns
        repository.append_many([_event_write(event) for event in events])
        elapsed_ns = monotonic_ns() - started_ns
        if elapsed_ns >= _SLOW_COMMIT_NS:
            _log_slow_commit(
                events[0],
                elapsed_ns,
                pending_count=len(self._event_queue),
                batch_count=len(events),
            )
        self._event_queue.task_done(len(events))
        with self._state_lock:
            self._commit_started_ns = None
            self._completed += len(events)
            self._latency_monitor.record(
                completed_ns=monotonic_ns(), elapsed_ns=elapsed_ns
            )

    @staticmethod
    def _apply_counter_reset(
        connection: sqlite3.Connection, pending: PendingCounterReset
    ) -> None:
        """FIFOの位置でresetをcommitし、対象変更時はwidgetも原子的に保存する。

        Args:
            connection: この関数が使うSQLite connection。
            pending: Queue上の位置に保存する未commit event。

        Returns:
            None。FIFO位置でresetを保存し、結果または要求失敗をpendingへ通知する。
        """
        try:
            repository = CounterResetRepository(connection)
            if pending.widget is None:
                pending.result = repository.create(
                    widget_id=pending.widget_id, reset_at_ms=pending.reset_at_ms
                )
            else:
                WidgetRepository(connection).update_with_counter_reset(
                    pending.widget, reset_at_ms=pending.reset_at_ms
                )
                pending.result = repository.latest(pending.widget_id)
        except (ValueError, sqlite3.IntegrityError) as error:
            # 削除済みwidget等の要求拒否は、入力writer自体の保存障害ではない。
            pending.error = error
        except Exception as error:
            pending.error = error
            raise
        finally:
            pending.completed.set()

    def _set_failure(self, error: Exception) -> None:
        """失敗を保持し、daemon側の次回loopでCriticalへ伝える。

        Args:
            error: worker threadが保存不能になった原因例外。

        Returns:
            None。最初のwriter障害を保持してloggerdへ後続通知できるようにする。
        """
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
    """DB workerで遅れたcommitを記録する。

    Args:
        event: FIFOへ追加、保存、または型変換する確定event。
        elapsed_ns: commitに要したmonotonic経過時間（ns）。
        pending_count: Queue待機中とcommit中を合わせたevent数。
        batch_count: 一回のcommit batchに含めたevent件数。

    Returns:
        None。

    Raises:
        TypeError: 受け取った値の型またはevent種別がこの処理の契約に合わない場合。
    """
    if isinstance(event, PendingCounterReset):
        raise TypeError("counter reset is not an input/output event")
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
    """Queue eventをDB writer向けの不変値へ変換する。

    Args:
        event: FIFOへ追加、保存、または型変換する確定event。

    Returns:
        入力または出力eventに対応する不変DB write値。

    Raises:
        TypeError: 受け取った値の型またはevent種別がこの処理の契約に合わない場合。
    """
    if isinstance(event, PendingCounterReset):
        raise TypeError("counter reset is not an input/output event")
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
