"""FakeGPIOでも検証できるloggerdの監視ライフサイクル。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from enum import StrEnum

from event_logger.adapters.clock import Clock
from event_logger.common.models import EventKind
from event_logger.database.repositories import EventRepository, SessionRepository
from event_logger.domain.debounce import Debouncer
from event_logger.loggerd.db_writer import EventWriter, EventWriterPort
from event_logger.loggerd.event_queue import (
    EventQueue,
    EventQueueFull,
    PendingCounterReset,
    PendingInputEvent,
)
from event_logger.loggerd.outputs import OutputManager

_LOGGER = logging.getLogger(__name__)


class LoggerStatus(StrEnum):
    """loggerdの外部表示用状態。"""

    STOPPED = "stopped"
    LOGGING = "logging"
    MAINTENANCE = "maintenance"
    CRITICAL = "critical"


class LoggerService:
    """入力snapshot、debounce、Queue、session終了を統括する。"""

    def __init__(
        self,
        *,
        clock: Clock,
        session_repository: SessionRepository,
        event_repository: EventRepository,
        event_queue: EventQueue,
        event_writer: EventWriterPort | None = None,
        app_version: str,
        boot_id: str,
        enabled_channels: Mapping[int, int],
        output_manager: OutputManager | None = None,
        confirmed_input_callback: Callable[[int, bool, int], None] | None = None,
    ) -> None:
        """loggerd依存を注入して作成する。

        Args:
            clock: UTC/monotonic時刻と同期状態を提供するClock。
            session_repository: 監視sessionの作成と終了を担当するRepository。
            event_repository: 入力eventの保存先Repository。
            event_queue: producerとwriterで共有する有界FIFO Queue。
            event_writer: Queue eventを保存するwriter。未指定なら同期writerを構成する。
            app_version: 稼働中ソフトウェアのversion文字列。
            boot_id: OS起動単位の識別値。
            enabled_channels: 有効入力のchannel_idとdebounce_msの対応。
                GPIO番号はこの層へ渡さず、hardware adapter側で解決する。
            output_manager: OUT ownerと物理出力状態を管理する任意の依存。
            confirmed_input_callback: debounce確定値と確定時刻を受け取る任意callback。

        Returns:
            None。
        """
        self._clock = clock
        self._session_repository = session_repository
        self._event_repository = event_repository
        self._event_queue = event_queue
        self._event_writer = (
            event_writer
            if event_writer is not None
            else EventWriter(event_repository, event_queue)
        )
        self._app_version = app_version
        self._boot_id = boot_id
        self._enabled_channels = dict(enabled_channels)
        self._output_manager = output_manager
        self._confirmed_input_callback = confirmed_input_callback
        self._debouncers: dict[int, Debouncer] = {}
        self._confirmed_values: dict[int, bool] = {}
        self._confirmed_since_ns: dict[int, int] = {}
        self._session_id: int | None = None
        # GPIO Zeroのcallback threadとdaemon loopが同じdebouncerを触るため、
        # 候補状態・session ID・Critical遷移を一つのロックで直列化する。
        self._lock = threading.RLock()
        self.status = LoggerStatus.STOPPED

    @property
    def session_id(self) -> int | None:
        """現在の監視session IDを返す。

        Args:
            なし。

        Returns:
            Heartbeatが割り当てられていれば論理OUT channel ID、未割当ならNone。
        """
        return self._session_id

    @property
    def pending_event_count(self) -> int:
        """DB writer待ちの確定event数を返す。

        Args:
            なし。

        Returns:
            writerが保持する未commit eventの件数。Queue待機中とcommit中を含む。
        """
        return len(self._event_queue)

    @property
    def enabled_channel_ids(self) -> tuple[int, ...]:
        """現在の監視対象入力IDを返す。

        Args:
            なし。

        Returns:
            監視対象の論理入力IDを設定順に並べたtuple。
        """
        with self._lock:
            return tuple(self._enabled_channels)

    @property
    def enabled_channels(self) -> dict[int, int]:
        """現在の監視対象入力とdebounce設定のコピーを返す。

        Args:
            なし。

        Returns:
            現在のchannel IDとdebounce ms設定のコピー。
        """
        with self._lock:
            return dict(self._enabled_channels)

    @property
    def confirmed_inputs(self) -> dict[int, bool]:
        """現在sessionで最後に確定した論理入力値を返す。

        Args:
            なし。

        Returns:
            各入力の最後の確定論理値を持つコピー。
        """
        with self._lock:
            return dict(self._confirmed_values)

    @property
    def confirmed_input_since_ns(self) -> dict[int, int]:
        """各入力値が確定したmonotonic時刻を返す。

        Args:
            なし。

        Returns:
            各入力の確定monotonic時刻（ns）のコピー。
        """
        with self._lock:
            return dict(self._confirmed_since_ns)

    def mark_critical(self) -> None:
        """外部callbackから記録不能状態へ安全に遷移させる。

        Args:
            なし。

        Returns:
            None。
        """
        with self._lock:
            self.status = LoggerStatus.CRITICAL

    def start(self, input_values: Mapping[int, bool]) -> None:
        """新しいsessionを作成し、有効入力のsnapshotを保存する。

        Args:
            input_values: channel IDを論理状態へ対応づけた入力snapshot。

        Returns:
            None。sessionを作り、全有効入力のsnapshotを保存してLOGGINGへ移る。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        with self._lock:
            if self.status is not LoggerStatus.STOPPED:
                raise RuntimeError(f"cannot start from status {self.status}")
            self._validate_input_values(input_values)
            # sessionより先にOUTをOFFへ初期化する。出力初期化に失敗したときは
            # DB sessionだけが開始済みになる状態を作らない。
            if self._output_manager is not None:
                try:
                    self._output_manager.startup_off()
                except Exception:
                    self.status = LoggerStatus.CRITICAL
                    raise
            now_utc_ms = self._clock.now_utc_ms()
            session = self._session_repository.create(
                boot_id=self._boot_id,
                started_at_ms=now_utc_ms,
                app_version=self._app_version,
                clock_synced=self._clock.is_synchronized(),
            )
            self._session_id = session.id
            # session開始時の入力状態をchannelごとのdebounce基準にし、再起動後に
            # 起動時点のON/OFFを変化eventとして誤記録しない。
            self._debouncers = {}
            self._confirmed_values = {}
            self._confirmed_since_ns = {}
            try:
                for channel_id, debounce_ms in self._enabled_channels.items():
                    value = input_values[channel_id]
                    snapshot_mono_ns = self._clock.now_monotonic_ns()
                    debouncer = Debouncer(debounce_ms=debounce_ms)
                    debouncer.initialize(value, snapshot_mono_ns)
                    self._debouncers[channel_id] = debouncer
                    self._confirmed_values[channel_id] = bool(value)
                    self._confirmed_since_ns[channel_id] = snapshot_mono_ns
                    self._event_repository.append(
                        session_id=session.id,
                        ts_utc_ms=now_utc_ms,
                        mono_ns=snapshot_mono_ns,
                        channel_id=channel_id,
                        value=int(value),
                        kind=EventKind.SNAPSHOT,
                        clock_synced=self._clock.is_synchronized(),
                    )
            except Exception:
                # snapshotの一部だけ保存されたsessionをLOGGING扱いにしない。
                self.status = LoggerStatus.CRITICAL
                raise
            self.status = LoggerStatus.LOGGING

    def observe_input(self, channel_id: int, value: bool) -> None:
        """GPIO callbackから論理入力変化を受け取る。

        callback内ではSQLiteへ直接書き込まず、debounce確定eventをQueueへ積む。

        Args:
            channel_id: 論理IN/OUT番号（1〜6）。GPIO pin番号ではない。
            value: 論理状態。ONはTrue/1、OFFはFalse/0。

        Returns:
            None。入力候補をdebounceへ渡し、安定確定時だけQueueへ積む。
        """
        with self._lock:
            if self.status is not LoggerStatus.LOGGING:
                return
            debouncer = self._debouncers.get(channel_id)
            if debouncer is None:
                return
            now_utc_ms = self._clock.now_utc_ms()
            now_monotonic_ns = self._clock.now_monotonic_ns()
            confirmed = debouncer.observe(value, now_monotonic_ns)
            if confirmed is None:
                return
            self._enqueue_confirmed(
                channel_id,
                confirmed,
                now_utc_ms=now_utc_ms,
                now_monotonic_ns=now_monotonic_ns,
            )

    def tick_debounce(self) -> int:
        """edge callback後も安定時間を経過した候補を確定する。

        GPIO Zeroは安定中に追加callbackを発生させないため、daemon loopから
        候補をpollしないと、変化がそのまま保持された入力のeventが欠落する。

        Args:
            なし。

        Returns:
            今回確定してQueueへ追加した入力event数。
        """
        with self._lock:
            if self.status is not LoggerStatus.LOGGING:
                return 0
            now_utc_ms = self._clock.now_utc_ms()
            now_monotonic_ns = self._clock.now_monotonic_ns()
            confirmed_count = 0
            for channel_id, debouncer in self._debouncers.items():
                confirmed = debouncer.poll(now_monotonic_ns)
                if confirmed is None:
                    continue
                self._enqueue_confirmed(
                    channel_id,
                    confirmed,
                    now_utc_ms=now_utc_ms,
                    now_monotonic_ns=now_monotonic_ns,
                )
                confirmed_count += 1
            return confirmed_count

    def enqueue_counter_reset(self, pending: PendingCounterReset) -> None:
        """確定入力と同じ排他境界でresetをFIFOへ投入する。

        Args:
            pending: Queue上の位置に保存する未commit event。

        Returns:
            None。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        with self._lock:
            if self.status is LoggerStatus.CRITICAL:
                raise RuntimeError("counter reset is unavailable in critical state")
            pending.reset_at_ms = self._clock.now_utc_ms()
            self._event_queue.put(pending)

    def _enqueue_confirmed(
        self,
        channel_id: int,
        confirmed: bool,
        *,
        now_utc_ms: int,
        now_monotonic_ns: int,
        notify_rules: bool = True,
    ) -> None:
        """確定済みedgeをQueueへ積み、出力ルールへ通知する。

        Args:
            channel_id: 論理IN/OUT番号（1〜6）。GPIO pin番号ではない。
            confirmed: debounceを通過した論理入力状態。
            now_utc_ms: 現在のUTC Unix epoch時刻（ms）。
            now_monotonic_ns: 現在のmonotonic時計値（ns）。
            notify_rules: 確定edgeを条件rule evaluatorへ通知するか。

        Returns:
            None。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        if self._session_id is None:
            raise RuntimeError("logging status has no active session")
        pending = PendingInputEvent(
            session_id=self._session_id,
            ts_utc_ms=now_utc_ms,
            mono_ns=now_monotonic_ns,
            channel_id=channel_id,
            value=int(confirmed),
            kind=EventKind.EDGE,
            clock_synced=self._clock.is_synchronized(),
        )
        try:
            self._event_writer.enqueue_input(pending)
        except EventQueueFull:
            self.status = LoggerStatus.CRITICAL
            raise
        self._confirmed_values[channel_id] = bool(confirmed)
        self._confirmed_since_ns[channel_id] = now_monotonic_ns
        if notify_rules and self._confirmed_input_callback is not None:
            try:
                self._confirmed_input_callback(
                    channel_id,
                    bool(confirmed),
                    now_monotonic_ns,
                )
            except Exception:
                self.status = LoggerStatus.CRITICAL
                raise

    def drain_events(self) -> int:
        """DB writerを進め、保存件数を返す。

        Args:
            なし。

        Returns:
            writerが保持する未commit eventの件数。Queue待機中とcommit中を含む。
        """
        with self._lock:
            if self.status is LoggerStatus.CRITICAL:
                return 0
        # production writerは専用threadがSQLiteへ保存する。ここはworker状態の
        # 確認だけに留め、SD待ちでHeartbeat loopを塞がない。
        try:
            return self._event_writer.drain_all()
        except Exception:
            with self._lock:
                self.status = LoggerStatus.CRITICAL
            raise

    def stop(self, *, on_quiesced: Callable[[], None] | None = None) -> None:
        """Queueを保存し、正常終了できたsessionへ終了時刻を記録する。

        Args:
            on_quiesced: 受付停止後、Queue drain前に呼ぶ任意callback。

        Returns:
            None。新規eventを止め、Queue drain後に正常sessionを終了する。
        """
        with self._lock:
            if self.status is LoggerStatus.STOPPED:
                return
            if self.status is LoggerStatus.CRITICAL:
                self._try_shutdown_outputs()
                return
            if self.status is not LoggerStatus.LOGGING:
                return
            session_id = self._session_id
            # 停止処理の概要: 停止境界までのdebounce確定→callback受付遮断→OUT OFF
            # とpause記録→Queue drain→session終了。長いDB flushはlock外で待つ。
            # maintenance遷移時刻が監視受付の境界であり、DB flushやOUT OFFの
            # 遅延をsessionの正常終了時刻へ含めない。
            ended_at_ms = self._clock.now_utc_ms()
            stopped_at_ns = self._clock.now_monotonic_ns()
            # 通常loopの次回pollより先に停止要求が届くと、既にdebounce時間を
            # 満たした候補を破棄してしまう。callback受付と同じlock内で境界時刻
            # までを最終判定し、確定分は出力ruleを再起動させず記録する。
            try:
                for channel_id, debouncer in self._debouncers.items():
                    confirmed = debouncer.poll(stopped_at_ns)
                    if confirmed is None:
                        continue
                    self._enqueue_confirmed(
                        channel_id,
                        confirmed,
                        now_utc_ms=ended_at_ms,
                        now_monotonic_ns=stopped_at_ns,
                        notify_rules=False,
                    )
            except Exception:
                self.status = LoggerStatus.CRITICAL
                self._try_shutdown_outputs()
                raise
            # ここでcallbackを受け付けない状態へ先に遷移し、以降のDB flushを
            # lock外で行う。SD遅延中もGPIO callbackは即時returnできる。
            self.status = LoggerStatus.MAINTENANCE
        try:
            try:
                if self._output_manager is not None:
                    self._output_manager.shutdown_off()
            finally:
                # 永続pauseは入力受付停止・全OUTのOFF試行後、queueの保存待ち前。
                # callback内で失敗を保持すれば、flag異常でも現在の停止を完了できる。
                if on_quiesced is not None:
                    on_quiesced()
            # Queueの保存完了後にsession終了時刻を書き、永続化待ちで終了済み
            # sessionの時刻が先行しないようにする。
            self._event_writer.wait_until_drained()
            if session_id is not None:
                self._session_repository.end(
                    session_id,
                    ended_at_ms,
                )
        except Exception:
            with self._lock:
                self.status = LoggerStatus.CRITICAL
            raise
        with self._lock:
            self._session_id = None
            self._debouncers = {}
            self._confirmed_values = {}
            self._confirmed_since_ns = {}
            self.status = LoggerStatus.STOPPED

    def _try_shutdown_outputs(self) -> None:
        """Critical時にも可能な範囲でOUTをOFFへ戻す。

        Args:
            なし。

        Returns:
            None。
        """
        if self._output_manager is None:
            return
        try:
            self._output_manager.shutdown_off()
        except Exception:
            # DB/GPIO双方の異常時に停止処理を再帰的に失敗させない。
            # Criticalの詳細はここで記録し、safe defaultは維持する。
            _LOGGER.exception("failed to shut down outputs in critical state")
            return

    def enter_maintenance(
        self, *, on_quiesced: Callable[[], None] | None = None
    ) -> None:
        """監視を停止し、Web出力テスト可能な状態へ移行する。

        Args:
            on_quiesced: 受付停止後、Queue drain前に呼ぶ任意callback。

        Returns:
            None。監視とrule ownerを停止してmaintenance状態へ移る。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        with self._lock:
            if self.status is not LoggerStatus.LOGGING:
                raise RuntimeError(
                    f"cannot enter maintenance from status {self.status}"
                )
        self.stop(on_quiesced=on_quiesced)
        with self._lock:
            self.status = LoggerStatus.MAINTENANCE

    def enter_initial_maintenance(self) -> None:
        """初回設定前にsessionを作らず、設定完了待ちへ移行する。

        Args:
            なし。

        Returns:
            None。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        with self._lock:
            if self.status is not LoggerStatus.STOPPED:
                raise RuntimeError(
                    f"cannot enter initial maintenance from status {self.status}"
                )
            # 未設定の全IN無効状態を「正常監視」として扱うと、設定前の
            # 起動時間が監視済み区間に見えてしまう。初回設定後にSTART_LOGGING
            # された時点で初めてsnapshot/sessionを作る。
            self.status = LoggerStatus.MAINTENANCE

    def leave_maintenance(self) -> None:
        """再開前の入力値を受け取る呼び出しへ状態を戻す。

        Args:
            なし。

        Returns:
            None。次のstartで入力snapshotを受けられる状態へ戻す。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        with self._lock:
            if self.status is not LoggerStatus.MAINTENANCE:
                raise RuntimeError(f"not in maintenance: {self.status}")
            self.status = LoggerStatus.STOPPED

    def abort_start(self) -> None:
        """開始途中のsessionを監視不能としてmaintenanceへ戻す。

        session終了時刻を付けると、snapshot後に監視できなかった区間まで
        正常終了したように見えるため、異常sessionは未終了のまま残す。

        Args:
            なし。

        Returns:
            None。
        """
        with self._lock:
            if self.status not in {LoggerStatus.LOGGING, LoggerStatus.CRITICAL}:
                self.status = LoggerStatus.MAINTENANCE
                return
            # 起動失敗後はcallback受付を先に閉じ、flush対象が増え続けないようにする。
            self.status = LoggerStatus.MAINTENANCE
            try:
                self._try_shutdown_outputs()
                self._event_writer.wait_until_drained()
            except Exception:
                # 開始失敗の原因がDB障害の場合も、session境界を壊さずに
                # Webから再試行できるmaintenance状態へ戻す。
                _LOGGER.exception("failed to drain events while aborting start")
            self._session_id = None
            self._debouncers = {}
            self._confirmed_values = {}
            self._confirmed_since_ns = {}
            self.status = LoggerStatus.MAINTENANCE

    def reload_channels(self, enabled_channels: Mapping[int, int]) -> None:
        """停止中の次回sessionへ入力設定を反映する。

        Args:
            enabled_channels: 有効channel IDをdebounce時間（ms）へ対応づける設定。

        Returns:
            None。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        with self._lock:
            if self.status is LoggerStatus.LOGGING:
                raise RuntimeError("channel reload requires logging to be stopped")
            self._validate_enabled_channels(enabled_channels)
            self._enabled_channels = dict(enabled_channels)

    def _validate_input_values(self, input_values: Mapping[int, bool]) -> None:
        """有効入力の初期値がすべて揃っていることを検証する。

        Args:
            input_values: channel IDを論理状態へ対応づけた入力snapshot。

        Returns:
            None。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        self._validate_enabled_channels(self._enabled_channels)
        missing = set(self._enabled_channels) - set(input_values)
        if missing:
            raise ValueError(f"missing input values: {sorted(missing)}")

    @staticmethod
    def _validate_enabled_channels(enabled_channels: Mapping[int, int]) -> None:
        """enabled channel設定の範囲を検証する。

        Args:
            enabled_channels: 有効channel IDをdebounce時間（ms）へ対応づける設定。

        Returns:
            None。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        for channel_id, debounce_ms in enabled_channels.items():
            if not 1 <= channel_id <= 6:
                raise ValueError("enabled channel id must be between 1 and 6")
            if not 0 <= debounce_ms <= 5000:
                raise ValueError("debounce_ms must be between 0 and 5000")
