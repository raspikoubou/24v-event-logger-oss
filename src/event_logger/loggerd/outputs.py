"""OUT状態、Heartbeat、診断テストの副作用を一箇所へ集約する。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Protocol

from event_logger.adapters.clock import Clock
from event_logger.common.models import OutputSource
from event_logger.database.repositories import OutputEventRepository
from event_logger.domain.outputs import (
    OutputOwnerKind,
    OutputOwnerRegistry,
    OutputTest,
)
from event_logger.hardware.ports import OutputPort
from event_logger.loggerd.event_queue import PendingOutputEvent

_LOGGER = logging.getLogger(__name__)
_SLOW_OUTPUT_OPERATION_NS = 250_000_000
_OUTPUT_TEST_TIMER_GUARD_MAX_MS = 50


class OutputEventSink(Protocol):
    """OUT履歴を制御loop外のsingle DB writerへ渡す境界。"""

    def enqueue_output(self, event: PendingOutputEvent) -> None:
        """OUT履歴eventをcommit Queueへ入れる。"""


class OutputManager:
    """OUT所有ルールを守りながら論理出力を変更する。"""

    def __init__(
        self,
        *,
        clock: Clock,
        outputs: Mapping[int, OutputPort],
        repository: OutputEventRepository,
        owner_registry: OutputOwnerRegistry,
        session_id_provider: Callable[[], int | None],
        maintenance_mode_provider: Callable[[], bool],
        event_sink: OutputEventSink | None = None,
    ) -> None:
        """出力port、履歴Repository、現在session providerを注入する。"""
        self._clock = clock
        self._outputs = dict(outputs)
        self._repository = repository
        self._owners = owner_registry
        self._session_id_provider = session_id_provider
        self._maintenance_mode_provider = maintenance_mode_provider
        self._event_sink = event_sink
        self._heartbeat_channel: int | None = None
        self._heartbeat_interval_ms = 5_000
        self._heartbeat_next_ns: int | None = None
        self._output_test: OutputTest | None = None
        self._output_test_timer: threading.Timer | None = None
        self._output_test_watchdog_expired = False
        self._output_test_lock = threading.RLock()
        # Timer threadとdaemon loopが同じGPIO portを触るため、物理操作だけを
        # 直列化する。SQLiteの同期commit待ちでwatchdogのOFFが遅れないよう、
        # productionでは履歴eventをlock外から専用writer queueへ渡す。
        self._physical_lock = threading.Lock()
        self._emergency_stopped = False
        self._emergency_off_events: list[
            tuple[int, OutputSource, int, int, bool, int | None]
        ] = []

    def has_active_output(self) -> bool:
        """物理OUTのいずれかがONか、watchdog用に取得する。"""
        with self._physical_lock:
            return any(output.is_active for output in self._outputs.values())

    def emergency_off(self) -> bool:
        """制御loopが停止した場合、DBを待たずに全OUTをOFFへ戻す。"""
        session_id = self._session_id_provider()
        synced = self._clock.is_synchronized()
        # 診断テストも同じ順序（test→physical）でlockを取得する。
        with self._output_test_lock, self._physical_lock:
            if self._emergency_stopped:
                return False
            self._emergency_stopped = True
            first_error: Exception | None = None
            for channel_id, output in self._outputs.items():
                if not output.is_active:
                    continue
                try:
                    output.off()
                except Exception as error:
                    # 1点のGPIO異常でも残りのOUTのOFF試行を続ける。
                    if first_error is None:
                        first_error = error
                    continue
                source = (
                    OutputSource.HEARTBEAT
                    if channel_id == self._heartbeat_channel
                    else OutputSource.TEST
                    if self._output_test is not None
                    and self._output_test.channel_id == channel_id
                    else OutputSource.RULE
                )
                self._emergency_off_events.append(
                    (
                        channel_id,
                        source,
                        self._clock.now_utc_ms(),
                        self._clock.now_monotonic_ns(),
                        synced,
                        session_id,
                    )
                )
            if first_error is not None:
                raise first_error
            return True

    def flush_emergency_off_events(self) -> None:
        """watchdogが物理OFFを済ませた時刻を、loop復帰後に履歴化する。"""
        while self._emergency_off_events:
            channel_id, source, utc_ms, mono_ns, synced, session_id = (
                self._emergency_off_events[0]
            )
            self._persist_output_event(
                PendingOutputEvent(
                    session_id=session_id,
                    ts_utc_ms=utc_ms,
                    mono_ns=mono_ns,
                    channel_id=channel_id,
                    value=0,
                    source=source,
                    clock_synced=synced,
                )
            )
            self._emergency_off_events.pop(0)

    @property
    def heartbeat_channel(self) -> int | None:
        """Heartbeat割当OUTを返す。"""
        return self._heartbeat_channel

    @property
    def heartbeat_interval_ms(self) -> int | None:
        """Heartbeat周期を返す。"""
        return (
            self._heartbeat_interval_ms if self._heartbeat_channel is not None else None
        )

    @property
    def output_test_channel(self) -> int | None:
        """進行中の出力テストOUTを返す。"""
        with self._output_test_lock:
            return (
                self._output_test.channel_id if self._output_test is not None else None
            )

    def startup_off(self) -> None:
        """daemon起動時に全OUTをOFFへ戻し、実状態を履歴へ記録する。"""
        for channel_id in sorted(self._outputs):
            self._set_state(channel_id, False, OutputSource.STARTUP, force_record=True)

    def shutdown_off(self) -> None:
        """正常終了時に全OUTをOFFへ戻す。"""
        first_error: Exception | None = None
        try:
            for channel_id in sorted(self._outputs):
                try:
                    self._set_state(
                        channel_id,
                        False,
                        OutputSource.STARTUP,
                        force_record=False,
                    )
                except Exception as error:
                    # 1 OUTの履歴DB失敗で残りの物理OUTのOFF試行を止めない。
                    # 最後に最初の例外を返し、呼び出し側はCriticalを維持する。
                    if first_error is None:
                        first_error = error
        finally:
            # 異常終了経路でも、物理状態だけOFFになって期限処理が残らない
            # よう、内部の診断テスト状態を破棄する。
            self._clear_output_test()
        if first_error is not None:
            raise first_error

    def configure_heartbeat(self, channel_id: int, interval_ms: int = 5_000) -> None:
        """Heartbeat ownerと反転周期を設定する。"""
        if not 1_000 <= interval_ms <= 60_000:
            raise ValueError("heartbeat interval must be between 1000 and 60000ms")
        self._owners.assign_heartbeat(channel_id)
        self._heartbeat_channel = channel_id
        self._heartbeat_interval_ms = interval_ms
        self._heartbeat_next_ns = (
            self._clock.now_monotonic_ns() + interval_ms * 1_000_000
        )

    def apply_heartbeat_settings(
        self,
        *,
        enabled: bool,
        channel_id: int | None = None,
        interval_ms: int = 5_000,
    ) -> None:
        """保存済みHeartbeat設定をownerと周期へ反映する。"""
        if (
            enabled
            and self._heartbeat_channel == channel_id
            and self._heartbeat_interval_ms == interval_ms
        ):
            # 名称変更や別設定のreloadで、同じHeartbeatを一度OFFにして
            # 再割当すると不要なoutput eventと短い停止を発生させる。
            return
        if not enabled and self._heartbeat_channel is None:
            return
        if self._heartbeat_channel is not None:
            self.disable_heartbeat()
        if enabled:
            if channel_id is None:
                raise ValueError("enabled heartbeat requires channel_id")
            self.configure_heartbeat(channel_id, interval_ms)

    def resume_heartbeat(self) -> None:
        """新session開始後にHeartbeat反転の起点を作り直す。"""
        if self._heartbeat_channel is not None:
            self._heartbeat_next_ns = (
                self._clock.now_monotonic_ns() + self._heartbeat_interval_ms * 1_000_000
            )

    def disable_heartbeat(self) -> None:
        """Heartbeatを停止し、割当を解放する。"""
        if self._heartbeat_channel is not None:
            self._set_state(
                self._heartbeat_channel,
                False,
                OutputSource.HEARTBEAT,
                force_record=False,
            )
            self._owners.release(self._heartbeat_channel)
        self._heartbeat_channel = None
        self._heartbeat_next_ns = None

    def heartbeat_tick(self, healthy: bool) -> bool:
        """Heartbeatを必要な時だけ反転する。

        DB書込みやQueue処理が正常でない場合は反転を停止し、可能ならOFFにする。
        """
        if not healthy or self._heartbeat_channel is None:
            if self._heartbeat_channel is not None:
                self._set_state(
                    self._heartbeat_channel,
                    False,
                    OutputSource.HEARTBEAT,
                    force_record=False,
                )
                self._heartbeat_next_ns = None
            return False
        now_ns = self._clock.now_monotonic_ns()
        if self._heartbeat_next_ns is None or now_ns < self._heartbeat_next_ns:
            return False
        channel_id = self._heartbeat_channel
        self._set_state(
            channel_id,
            not self._outputs[channel_id].is_active,
            OutputSource.HEARTBEAT,
            force_record=False,
        )
        self._heartbeat_next_ns = now_ns + self._heartbeat_interval_ms * 1_000_000
        return True

    def begin_output_test(self, channel_id: int, duration_ms: int = 5_000) -> None:
        """メンテナンス中だけ指定OUTの一時テストを開始する。"""
        if not self._maintenance_mode_provider():
            raise RuntimeError("output test is available only in maintenance mode")
        if self._heartbeat_channel is not None:
            raise RuntimeError("heartbeat must be stopped before output test")
        if self._owners.owner(channel_id).kind is not OutputOwnerKind.UNUSED:
            raise RuntimeError("output is owned by another logger function")
        output_test = OutputTest(
            channel_id=channel_id,
            started_ns=self._clock.now_monotonic_ns(),
            duration_ms=duration_ms,
        )
        # 履歴DBやIPCがブロックしても物理出力を5秒以内にOFFへ戻すため、
        # 同期的なrepository.appendの前にGPIO専用watchdogを起動する。
        with self._output_test_lock:
            # 2つのWeb/IPC要求が同時に来ても、checkと状態登録を同じlock内で
            # 行い、診断テストを二重開始しない。
            if self._output_test is not None:
                raise RuntimeError("another output test is already running")
            self._output_test = output_test
            self._output_test_watchdog_expired = False
            # threading.Timerは指定時刻どおりに起床する保証がなく、Pi実測でも
            # 5,000ms指定のloopbackがわずかに上限を越えた。最大50ms（指定時間
            # の1%）早く物理OFFを予約し、1ms指定だけは最小1msを保つ。
            timer_guard_ms = min(
                duration_ms - 1,
                _OUTPUT_TEST_TIMER_GUARD_MAX_MS,
                max(1, duration_ms // 100),
            )
            watchdog_delay_ms = max(1, duration_ms - timer_guard_ms)
            timer = threading.Timer(
                watchdog_delay_ms / 1000,
                self._output_test_watchdog,
                args=(channel_id,),
            )
            timer.daemon = True
            self._output_test_timer = timer
            timer.start()
            try:
                # watchdog callbackはこのlockを取得できるまで待つため、物理ON
                # より先に物理OFFを実行する競合窓を作らない。
                with self._physical_lock:
                    if self._emergency_stopped:
                        raise RuntimeError("outputs are emergency-stopped")
                    self._outputs[channel_id].on()
            except Exception:
                with self._physical_lock:
                    self._outputs[channel_id].off()
                self._clear_output_test()
                raise
        try:
            self._record_output_event(channel_id, True, OutputSource.TEST)
        except Exception:
            # 同じtokenのテストだけを片付ける。履歴書込み中に別要求が
            # 先に終了・再開始した場合、その新しいテストをOFFにしない。
            with self._output_test_lock:
                if self._output_test is output_test:
                    with self._physical_lock:
                        self._outputs[channel_id].off()
                    self._clear_output_test()
            raise
        with self._output_test_lock:
            current = self._output_test is output_test
            watchdog_expired = self._output_test_watchdog_expired
        if (
            current
            and not watchdog_expired
            and output_test.is_expired(self._clock.now_monotonic_ns())
        ):
            # FakeClockや、履歴保存中に期限を越えた短い競合窓でも、begin完了
            # 時点で期限超過した物理ONを残さない。
            self._output_test_watchdog(channel_id)
            self.output_test_tick()
        # GPIO/履歴の更新が成功した後は、daemon loopが期限処理を引き継ぐ。

    def output_test_tick(self) -> bool:
        """期限切れの出力テストをOFFにする。"""
        with self._output_test_lock:
            output_test = self._output_test
            watchdog_expired = self._output_test_watchdog_expired
            if output_test is None:
                return False
            if not watchdog_expired and not output_test.is_expired(
                self._clock.now_monotonic_ns()
            ):
                return False
            channel_id = output_test.channel_id
            with self._physical_lock:
                was_active = self._outputs[channel_id].is_active
                self._outputs[channel_id].off()
            # 物理OFFを済ませてからstateをclearする。新しいテストがこのOUTを
            # 再利用した直後に、旧期限処理がそれをOFFにすることを防ぐ。
            self._clear_output_test()
        if was_active or watchdog_expired:
            self._record_output_event(channel_id, False, OutputSource.TEST)
        return True

    def end_output_test(self) -> None:
        """ユーザー操作で出力テストを早期終了する。"""
        with self._output_test_lock:
            output_test = self._output_test
            watchdog_expired = self._output_test_watchdog_expired
            if output_test is None:
                return
            with self._physical_lock:
                was_active = self._outputs[output_test.channel_id].is_active
                self._outputs[output_test.channel_id].off()
            self._clear_output_test()
        if was_active or watchdog_expired:
            self._record_output_event(
                output_test.channel_id,
                False,
                OutputSource.TEST,
            )

    def _output_test_watchdog(self, channel_id: int) -> None:
        """DBへ触れず、期限到達時の物理OFFだけを実行する。"""
        with self._output_test_lock:
            if self._output_test is None or self._output_test.channel_id != channel_id:
                return
            self._output_test_watchdog_expired = True
            with self._physical_lock:
                self._outputs[channel_id].off()

    def _clear_output_test(self) -> None:
        """出力テスト状態とwatchdog timerを破棄する。"""
        with self._output_test_lock:
            timer = self._output_test_timer
            self._output_test_timer = None
            self._output_test = None
            self._output_test_watchdog_expired = False
            if timer is not None:
                timer.cancel()

    def set_rule_state(self, channel_id: int, value: bool) -> None:
        """条件ルール所有OUTをON/OFFする。"""
        owner = self._owners.owner(channel_id)
        if owner.kind is not OutputOwnerKind.RULE:
            raise RuntimeError("output is not owned by a condition rule")
        self._set_state(channel_id, value, OutputSource.RULE, force_record=False)

    def configure_rule(self, rule_id: int, channel_id: int) -> None:
        """条件ルール用OUT ownerを割り当てる。"""
        self._owners.assign_rule(channel_id, rule_id)

    def release_rule(self, channel_id: int, rule_id: int) -> None:
        """一致する条件ルールownerをOFFにして解放する。"""
        owner = self._owners.owner(channel_id)
        if owner.kind is not OutputOwnerKind.RULE or owner.rule_id != rule_id:
            return
        self._set_state(channel_id, False, OutputSource.RULE, force_record=False)
        self._owners.release(channel_id)

    def _set_state(
        self,
        channel_id: int,
        value: bool,
        source: OutputSource,
        *,
        force_record: bool,
    ) -> None:
        """物理出力とoutput_eventsを同じ論理値で更新する。"""
        output = self._outputs[channel_id]
        physical_lock_started_ns = self._clock.now_monotonic_ns()
        with self._physical_lock:
            lock_wait_ns = self._clock.now_monotonic_ns() - physical_lock_started_ns
            if value and self._emergency_stopped:
                raise RuntimeError("outputs are emergency-stopped")
            changed = output.is_active != value
            gpio_started_ns = self._clock.now_monotonic_ns()
            if value:
                output.on()
            else:
                output.off()
            gpio_elapsed_ns = self._clock.now_monotonic_ns() - gpio_started_ns
        self._log_slow_operation(
            stage="physical_lock",
            channel_id=channel_id,
            value=value,
            source=source,
            elapsed_ns=lock_wait_ns,
        )
        self._log_slow_operation(
            stage="gpio",
            channel_id=channel_id,
            value=value,
            source=source,
            elapsed_ns=gpio_elapsed_ns,
        )
        if changed or force_record:
            database_started_ns = self._clock.now_monotonic_ns()
            event_stage = (
                "event_enqueue" if self._event_sink is not None else "event_database"
            )
            try:
                self._record_output_event(channel_id, value, source)
            except Exception:
                database_elapsed_ns = (
                    self._clock.now_monotonic_ns() - database_started_ns
                )
                self._log_slow_operation(
                    stage=event_stage,
                    channel_id=channel_id,
                    value=value,
                    source=source,
                    elapsed_ns=database_elapsed_ns,
                )
                # OUTをONにしたままDBへ履歴を書けない状態を残さない。
                # OFFの履歴まで書ける保証はないため、物理状態をsafe defaultへ戻す。
                with self._physical_lock:
                    output.off()
                raise
            database_elapsed_ns = self._clock.now_monotonic_ns() - database_started_ns
            self._log_slow_operation(
                stage=event_stage,
                channel_id=channel_id,
                value=value,
                source=source,
                elapsed_ns=database_elapsed_ns,
            )

    @staticmethod
    def _log_slow_operation(
        *,
        stage: str,
        channel_id: int,
        value: bool,
        source: OutputSource,
        elapsed_ns: int,
    ) -> None:
        """watchdog予算の半分を使った物理/DB段階を記録する。"""
        if elapsed_ns < _SLOW_OUTPUT_OPERATION_NS:
            return
        _LOGGER.warning(
            "slow output operation: stage=%s channel=%s value=%s source=%s "
            "elapsed_ms=%s",
            stage,
            channel_id,
            int(value),
            source,
            elapsed_ns // 1_000_000,
        )

    def _record_output_event(
        self,
        channel_id: int,
        value: bool,
        source: OutputSource,
    ) -> None:
        """物理操作時刻を含むoutput_eventsをcommit Queueへ渡す。"""
        self._persist_output_event(
            PendingOutputEvent(
                session_id=self._session_id_provider(),
                ts_utc_ms=self._clock.now_utc_ms(),
                mono_ns=self._clock.now_monotonic_ns(),
                channel_id=channel_id,
                value=int(value),
                source=source,
                clock_synced=self._clock.is_synchronized(),
            )
        )

    def _persist_output_event(self, event: PendingOutputEvent) -> None:
        """履歴eventを専用writerへ渡すか、単体テスト用Repositoryへ保存する。"""
        if self._event_sink is not None:
            self._event_sink.enqueue_output(event)
            return
        self._repository.append(
            session_id=event.session_id,
            ts_utc_ms=event.ts_utc_ms,
            mono_ns=event.mono_ns,
            output_channel=event.channel_id,
            value=event.value,
            source=event.source,
            clock_synced=event.clock_synced,
        )
