"""event-loggerdの起動、制御IPC、graceful shutdownを統括する。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from event_logger.adapters.clock import Clock, FakeClock, SystemClock
from event_logger.common.configuration import configuration_fingerprint
from event_logger.database.connection import connect_database, initialize_connection
from event_logger.database.repositories import (
    AuditLogRepository,
    ChannelRepository,
    EventRepository,
    OutputEventRepository,
    OutputRuleRepository,
    SessionRepository,
    SettingsRepository,
    WidgetRepository,
)
from event_logger.domain.outputs import OutputOwnerRegistry
from event_logger.domain.widgets import WidgetType
from event_logger.hardware.fake_gpio import FakeGPIO
from event_logger.hardware.gpiozero_adapter import GpioZeroHat
from event_logger.hardware.ports import OutputPort
from event_logger.loggerd.db_writer import BackgroundEventWriter
from event_logger.loggerd.event_queue import EventQueue, PendingCounterReset
from event_logger.loggerd.ipc import (
    ControlCommand,
    ControlRequest,
    UnixControlServer,
)
from event_logger.loggerd.outputs import OutputManager
from event_logger.loggerd.rules import (
    OutputRuleConfigurationError,
    OutputRuleRunner,
)
from event_logger.loggerd.service import LoggerService, LoggerStatus
from event_logger.version import VERSION

_LOGGER = logging.getLogger(__name__)
_OUTPUT_LOOP_WATCHDOG_NS = 500_000_000


class GPIOPort(Protocol):
    """loggerdが必要とするGPIO adapter port。"""

    @property
    def output_ports(self) -> Mapping[int, OutputPort]:
        """論理OUT portを返す。

        Args:
            なし。

        Returns:
            論理OUT番号をadapter portへ対応づけたmapping。
        """

    def input_values(self) -> dict[int, bool]:
        """全INの論理状態を返す。

        Args:
            なし。

        Returns:
            全channel IDを論理入力値へ対応づけたmapping。
        """

    def attach_input_callbacks(self, callback: Callable[[int, bool], None]) -> None:
        """入力callbackを接続する。

        Args:
            callback: 入力確定時に呼ぶ関数。

        Returns:
            None。
        """

    def close(self) -> None:
        """GPIO resourcesを閉じる。

        Args:
            なし。

        Returns:
            None。出力停止を試み、session・socket・GPIO・DBを順に解放する。
        """


class LoggerDaemon:
    """実機・FakeGPIOに共通するdaemon lifecycle。"""

    def __init__(
        self,
        *,
        database_path: str | Path,
        socket_path: str | Path,
        app_version: str = VERSION,
        boot_id: str | None = None,
        gpio: GPIOPort | None = None,
        clock: Clock | None = None,
        queue_maxsize: int = 10_000,
    ) -> None:
        """daemon構成を注入し、まだGPIO listenやthreadを開始しない。

        Args:
            database_path: SQLite databaseのfilesystem path。
            socket_path: 接続先またはlistenするUnix socket path。
            app_version: 稼働中ソフトウェアのversion文字列。
            boot_id: OS起動単位の識別値。
            gpio: GPIO hardware adapter。pin割当と電気極性はadapter内で解決する。
            clock: UTC/monotonic時刻と同期状態を提供するClock。
            queue_maxsize: 未commit event Queueの上限件数。

        Returns:
            None。
        """
        # DB schemaとGPIO adapterを先に用意する。GPIO初期化失敗時は既に開いた
        # 接続を閉じ、構築途中のresource leakを避ける。
        self._connection = connect_database(database_path)
        try:
            initialize_connection(self._connection)
            self._gpio = gpio or GpioZeroHat.create()
        except Exception:
            # GPIO初期化失敗時も先に開いたWAL接続を残さない。
            self._connection.close()
            raise
        # Repository群はdaemon lifetimeで同じconnectionを使い、background writer
        # だけはSQLite thread affinityのため別connectionを持つ。
        self._clock = clock or SystemClock()
        self._socket_path = Path(socket_path)
        self._app_version = app_version
        self._boot_id = boot_id or _read_boot_id()
        self._channels = ChannelRepository(self._connection)
        self._settings = SettingsRepository(self._connection)
        self._audit = AuditLogRepository(self._connection)
        self._rule_repository = OutputRuleRepository(self._connection)
        self._event_queue = EventQueue(maxsize=queue_maxsize)
        self._event_writer = BackgroundEventWriter(
            database_path=database_path,
            event_queue=self._event_queue,
        )
        # 起動時設定を読み込んでからrule/output/serviceを組み立て、adapter間の
        # callback参照が使われる前に初期設定を適用する。
        channels = self._channels.list_all()
        rules = self._rule_repository.list_all()
        enabled_channels = {
            channel.id: channel.debounce_ms for channel in channels if channel.enabled
        }
        self._service: LoggerService
        self._outputs = OutputManager(
            clock=self._clock,
            outputs=self._gpio.output_ports,
            repository=OutputEventRepository(self._connection),
            owner_registry=OutputOwnerRegistry(),
            session_id_provider=lambda: self._service.session_id,
            maintenance_mode_provider=lambda: (
                self._service.status is LoggerStatus.MAINTENANCE
            ),
            event_sink=self._event_writer,
        )
        self._rule_runner = OutputRuleRunner(
            clock=self._clock,
            output_manager=self._outputs,
        )
        self._rule_runner.configure(rules)
        self._service = LoggerService(
            clock=self._clock,
            session_repository=SessionRepository(self._connection),
            event_repository=EventRepository(self._connection),
            event_queue=self._event_queue,
            event_writer=self._event_writer,
            app_version=app_version,
            boot_id=self._boot_id,
            enabled_channels=enabled_channels,
            output_manager=self._outputs,
            confirmed_input_callback=self._rule_runner.observe,
        )
        # 開始時の再読とcallbackの順序だけを保護する。開始処理ではGPIO読取りや
        # snapshotのDB保存をこのlockの外で行い、追加の待機区間を作らない。
        self._input_callback_lock = threading.RLock()
        self._input_sequences: dict[int, int] = {}
        self._counter_reset_requests: dict[
            str, tuple[ControlRequest, PendingCounterReset]
        ] = {}
        self._apply_heartbeat_settings()
        heartbeat_enabled, heartbeat_channel, heartbeat_interval = (
            self._heartbeat_settings()
        )
        self._configuration_digest = configuration_fingerprint(
            channels,
            rules,
            {
                "enabled": heartbeat_enabled,
                "channel_id": heartbeat_channel,
                "interval_ms": heartbeat_interval,
            },
        )
        # output testの最大5秒制限とdebounceのpollをloop遅延で超過させない。
        # socket処理は短いpollに留め、重い処理はhandler側で行わない。
        self._control = UnixControlServer(
            self._socket_path,
            self._handle_control,
            socket_timeout=0.05,
        )
        self._started = False
        self._last_loop_ns = self._clock.now_monotonic_ns()
        # watchdogと制御loopが同じprogress checkpointを読み書きするため、
        # 古いcheckpointを見たwatchdogと復帰後のheartbeatが競合しないよう、
        # checkpointの読書きとOFF処理の判定を直列化する。
        self._watchdog_state_lock = threading.Lock()
        self._loop_phase = "startup"
        self._watchdog_trip_phase: str | None = None
        self._watchdog_trip_elapsed_ms: int | None = None
        self._watchdog_stop = threading.Event()
        self._watchdog_tripped = threading.Event()
        self._watchdog_reported = False
        self._watchdog_thread: threading.Thread | None = None

    @property
    def service(self) -> LoggerService:
        """テスト・監視用のLoggerServiceを返す。

        Args:
            なし。

        Returns:
            このdaemonが利用するLoggerService。
        """
        return self._service

    @property
    def output_manager(self) -> OutputManager:
        """テスト・監視用のOutputManagerを返す。

        Args:
            なし。

        Returns:
            このdaemonが利用するOutputManager。
        """
        return self._outputs

    def start(self) -> None:
        """GPIO callbackとcontrol socketを開始して監視sessionを作る。

        Args:
            なし。

        Returns:
            None。依存serviceとsocketを準備し、session・watchdogを開始する。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        if self._started:
            raise RuntimeError("logger daemon is already started")
        try:
            # 永続化threadとIPCを先に準備する。以降のsession開始やcallbackからの
            # eventを、writer/socketの未準備状態へ流さない。
            self._event_writer.start()
            self._control.start()
            setup_completed = self._settings.get_json("setup_completed", False)
            if not isinstance(setup_completed, bool):
                raise ValueError("setup_completed setting must be boolean")
            if not setup_completed or self._logging_is_paused():
                # 初回設定未完了または明示停止中は、GPIO監視・OUT動作を始めず
                # maintenanceのまま起動する。
                self._rule_runner.pause()
                self._outputs.disable_heartbeat()
                self._service.enter_initial_maintenance()
                self._started = True
                return
            self._start_logging_session()
        except Exception:
            # 部分起動で作成済みのsocket/threadを巻き戻す。cleanup失敗はログに
            # 残しつつ、呼出側には元の起動例外を返す。
            self._control.close()
            try:
                self._event_writer.stop()
            except Exception:
                _LOGGER.exception("failed to stop event writer after startup failure")
            raise
        self._started = True

    def _logging_is_paused(self) -> bool:
        """明示停止を再起動・backup復元後にも維持する。

        旧DBの未設定は従来どおり通常起動を許す。破損した型をFalseへ
        丸めると意図せず記録/OUTが動くため、boolean以外は起動を拒否する。

        Args:
            なし。

        Returns:
            DBに保存されたpause flagがTrueならTrue。未設定の場合だけFalseとする。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        value = self._settings.get_json("logging_paused", False)
        if not isinstance(value, bool):
            raise ValueError("logging_paused setting must be boolean")
        return value

    def run_once(self) -> None:
        """Queue、Heartbeat、OUT test、control requestを1回進める。

        Args:
            なし。

        Returns:
            None。各処理の状態更新を完了する。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        # 1周は「入力確定→Heartbeat/rule→OUT test→IPC」。watchdog判定を各段階で
        # 挟み、異常trip後に後続処理が出力を再度ONにしない。
        if not self._started:
            raise RuntimeError("logger daemon is not started")
        self._complete_watchdog_trip_if_needed()
        try:
            if self._service.status is LoggerStatus.LOGGING:
                # queue済み確定eventをdebounce前に処理し、候補のpoll後にもdrainする。
                # この二段階で既存eventと今回確定したeventの両方を同じloopで反映する。
                if self._set_loop_phase("input_queue_drain_before_debounce"):
                    self._service.drain_events()
                self._complete_watchdog_trip_if_needed()
                if self._service.status is LoggerStatus.LOGGING:
                    if self._set_loop_phase("input_debounce"):
                        self._service.tick_debounce()
                    self._complete_watchdog_trip_if_needed()
                if self._service.status is LoggerStatus.LOGGING:
                    if self._set_loop_phase("input_queue_drain_after_debounce"):
                        self._service.drain_events()
                    self._complete_watchdog_trip_if_needed()
                if self._service.status is LoggerStatus.LOGGING:
                    # 出力制御は入力状態の反映後に実行する。heartbeat/rule間もtripを
                    # 確認し、watchdogが停止した後の再出力を避ける。
                    # 入力処理中にwatchdogがtrip済みなら、出力操作を再開しない。
                    # checkpoint更新とwatchdog判定を同じlockで直列化する。
                    if self._refresh_loop_progress("heartbeat"):
                        self._outputs.heartbeat_tick(healthy=True)
                    else:
                        self._complete_watchdog_trip_if_needed()
                    self._complete_watchdog_trip_if_needed()
                if self._service.status is LoggerStatus.LOGGING:
                    if self._set_loop_phase("output_rules"):
                        self._rule_runner.tick()
                    self._complete_watchdog_trip_if_needed()
            # 診断テストの期限切れ確認は通常loop末尾で行う。
            if self._set_loop_phase("output_test"):
                self._outputs.output_test_tick()
        except Exception:
            with self._watchdog_state_lock:
                watchdog_tripped = self._watchdog_tripped.is_set()
            # trip起因の例外は物理OFF済み状態を確定し、通常障害だけをcritical化する。
            if watchdog_tripped:
                # watchdogが先に物理OFFした直後の再出力要求は、通常loop例外と
                # 誤分類せず、trip状態の確定だけを行う。
                self._complete_watchdog_trip()
            else:
                _LOGGER.exception("loggerd loop failed; entering critical state")
                self._mark_critical()
        # Critical時も照会・停止のcontrol IPCを処理できるようacceptを続ける。
        self._complete_watchdog_trip_if_needed()
        # Critical中もstatus照会を処理できるようIPC acceptは継続する。
        self._set_loop_phase("control_ipc")
        self._control.serve_once()
        if not self._refresh_loop_progress("idle"):
            self._complete_watchdog_trip_if_needed()

    def _watchdog_check(self) -> None:
        """DB/IPC停止中でも、ON中のOUTを物理OFFへ戻す。

        Args:
            なし。

        Returns:
            None。
        """
        with self._watchdog_state_lock:
            if self._watchdog_tripped.is_set():
                return
            if self._service.status is not LoggerStatus.LOGGING:
                return
            if not self._outputs.has_active_output():
                return
            elapsed_ns = self._clock.now_monotonic_ns() - self._last_loop_ns
            if elapsed_ns <= _OUTPUT_LOOP_WATCHDOG_NS:
                return
            self._watchdog_trip_phase = self._loop_phase
            self._watchdog_trip_elapsed_ms = elapsed_ns // 1_000_000
            try:
                self._outputs.emergency_off()
            finally:
                # 物理OFFの一部が失敗しても、制御loopはCriticalへ移す。
                self._watchdog_tripped.set()

    def _set_loop_phase(self, phase: str) -> bool:
        """現在の制御段階をwatchdogと共有する。trip後の段階変更は拒否する。

        Args:
            phase: watchdogと共有するlogger loop段階名。

        Returns:
            watchdog
            trip前にphaseを更新できた場合True、trip済みで更新を拒否した場合False。
        """
        with self._watchdog_state_lock:
            if self._watchdog_tripped.is_set():
                return False
            self._loop_phase = phase
            return True

    def _refresh_loop_progress(self, phase: str) -> bool:
        """trip前にだけloop checkpointと段階を同期更新する。

        Args:
            phase: watchdogと共有するlogger loop段階名。

        Returns:
            watchdog trip前にcheckpointとphaseを同期更新できた場合True。
        """
        with self._watchdog_state_lock:
            if self._watchdog_tripped.is_set():
                return False
            self._last_loop_ns = self._clock.now_monotonic_ns()
            self._loop_phase = phase
            return True

    def _complete_watchdog_trip_if_needed(self) -> bool:
        """watchdogが物理OFFした場合、制御loop側のCritical処理を完了する。

        Args:
            なし。

        Returns:
            watchdog tripがあれば制御loop側の後処理を完了してTrue、なければFalse。
        """
        if not self._watchdog_tripped.is_set():
            return False
        self._complete_watchdog_trip()
        return True

    def _watchdog_loop(self) -> None:
        """SQLite待ちに巻き込まれないthreadでloopの停止を検出する。

        Args:
            なし。

        Returns:
            None。
        """
        while not self._watchdog_stop.wait(0.05):
            try:
                self._watchdog_check()
            except Exception:
                _LOGGER.exception("output loop watchdog failed")

    def _complete_watchdog_trip(self) -> None:
        """物理OFFの後、履歴とCritical状態を制御loop側で確定する。

        Args:
            なし。

        Returns:
            None。
        """
        if not self._watchdog_reported:
            self._watchdog_reported = True
            _LOGGER.error(
                "loggerd loop stalled while an output was ON: "
                "phase=%s stalled_ms=%s pending_events=%s",
                self._watchdog_trip_phase or self._loop_phase,
                self._watchdog_trip_elapsed_ms,
                self._service.pending_event_count,
            )
            self._service.mark_critical()
            self._mark_critical()
        # 初回のDB書込みが失敗しても、loopが動く限り次回に再試行する。
        try:
            self._outputs.flush_emergency_off_events()
        except Exception:
            _LOGGER.exception("failed to record watchdog output OFF events")

    def run(self, stop_event: threading.Event) -> None:
        """stop_eventが立つまでdaemon loopを回す。

        Args:
            stop_event: daemon loopの終了を通知するthreading.Event。

        Returns:
            None。終了通知後にdaemon資源を解放する。
        """
        # 起動・通常運転・終了処理をこの関数でまとめて管理する。
        # 起動途中で失敗しても、作成済み資源を解放する。
        try:
            self.start()
        except Exception:
            # start()はcontrol socketやDB sessionを部分的に作成してから失敗する
            # 可能性がある。started flagだけに依存せず、資源を必ず解放する。
            self.close()
            raise
        try:
            # 長いloopへ入る前にwatchdog基準時刻を更新して監視threadを開始する。
            self._refresh_loop_progress("run_loop")
            self._watchdog_stop.clear()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="event-logger-output-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()
            # 各周回で入力確定・出力制御・IPC要求を進め、周回の間に終了通知を確認する。
            while not stop_event.is_set():
                self.run_once()
        finally:
            # 制御loopを抜けたらwatchdogへ終了を通知し、物理OFF監視threadが
            # 残らないようjoinしてからdaemon資源をcloseする。
            self._watchdog_stop.set()
            if self._watchdog_thread is not None:
                self._watchdog_thread.join()
                self._watchdog_thread = None
            # service/session、control socket、GPIO、writer、DBの順に解放する。
            self.close()

    def close(self) -> None:
        """出力OFF、session終了、socket/GPIO/DB解放を順に行う。

        Args:
            なし。

        Returns:
            None。出力停止を試み、session・socket・GPIO・DBを順に解放する。
        """
        # 停止失敗を保持しながらも後続資源の解放を続け、最後に最初の障害を返す。
        stop_error: Exception | None = None
        was_logging = self._service.status is LoggerStatus.LOGGING
        if self._started or self._service.status not in {
            LoggerStatus.STOPPED,
            LoggerStatus.MAINTENANCE,
        }:
            try:
                self._service.stop()
            except Exception as error:
                stop_error = error
            finally:
                self._started = False
            if stop_error is None and was_logging:
                self._record_audit("logging_stopped")
        try:
            # maintenance中はservice.stop()がsessionを持たないため何もしない。
            # GPIO/DBを閉じる前に、診断テストのOFF・履歴・timer終了も必ず済ませる。
            try:
                self._outputs.shutdown_off()
            except Exception as error:
                _LOGGER.exception("failed to shut down outputs before GPIO close")
                if stop_error is None:
                    stop_error = error
            self._control.close()
        finally:
            try:
                self._gpio.close()
            finally:
                try:
                    self._event_writer.stop()
                except Exception as error:
                    _LOGGER.exception("failed to stop SQLite event writer")
                    if stop_error is None:
                        stop_error = error
                finally:
                    self._connection.close()
        if stop_error is not None:
            raise stop_error

    def _on_input(self, channel_id: int, value: bool) -> None:
        """GPIO callbackではserviceへ値を渡すだけにする。

        Args:
            channel_id: 論理IN/OUT番号（1〜6）。GPIO pin番号ではない。
            value: 論理状態。ONはTrue/1、OFFはFalse/0。

        Returns:
            None。
        """
        try:
            with self._input_callback_lock:
                self._input_sequences[channel_id] = (
                    self._input_sequences.get(channel_id, 0) + 1
                )
                self._service.observe_input(channel_id, value)
        except Exception:
            # GPIO Zeroのcallback threadへ例外を返すと、入力監視thread側の
            # エラーだけが残り、Heartbeat停止・OUT OFFが実行されない。
            _LOGGER.exception("input callback failed for channel %s", channel_id)
            self._mark_critical()

    def _mark_critical(self) -> None:
        """記録不能時にHeartbeatと全OUTを可能な範囲でOFFにする。

        Args:
            なし。

        Returns:
            None。Criticalへ移行しHeartbeatとOUTの停止を試みる。
        """
        self._service.mark_critical()
        try:
            self._outputs.heartbeat_tick(healthy=False)
        except Exception:
            _LOGGER.exception("failed to stop heartbeat in critical state")
        try:
            self._outputs.shutdown_off()
        except Exception:
            # 物理OFF自体はOutputManager内で先に試行される。履歴DBも壊れて
            # いる場合は再度の例外でdaemonを落とさず、Criticalを維持する。
            _LOGGER.exception("failed to force outputs off in critical state")

    def _record_audit(
        self,
        action: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """操作auditをbest effortで保存し、DB障害時も安全停止を優先する。

        Args:
            action: auditに記録する操作名。
            details: audit recordへ保存する追加情報mapping。

        Returns:
            None。
        """
        try:
            self._audit.record(
                action=action,
                details_json=json.dumps(details or {}, separators=(",", ":")),
                ts_utc_ms=self._clock.now_utc_ms(),
            )
        except sqlite3.Error:
            _LOGGER.exception("failed to write audit action %s", action)

    def _handle_control(self, request: ControlRequest) -> Mapping[str, object]:
        """allowlist済みcontrol commandをdaemon状態へ適用する。

        Args:
            request: 検証済みのloggerd制御要求。

        Returns:
            成功したloggerd応答のdata
            mapping。遠隔拒否とprotocol不整合は例外で通知する。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        if request.command is ControlCommand.STATUS:
            return self._status_payload()
        if request.command is ControlCommand.RESET_COUNTER:
            return self._reset_counter(request)
        if request.command is ControlCommand.STOP_LOGGING:
            self._stop_logging()
            self._record_audit("logging_stopped")
            return self._status_payload()
        if request.command is ControlCommand.START_LOGGING:
            # setup完了とOUT test終了を確認し、pause状態を開始前に保存してから
            # sessionを作る。開始途中で失敗しても次回起動の自動再開を防ぐ。
            # 出力テスト中の明示的な再開はテストを終了させてから新sessionを
            # 作る。テスト状態を残したままloggingへ戻すと、expiry callbackが
            # 通常監視中のOUTを後から変更してしまう。
            setup_completed = self._settings.get_json("setup_completed", False)
            if setup_completed is not True:
                raise RuntimeError("initial setup is not completed")
            self._outputs.end_output_test()
            self._service.leave_maintenance()
            try:
                # 旧DB/初回設定後の明示STARTも、途中失敗で次回起動時に
                # 自動再開しないよう、開始前に停止状態を永続化しておく。
                if not self._logging_is_paused():
                    self._settings.set_json(
                        key="logging_paused",
                        value=True,
                        updated_at_ms=self._clock.now_utc_ms(),
                        audit_action="logging_pause_changed",
                    )
                self._start_logging_session()
                # session/入力/出力の開始が成功した後でだけ、永続停止を解除する。
                self._settings.set_json(
                    key="logging_paused",
                    value=False,
                    updated_at_ms=self._clock.now_utc_ms(),
                    audit_action="logging_pause_changed",
                )
            except Exception:
                # flag保存で失敗した場合はsessionが既に開始しているため、
                # 状態表示だけでなくcallback受付・出力・sessionも停止する。
                try:
                    self._rule_runner.pause()
                except (OSError, RuntimeError, ValueError, sqlite3.Error):
                    _LOGGER.exception("failed to pause rules after resume failure")
                try:
                    self._outputs.disable_heartbeat()
                except (OSError, RuntimeError, ValueError, sqlite3.Error):
                    _LOGGER.exception(
                        "failed to disable heartbeat after resume failure"
                    )
                self._service.abort_start()
                raise
            return self._status_payload()
        if request.command is ControlCommand.TEST_OUTPUT:
            # request schemaで検証済みでもchannelは明示確認し、監査記録は
            # OutputManagerがテスト開始を受理した後だけ残す。
            if request.channel is None:
                raise ValueError("test_output requires channel")
            self._outputs.begin_output_test(
                request.channel,
                request.duration_ms or 5_000,
            )
            self._record_audit(
                "output_test_started",
                {"channel_id": request.channel, "duration_ms": request.duration_ms},
            )
            return self._status_payload()
        if request.command is ControlCommand.END_OUTPUT_TEST:
            self._outputs.end_output_test()
            self._record_audit("output_test_ended")
            return self._status_payload()
        if request.command is ControlCommand.RELOAD_CONFIG:
            self._reload_config()
            return self._status_payload()
        raise ValueError(f"unsupported control command: {request.command}")

    def _stop_logging(self) -> None:
        """受付を先に閉じ、1つの停止障害でも残りのOUT停止とflag保存を試す。

        Args:
            なし。

        Returns:
            None。入力受付を閉じ、OUT停止、Queue保存、pause永続化を行う。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        if self._service.status is not LoggerStatus.LOGGING:
            raise RuntimeError("logger is not logging")
        # 順序は受付停止とpause保存、続いてrule owner/Heartbeat停止。
        # 各停止処理は独立して試し、最初のerrorだけを保持する。
        # 1段階の失敗で後続停止を飛ばすとOUT所有者が残るためである。
        stop_error: Exception | None = None
        pause_attempted = False

        def persist_pause() -> None:
            """入力・OUT停止後、queue保存待ちより前に永続pauseを保存する。

            Args:
                なし。

            Returns:
                None。
            """
            nonlocal stop_error, pause_attempted
            pause_attempted = True
            try:
                self._settings.set_json(
                    key="logging_paused",
                    value=True,
                    updated_at_ms=self._clock.now_utc_ms(),
                    audit_action="logging_pause_changed",
                )
            except Exception as error:
                _LOGGER.exception("failed to save logging pause")
                if stop_error is None:
                    stop_error = error

        # serviceがMAINTENANCEへ遷移すると入力callback受付が閉じる。
        try:
            self._service.enter_maintenance(on_quiesced=persist_pause)
        except Exception as error:
            _LOGGER.exception("failed to stop logging session")
            self._mark_critical()
            if stop_error is None:
                stop_error = error
        finally:
            if not pause_attempted:
                persist_pause()
        # service stop後にも各OUT ownerを明示停止する。1つの例外で残りを省略しない。
        for stop_operation in (
            self._rule_runner.pause,
            self._outputs.disable_heartbeat,
        ):
            try:
                stop_operation()
            except Exception as error:
                _LOGGER.exception("failed to stop output owner")
                self._mark_critical()
                if stop_error is None:
                    stop_error = error
        # 停止試行をすべて済ませた後に、最初に保存したfailureを呼出側へ返す。
        if stop_error is not None:
            raise stop_error

    def _reset_counter(self, request: ControlRequest) -> Mapping[str, object]:
        """resetをFIFOへ一度だけ投入し、loopを塞がず同じ要求IDで結果を返す。

        Args:
            request: 検証済みのloggerd制御要求。

        Returns:
            成功したloggerd応答のdata
            mapping。遠隔拒否とprotocol不整合は例外で通知する。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        if request.request_id is None or request.widget_id is None:
            raise ValueError("counter reset requires request_id and widget_id")
        self._event_writer.raise_if_failed()
        saved = self._counter_reset_requests.get(request.request_id)
        if saved is None:
            # 再送識別の履歴を上限付きで保持し、完了済みだけを整理する。
            # 未完了requestを追い出すと同一IDの再送で二重resetになる。
            # 応答消失時の再送を二重resetにしない。完了分だけを有界に保持する。
            if len(self._counter_reset_requests) >= 64:
                for key, (_, operation) in tuple(self._counter_reset_requests.items()):
                    if operation.completed.is_set():
                        del self._counter_reset_requests[key]
                        break
            if len(self._counter_reset_requests) >= 64:
                raise RuntimeError("too many pending counter resets")
            # caller snapshotがなければDBから読み、reset modeのcounterだけ受け付ける。
            widget = request.widget or WidgetRepository(self._connection).get(
                request.widget_id
            )
            if (
                widget is None
                or widget.id != request.widget_id
                or widget.widget_type is not WidgetType.EVENT_COUNT
                or widget.config.get("count_mode") != "reset"
            ):
                raise ValueError("widget is not a reset-based counter")
            # reset recordをFIFOへ一度だけ追加し、HTTP/IPC loopはcommitを待たない。
            pending = PendingCounterReset(
                widget_id=request.widget_id,
                reset_at_ms=self._clock.now_utc_ms(),
                widget=request.widget,
            )
            self._service.enqueue_counter_reset(pending)
            self._counter_reset_requests[request.request_id] = (request, pending)
        else:
            previous, pending = saved
            if previous != request:
                raise ValueError(
                    "counter reset request_id was reused with other values"
                )
        # 同じrequest IDの再送は既存operationを参照し、未完了ならpendingを返す。
        if not pending.completed.is_set():
            return {"pending": True}
        if pending.error is not None:
            raise RuntimeError("counter reset could not be saved") from pending.error
        if pending.result is None:
            raise RuntimeError("counter reset has no result")
        result = pending.result
        return {
            "pending": False,
            "reset": {
                "id": result.id,
                "widget_id": result.widget_id,
                "reset_at_ms": result.reset_at_ms,
                "after_event_id": result.after_event_id,
                "carried_count": result.carried_count,
            },
        }

    def _start_logging_session(self) -> None:
        """現在入力のsnapshotを保存し、callbackを接続して監視を開始する。

        Args:
            なし。

        Returns:
            None。入力snapshotを保存してcallbackを有効化し、新sessionを開始する。
        """
        # 開始段階は入力snapshot共有、rule/service初期化、callback接続、
        # 差分再読、OUT開始の順。
        # session開始とrule evaluator開始で異なるsnapshotを読むと、同時に
        # 変化した入力だけが片方へ反映されるため、一つのsnapshotを共有する。
        input_values = self._gpio.input_values()
        try:
            # 再開時は前sessionのcallbackが既に接続されている。入力受付が
            # LOGGINGへ変わる前にrule側も初期化し、確定edgeを後から消さない。
            self._rule_runner.start_session(
                {
                    channel_id: input_values[channel_id]
                    for channel_id in self._service.enabled_channel_ids
                }
            )
            self._service.start(input_values)
            # 初回設定後のSTART_LOGGINGでも必ずcallbackを接続する。callback登録前
            # の変化は再読してdebounce候補へ渡し、監視開始直後のedgeを欠落させない。
            self._gpio.attach_input_callbacks(self._on_input)
            with self._input_callback_lock:
                read_sequences = dict(self._input_sequences)
            current_values = self._gpio.input_values()
            with self._input_callback_lock:
                for channel_id, value in current_values.items():
                    # 値を読んでから適用するまでに届いたcallbackを優先する。
                    # 古いON再読が新しいOFFを消すと、次のedgeまで誤状態が残る。
                    if self._input_sequences.get(channel_id, 0) != (
                        read_sequences.get(channel_id, 0)
                    ):
                        continue
                    if value != input_values.get(channel_id):
                        self._service.observe_input(channel_id, value)
            self._apply_heartbeat_settings()
            self._outputs.resume_heartbeat()
            self._record_audit("logging_started")
        except Exception:
            # snapshot後のrule/heartbeat/GPIO接続に失敗した場合は、未監視のまま
            # loggingを名乗らない。開始途中のsessionは異常終了として残し、
            # 次回の明示STARTで新しいsnapshotを作る。
            try:
                self._rule_runner.pause()
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                _LOGGER.exception("failed to release rule owners after start failure")
            try:
                self._outputs.disable_heartbeat()
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                _LOGGER.exception("failed to disable heartbeat after start failure")
            self._service.abort_start()
            raise

    def _reload_config(self) -> None:
        """チャンネル設定を読み直し、次回再開時に適用する。

        Args:
            なし。

        Returns:
            None。DB設定を読み直し、停止中のdaemon設定へ反映する。

        Raises:
            RuntimeError: 必要なservice、owner
                、writerなどの状態が成立していない場合、または
                副作用処理に失敗した場合。
        """
        channels = self._channels.list_all()
        rules = self._rule_repository.list_all()
        heartbeat = self._heartbeat_settings()
        self._rule_runner.validate_configuration(
            rules,
            heartbeat[1] if heartbeat[0] else None,
        )
        enabled_channels = {
            channel.id: channel.debounce_ms for channel in channels if channel.enabled
        }
        # 監視中のdebounce stateを途中で書き換えず、停止後の次sessionへ適用する。
        # 名称やルールだけの変更は通常監視を止めずに反映できるが、入力の
        # 有効/無効変更はsnapshotとUNKNOWN境界を作るため停止中に限定する。
        if self._service.status is LoggerStatus.LOGGING:
            if self._service.enabled_channels != enabled_channels:
                raise RuntimeError(
                    "changing enabled inputs or debounce requires logging to be stopped"
                )
        else:
            self._service.reload_channels(enabled_channels)
        try:
            self._rule_runner.configure(rules)
        except OutputRuleConfigurationError:
            _LOGGER.exception("rule owner rollback incomplete during config reload")
            self._mark_critical()
            raise
        if self._service.status is not LoggerStatus.MAINTENANCE:
            self._apply_heartbeat_settings()
        self._configuration_digest = configuration_fingerprint(
            channels,
            rules,
            {
                "enabled": heartbeat[0],
                "channel_id": heartbeat[1],
                "interval_ms": heartbeat[2],
            },
        )

    def _apply_heartbeat_settings(self) -> None:
        """app_settingsのHeartbeat設定を検証してOutputManagerへ反映する。

        Args:
            なし。

        Returns:
            None。
        """
        enabled, channel, interval = self._heartbeat_settings()
        self._rule_runner.validate_configuration(
            self._rule_runner.rules,
            channel if enabled else None,
        )
        self._outputs.apply_heartbeat_settings(
            enabled=enabled,
            channel_id=channel,
            interval_ms=interval,
        )

    def _heartbeat_settings(self) -> tuple[bool, int | None, int]:
        """保存済みHeartbeat設定を型・範囲検証して返す。

        Args:
            なし。

        Returns:
            検証済みHeartbeat有効状態、channel ID、周期msのtuple。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        raw_settings = self._settings.get_json(
            "heartbeat",
            {"enabled": False, "channel_id": None, "interval_ms": 5_000},
        )
        if not isinstance(raw_settings, dict):
            raise ValueError("heartbeat setting must be an object")
        enabled = raw_settings.get("enabled", False)
        channel = raw_settings.get("channel_id")
        interval = raw_settings.get("interval_ms", 5_000)
        if not isinstance(enabled, bool):
            raise ValueError("heartbeat_enabled must be boolean")
        if channel is not None and (
            isinstance(channel, bool) or not isinstance(channel, int)
        ):
            raise ValueError("heartbeat_channel must be integer or null")
        if isinstance(interval, bool) or not isinstance(interval, int):
            raise ValueError("heartbeat_interval_ms must be integer")
        if channel is not None and not 1 <= channel <= 6:
            raise ValueError("heartbeat_channel must be between 1 and 6")
        if not 1_000 <= interval <= 60_000:
            raise ValueError("heartbeat_interval_ms must be between 1000 and 60000")
        return enabled, channel, interval

    def _status_payload(self) -> dict[str, object]:
        """Web診断へ返す最小状態。

        Args:
            なし。

        Returns:
            loggerd状態をWeb診断へ返すJSON互換mapping。
        """
        write_latency = self._event_writer.latency_snapshot()
        oldest_pending_ms = self._event_queue.oldest_pending_ms()
        if self._service.status is LoggerStatus.LOGGING:
            # logging中はdebounce確定済み状態とmonotonic起点を返す。
            # retentionに影響されるevent履歴から現在値を再構成しない。
            # dashboardの現在値はGPIOの生値ではなく、debounceを通過して
            # Queueへ受理された論理値を使う。DB retention後も現在状態を
            # UNKNOWNへ戻さず、表示時刻は別のmonotonic mapで計算できる。
            input_values = self._service.confirmed_inputs
            input_since_ns = self._service.confirmed_input_since_ns
        else:
            # 停止・maintenance中は物理入力を読み、継続時間を推測しない。
            input_values = self._gpio.input_values()
            input_since_ns = {}
        return {
            "logger_status": self._service.status.value,
            "app_version": self._app_version,
            "configuration_digest": self._configuration_digest,
            "session_id": self._service.session_id,
            "pending_event_count": self._service.pending_event_count,
            "write_latency": {
                "delayed_count_5m": write_latency.delayed_count,
                "max_delay_ms_5m": write_latency.max_delay_ms,
                "last_commit_ms": write_latency.last_commit_ms,
                "in_progress_ms": write_latency.in_progress_ms,
                "oldest_pending_ms": oldest_pending_ms,
                "warning": write_latency.warning,
                "severity": write_latency.severity,
                "queue_capacity": self._event_queue.capacity,
            },
            "inputs": {
                str(channel_id): int(value)
                for channel_id, value in sorted(input_values.items())
            },
            "input_since_mono_ns": {
                str(channel_id): since_ns
                for channel_id, since_ns in sorted(input_since_ns.items())
            },
            "outputs": {
                str(channel_id): int(output.is_active)
                for channel_id, output in sorted(self._gpio.output_ports.items())
            },
            "heartbeat_channel": self._outputs.heartbeat_channel,
            "heartbeat_interval_ms": self._outputs.heartbeat_interval_ms,
            "output_test_channel": self._outputs.output_test_channel,
        }


def create_demo_daemon(
    *, database_path: str | Path, socket_path: str | Path
) -> LoggerDaemon:
    """GPIOなしのdaemonを作成する。

    Args:
        database_path: SQLite databaseのfilesystem path。
        socket_path: 接続先またはlistenするUnix socket path。

    Returns:
        FakeGPIO、FakeClockを使うdemo用daemon。
    """
    daemon = LoggerDaemon(
        database_path=database_path,
        socket_path=socket_path,
        gpio=FakeGPIO(),
        clock=FakeClock(1_700_000_000_000, 0, True),
        boot_id="demo-boot",
    )
    # demo daemonは固定fixtureを直ちに確認できる既設定環境として扱う。
    # 本番の初回起動はsetup_completedが未設定のためmaintenanceで待機する。
    if daemon._settings.get_json("setup_completed") is None:
        daemon._settings.set_json(
            key="setup_completed",
            value=True,
            updated_at_ms=1_700_000_000_000,
            audit_action="demo_setup_completed",
        )
    return daemon


def main() -> None:
    """環境変数からdaemonを構成して常駐させる。

    Args:
        なし。

    Returns:
        None。
    """
    parser = argparse.ArgumentParser(description="24V event logger daemon")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    demo = args.demo or os.environ.get("EVENT_LOGGER_DEMO") == "1"
    database_path = os.environ.get(
        "EVENT_LOGGER_DB",
        ":memory:" if demo else "/var/lib/event-logger/logger.db",
    )
    socket_path = os.environ.get(
        "EVENT_LOGGER_SOCKET",
        "/tmp/event-logger-demo.sock" if demo else "/run/event-logger/control.sock",
    )
    stop_event = threading.Event()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signal_number, lambda _signum, _frame: stop_event.set())
    if demo:
        daemon = create_demo_daemon(
            database_path=database_path,
            socket_path=socket_path,
        )
    else:
        daemon = LoggerDaemon(database_path=database_path, socket_path=socket_path)
    daemon.run(stop_event)


def _read_boot_id() -> str:
    """Linux boot_idを読み、テスト環境ではプロセス固有値へフォールバックする。

    Args:
        なし。

    Returns:
        OS boot ID。Linux以外ではprocess固有fallback値。
    """
    try:
        value = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
    except OSError:
        value = ""
    return value or f"process-{os.getpid()}"


if __name__ == "__main__":
    main()
