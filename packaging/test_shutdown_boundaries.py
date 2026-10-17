"""最終監査で見つかったUNKNOWN区間と終了処理の回帰試験。"""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from event_logger.adapters.clock import FakeClock
from event_logger.common.models import InputState, OutputEvent, OutputSource, Session
from event_logger.database.connection import connect_database, initialize_database
from event_logger.database.repositories import SettingsRepository
from event_logger.domain.widget_values import evaluate_widget
from event_logger.domain.widgets import Widget, WidgetSize, WidgetType
from event_logger.hardware.fake_gpio import FakeGPIO
from event_logger.hardware.gpiozero_adapter import GpioZeroHat
from event_logger.loggerd.ipc import ControlCommand, ControlRequest
from event_logger.loggerd.main import LoggerDaemon
from event_logger.web.app import create_app
from event_logger.web.request_context import _ConfigurationRequestMiddleware


class ManualTimer:
    """実時間を待たずに期限処理を検証するTimer。"""

    def __init__(
        self, interval: float, function: Callable[..., None], args: tuple[object, ...]
    ) -> None:
        """起床処理を試験から呼べるよう保存する。"""
        self.function = function
        self.args = args
        self.daemon = True

    def start(self) -> None:
        """試験から明示的にfireするまで待つ。"""

    def cancel(self) -> None:
        """取消済みcallbackもfireできるようにする。"""

    def fire(self) -> None:
        """保存済みの期限callbackを実行する。"""
        self.function(*self.args)


def _daemon(tmp_path: Path) -> tuple[LoggerDaemon, FakeGPIO, FakeClock]:
    """実GPIOを使わずに、終了経路のwriterとdaemonを開始する。"""
    path = tmp_path / "logger.db"
    initialize_database(path)
    with closing(connect_database(path)) as connection:
        SettingsRepository(connection).set_json(
            key="setup_completed", value=True, updated_at_ms=0
        )
    clock = FakeClock(1_700_000_000_000, 0)
    gpio = FakeGPIO()
    daemon = LoggerDaemon(
        database_path=path,
        gpio=gpio,
        clock=clock,
        socket_path=Path("/tmp") / f"el-shutdown-{uuid.uuid4().hex}.sock",
    )
    daemon.start()
    return daemon, gpio, clock


@pytest.mark.parametrize("value", [0, 1])
def test_output_timeline_keeps_unknown_before_first_history(value: InputState) -> None:
    """期間途中からOUT履歴が始まる場合、先頭の不明区間を残す。"""
    widget = Widget(
        id="timeline",
        widget_type=WidgetType.TIMELINE,
        title="OUT",
        config={"signals": ["OUT1"]},
        size=WidgetSize.LARGE,
        sort_order=0,
        enabled=True,
        updated_at_ms=0,
    )
    result = evaluate_widget(
        widget,
        events=[],
        sessions=[Session(1, "boot", 10, None, "test", True)],
        now_ms=100,
        start_ms=0,
        active_session_id=1,
        output_events=[
            OutputEvent(1, 1, 10, 10_000_000, 1, value, OutputSource.RULE, True)
        ],
    )
    assert result["channels"] == [
        {
            "signal": "OUT1",
            "channel_id": 1,
            "intervals": [
                {"start_ms": 0, "end_ms": 10, "state": "UNKNOWN", "duration_ms": 10},
                {
                    "start_ms": 10,
                    "end_ms": 100,
                    "state": "ON" if value else "OFF",
                    "duration_ms": 90,
                    "source": "rule",
                },
            ],
        }
    ]


@pytest.mark.parametrize("expired", [False, True])
def test_maintenance_shutdown_finishes_test_and_cancels_timer(
    tmp_path: Path,
    expired: bool,
) -> None:
    """maintenance中の終了もTESTのOFFを保存し、閉じたGPIOへの期限処理を止める。"""
    daemon, gpio, clock = _daemon(tmp_path)
    with patch("event_logger.loggerd.outputs.threading.Timer", ManualTimer):
        daemon._handle_control(ControlRequest(ControlCommand.STOP_LOGGING))
        daemon.output_manager.begin_output_test(1)
        timer = daemon.output_manager._output_test_timer
        assert isinstance(timer, ManualTimer)
        clock.advance(4950 if expired else 100)
        if expired:
            timer.fire()
        expected_off = clock.now_utc_ms()
        if expired:
            clock.advance(1000)
        with patch.object(timer, "cancel", wraps=timer.cancel) as cancel:
            daemon.close()
            assert cancel.called
        assert daemon.output_manager.output_test_channel is None
        assert not gpio.outputs[1].is_active
    with closing(connect_database(tmp_path / "logger.db")) as connection:
        rows = connection.execute(
            "SELECT value, ts_utc_ms FROM output_events WHERE source = 'test' "
            "ORDER BY id"
        ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        (1, expected_off - (4950 if expired else 100)),
        (0, expected_off),
    ]


