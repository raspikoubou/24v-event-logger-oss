"""loggerd制御クライアント、設定反映と異常時の復旧処理。

FlaskのHTTP route登録を持たず、WebContextの依存先を通じて制御する。
停止状態・出力OFF確認・設定反映ACKの判断は移動前の実装を維持する。
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol, cast

from flask import Flask

from event_logger.common.configuration import configuration_fingerprint
from event_logger.common.models import ChannelConfig, CounterReset
from event_logger.domain.widgets import Widget
from event_logger.loggerd.ipc import (
    ControlCommand,
    ControlRequest,
    IpcProtocolError,
    IpcRemoteError,
    UnixControlClient,
)

if TYPE_CHECKING:
    from event_logger.web.app import WebContext


_LOGGER = logging.getLogger("event_logger.web.app")


class ControlClient(Protocol):
    """Webがloggerdへ依頼するための最小port。"""

    def request(self, request: ControlRequest) -> Mapping[str, object]:
        """制御要求を送信して応答dataを返す。"""


def _control_client(app: Flask, demo: bool) -> ControlClient | None:
    """設定から制御clientを作る。demoではGPIOなしFakeを使う。"""
    configured = app.config.get("CONTROL_CLIENT")
    if configured is not None:
        if not hasattr(configured, "request"):
            raise TypeError("CONTROL_CLIENT must provide request()")
        return cast(ControlClient, configured)
    socket_path = app.config.get("CONTROL_SOCKET_PATH")
    if socket_path:
        # loggerdは入力・OUT履歴を同期commitする。短いI/O遅延でACKだけを
        # 失って操作結果が曖昧にならないよう、SQLiteのbusy timeoutより長く待つ。
        return UnixControlClient(str(socket_path), timeout=6.0)
    return _DemoControlClient() if demo else None


class _DemoControlClient:
    """Web demo専用のloggerd状態Fake。"""

    def __init__(self) -> None:
        """初期状態を通常ロギングにする。"""
        self._lock = threading.RLock()
        self._status = "logging"
        self._test_channel: int | None = None
        self._configuration_digest_provider: Callable[[], str] | None = None

    def set_configuration_digest_provider(self, provider: Callable[[], str]) -> None:
        """Webが保持する現在設定のfingerprint供給元を接続する。"""
        with self._lock:
            self._configuration_digest_provider = provider

    def _configuration_digest(self) -> str:
        """demo Web設定をloggerdが適用したfingerprintとして返す。"""
        with self._lock:
            provider = self._configuration_digest_provider
        if provider is None:
            return ""
        return provider()

    def request(self, control_request: ControlRequest) -> Mapping[str, object]:
        """制御要求を固定Fake状態へ適用する。"""
        with self._lock:
            return self._request_locked(control_request)

    def _request_locked(
        self,
        control_request: ControlRequest,
    ) -> Mapping[str, object]:
        """Fake stateを一つの原子的な制御要求として更新・snapshotする。"""
        command = control_request.command
        if command is ControlCommand.STATUS:
            return {
                "logger_status": self._status,
                "configuration_digest": self._configuration_digest(),
                "session_id": 1 if self._status == "logging" else None,
                "output_test_channel": self._test_channel,
                "inputs": {str(channel_id): 0 for channel_id in range(1, 7)},
                "input_since_mono_ns": {
                    str(channel_id): 0 for channel_id in range(1, 7)
                },
                "outputs": {
                    str(channel_id): int(channel_id == self._test_channel)
                    for channel_id in range(1, 7)
                },
            }
        if command is ControlCommand.STOP_LOGGING:
            if self._status != "logging":
                raise RuntimeError("logger is not logging")
            self._status = "maintenance"
            self._test_channel = None
        elif command is ControlCommand.START_LOGGING:
            if self._status != "maintenance":
                raise RuntimeError("logger is not in maintenance")
            self._status = "logging"
        elif command is ControlCommand.TEST_OUTPUT:
            if self._status != "maintenance":
                raise RuntimeError("output test is available only in maintenance")
            self._test_channel = control_request.channel
        elif command is ControlCommand.END_OUTPUT_TEST:
            self._test_channel = None
        elif command is ControlCommand.RELOAD_CONFIG:
            pass
        return {
            "logger_status": self._status,
            "configuration_digest": self._configuration_digest(),
            "session_id": 1 if self._status == "logging" else None,
            "output_test_channel": self._test_channel,
            "inputs": {str(channel_id): 0 for channel_id in range(1, 7)},
            "outputs": {
                str(channel_id): int(channel_id == self._test_channel)
                for channel_id in range(1, 7)
            },
        }


def _daemon_status(
    context: WebContext,
) -> tuple[str, str | None, Mapping[str, object]]:
    """loggerd statusを読み、IPC失敗時はCritical表示へ倒す。"""
    if context.control_client is None:
        return context.status_provider(), None, {}
    try:
        data = context.control_client.request(ControlRequest(ControlCommand.STATUS))
    except (OSError, IpcProtocolError, IpcRemoteError) as error:
        return "unknown", str(error), {}
    raw_status = data.get("logger_status")
    if not isinstance(raw_status, str):
        return "unknown", "loggerd status response is invalid", {}
    return raw_status, None, data


def _request_config_reload(context: WebContext) -> Mapping[str, object]:
    """DBに保存した現行設定をloggerdへ再読込させる。"""
    if context.control_client is None:
        raise ConnectionError("loggerd reload was not acknowledged")
    expected_digest = _configuration_fingerprint(context)
    result = context.control_client.request(
        ControlRequest(ControlCommand.RELOAD_CONFIG)
    )
    if result.get("configuration_digest") != expected_digest:
        raise IpcProtocolError(
            "loggerd applied configuration does not match Web database"
        )
    _clear_configuration_out_of_sync(context)
    return result


def _reload_after_rollback(context: WebContext) -> bool:
    """旧設定の再読込を確認し、失敗時は停止と診断状態を残す。"""
    expected_digest = _configuration_fingerprint(context)
    try:
        _request_config_reload(context)
    except TimeoutError:
        # daemonが設定を反映した直後にACKだけ失った場合は、STATUSのdigestで
        # 旧設定への復旧を確認する。確認できた要求を重ねて送らない。
        try:
            if context.control_client is not None:
                status = context.control_client.request(
                    ControlRequest(ControlCommand.STATUS)
                )
                if status.get("configuration_digest") == expected_digest:
                    _clear_configuration_out_of_sync(context)
                    return True
        except (OSError, IpcProtocolError, IpcRemoteError) as error:
            _LOGGER.warning("rollback configuration status unavailable: %s", error)
    except (OSError, IpcProtocolError, IpcRemoteError) as error:
        _LOGGER.warning("rollback configuration reload failed: %s", error)
    else:
        return True
    # 通信または適用状態が確認できないdaemonを正常な設定状態と見せない。
    # STOP ACKが得られた場合だけ停止確認済みと記録する。
    stopped = _stop_for_configuration_recovery(context)
    _mark_configuration_out_of_sync(context, stopped=stopped)
    return False


def _stop_for_configuration_recovery(context: WebContext) -> bool:
    """停止または出力テスト終了のACKで通常OUTのOFF状態を確認する。"""
    if context.control_client is None:
        return False
    try:
        status = context.control_client.request(ControlRequest(ControlCommand.STATUS))
        logger_status = status.get("logger_status")
        if logger_status == "logging":
            status = context.control_client.request(
                ControlRequest(ControlCommand.STOP_LOGGING)
            )
            return status.get(
                "logger_status"
            ) == "maintenance" and _outputs_confirmed_off(status)
        if logger_status != "maintenance":
            return False
        if status.get("output_test_channel") is not None:
            status = context.control_client.request(
                ControlRequest(ControlCommand.END_OUTPUT_TEST)
            )
        return _outputs_confirmed_off(status)
    except (OSError, IpcProtocolError, IpcRemoteError) as error:
        _LOGGER.warning("loggerd stop state could not be confirmed: %s", error)
        return False


def _outputs_confirmed_off(status: Mapping[str, object]) -> bool:
    """loggerd STATUSの6出力値とtest状態がすべてOFFかを検証する。"""
    outputs = status.get("outputs")
    if not isinstance(outputs, Mapping):
        return False
    return status.get("output_test_channel") is None and all(
        outputs.get(str(channel_id)) == 0 for channel_id in range(1, 7)
    )


def _mark_configuration_out_of_sync(context: WebContext, *, stopped: bool) -> None:
    """daemon設定の不一致を永続化し、diagnosticsで判断できるようにする。"""
    message = (
        "設定の再反映を確認できません。記録停止を確認しました。設定を再読込してください。"
        if stopped
        else (
            "設定の再反映も記録停止も確認できません。"
            "OUT状態を実機で確認し、設定を再読込してください。"
        )
    )
    state = {
        "out_of_sync": True,
        "stopped": stopped,
        "message": message,
    }
    if context.settings.get_json("daemon_config_sync", None) == state:
        return
    _LOGGER.warning("loggerd configuration is out of sync: %s", state["message"])
    context.settings.set_json(
        key="daemon_config_sync",
        value=state,
        updated_at_ms=context.clock.now_utc_ms(),
        audit_action="daemon_config_sync_failed",
    )


def _clear_configuration_out_of_sync(context: WebContext) -> None:
    """loggerdのreload ACK後に不一致警告を解除する。"""
    current = context.settings.get_json("daemon_config_sync", None)
    if not isinstance(current, dict) or current.get("out_of_sync") is not True:
        return
    context.settings.set_json(
        key="daemon_config_sync",
        value={"out_of_sync": False, "stopped": False, "message": ""},
        updated_at_ms=context.clock.now_utc_ms(),
        audit_action="daemon_config_sync_recovered",
    )


def _configuration_fingerprint(context: WebContext) -> str:
    """Web DBが期待するloggerd設定のfingerprintを作る。"""
    heartbeat = context.settings.get_json(
        "heartbeat",
        {"enabled": False, "channel_id": None, "interval_ms": 5_000},
    )
    if not isinstance(heartbeat, dict):
        raise ValueError("stored heartbeat setting is invalid")
    normalized_heartbeat = {
        "enabled": heartbeat.get("enabled", False),
        "channel_id": heartbeat.get("channel_id"),
        "interval_ms": heartbeat.get("interval_ms", 5_000),
    }
    return configuration_fingerprint(
        context.channels.list_all(),
        context.output_rules.list_all(),
        normalized_heartbeat,
    )


def _restore_channel(context: WebContext, channel: ChannelConfig) -> None:
    """reload失敗時にtransactionで変更前channelへ戻す。"""
    context.channels.update(
        channel_id=channel.id,
        enabled=channel.enabled,
        name=channel.name,
        debounce_ms=channel.debounce_ms,
        on_label=channel.on_label,
        off_label=channel.off_label,
        updated_at_ms=context.clock.now_utc_ms(),
        audit_action="channel_update_rollback",
    )


def _restore_heartbeat(context: WebContext, value: Mapping[str, object]) -> None:
    """Heartbeat reload失敗時に変更前JSONへ戻す。"""
    context.settings.set_json(
        key="heartbeat",
        value=dict(value),
        updated_at_ms=context.clock.now_utc_ms(),
        audit_action="heartbeat_update_rollback",
    )


def _save_counter_reset(
    context: WebContext, widget_id: str, *, widget: Widget | None = None
) -> CounterReset:
    """確定入力と同じFIFOのresetを依頼し、制御loopを止めず完了を待つ。"""
    if context.demo or (
        context.database_path == ":memory:" and context.control_client is None
    ):
        # 固定fixtureと接続ごと独立するmemory DBには別processのwriterがない。
        # GPIO/IPCを要求せず、ファイルDBの本番経路だけをFIFOへ同期する。
        if widget is None:
            return context.counter_resets.create(
                widget_id=widget_id, reset_at_ms=context.clock.now_utc_ms()
            )
        context.widgets.update_with_counter_reset(
            widget, reset_at_ms=context.clock.now_utc_ms()
        )
        result = context.counter_resets.latest(widget_id)
        if result is None:
            raise RuntimeError("counter reset was not saved")
        return result
    if context.control_client is None:
        raise ConnectionError("loggerd counter reset is unavailable")
    command = ControlRequest(
        ControlCommand.RESET_COUNTER,
        request_id=secrets.token_hex(16),
        widget_id=widget_id,
        widget=widget,
    )
    deadline = time.monotonic() + 30.0
    pause = threading.Event()
    while True:
        try:
            result_data = context.control_client.request(command)
        except TimeoutError:
            # 送信後の応答消失でも、同じIDなら新しい基準を二重作成しない。
            result_data = {"pending": True}
        if result_data.get("pending") is False:
            raw = result_data.get("reset")
            if not isinstance(raw, Mapping) or raw.get("widget_id") != widget_id:
                raise IpcProtocolError("counter reset response is invalid")
            values = [
                raw.get(key)
                for key in ("id", "reset_at_ms", "after_event_id", "carried_count")
            ]
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in values
            ):
                raise IpcProtocolError("counter reset values are invalid")
            return CounterReset(
                id=cast(int, values[0]),
                widget_id=widget_id,
                reset_at_ms=cast(int, values[1]),
                after_event_id=cast(int, values[2]),
                carried_count=cast(int, values[3]),
            )
        if result_data.get("pending") is not True:
            raise IpcProtocolError("counter reset status is invalid")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "リセット結果を確認できません。表示値を確認してください。"
            )
        pause.wait(0.025)