@pytest.mark.parametrize("failure", ["off", "input_close", "output_close"])
def test_hat_close_releases_all_devices_after_one_failure(failure: str) -> None:
    """一つのGPIO例外で、残りOUTのOFFや全deviceの解放を中断しない。"""
    inputs = {index: Mock() for index in range(1, 7)}
    outputs = {index: Mock() for index in range(1, 7)}
    trace = Mock()
    failed = (
        outputs[1].off
        if failure == "off"
        else inputs[1].close
        if failure == "input_close"
        else outputs[1].close
    )
    failed.side_effect = OSError("injected device failure")
    hat = GpioZeroHat(inputs, outputs, trace)
    with pytest.raises(OSError, match="injected"):
        hat.close()
    assert all(output.off.called for output in outputs.values())
    assert all(device.close.called for device in (*inputs.values(), *outputs.values()))
    trace.close.assert_called_once()


@pytest.mark.parametrize("location", ["header", "form"])
def test_non_ascii_csrf_is_rejected_without_server_error(location: str) -> None:
    """文字列比較が受け付けない文字をCSRFへ渡されても400で拒否する。"""
    app = create_app({"TESTING": True})
    try:
        client = app.test_client()
        assert client.get("/api/auth/csrf").status_code == 200
        response = (
            client.post("/api/logging/stop", headers={"X-CSRF-Token": "不正"})
            if location == "header"
            else client.post("/api/logging/stop", data={"csrf_token": "不正"})
        )
        assert response.status_code == 400
        assert response.get_json()["error"]["code"] == "CSRF_FAILED"
    finally:
        app.extensions["event_logger.web_context"].connection.close()


@pytest.mark.parametrize("completed", [True, False])
def test_body_is_received_before_configuration_lock(completed: bool) -> None:
    """本文が揃う前に設定lockを占有せず、不足本文では変更を実行しない。"""
    application = Mock(return_value=[b"ok"])
    middleware = _ConfigurationRequestMiddleware(application)

    class Input(io.BytesIO):
        """受信中に設定lockを別の要求が使えるか確認する。"""

        def read(self, size: int | None = -1) -> bytes:
            acquired: list[bool] = []

            def inspect_lock() -> None:
                locked = middleware._lock.acquire(blocking=False)
                acquired.append(locked)
                if locked:
                    middleware._lock.release()

            from threading import Thread

            thread = Thread(target=inspect_lock)
            thread.start()
            thread.join(timeout=1)
            assert acquired == [True]
            return super().read(size)

    environ = {
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": "2",
        "gunicorn.socket": Mock(),
        "wsgi.input": Input(b"{}" if completed else b"{"),
    }
    start_response = Mock()
    with patch("event_logger.web.request_context.threading.Timer") as timer:
        response = middleware(environ, start_response)
        timer.return_value.cancel.assert_called_once()
    if completed:
        assert environ["wsgi.input"].read() == b"{}"  # type: ignore[attr-defined]
        assert list(response) == [b"ok"]
        application.assert_called_once()
    else:
        application.assert_not_called()
        assert start_response.call_args.args[0] == "400 Bad Request"


def test_body_deadline_cannot_close_a_reused_socket() -> None:
    """起床済み旧timerも完了後にはsocketへ作用せず、期限超過時だけ拒否する。"""
    application = Mock(return_value=[b"ok"])
    middleware = _ConfigurationRequestMiddleware(application)
    transport = Mock()
    environ = {
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": "2",
        "gunicorn.socket": transport,
        "wsgi.input": io.BytesIO(b"{}"),
    }
    with patch("event_logger.web.request_context.threading.Timer") as timer:
        assert list(middleware(environ, Mock())) == [b"ok"]
        callback = timer.call_args.args[1]
        callback()
        transport.shutdown.assert_not_called()

    application.reset_mock()
    environ["wsgi.input"] = io.BytesIO(b"{}")
    start_response = Mock()
    with patch("event_logger.web.request_context.threading.Timer") as timer:
        timer.return_value.start.side_effect = lambda: timer.call_args.args[1]()
        middleware(environ, start_response)
    assert start_response.call_args.args[0] == "408 Request Timeout"
    application.assert_not_called()
    assert transport.shutdown.called
