"""Webとloggerdの間で使う最小のUnix domain socket IPC。"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from event_logger.domain.widgets import (
    Widget,
    WidgetSize,
    WidgetType,
    normalize_widget_config,
)

MAX_MESSAGE_BYTES = 64 * 1024


class ControlCommand(StrEnum):
    """Webがloggerdへ依頼できる操作のallowlist。"""

    STATUS = "status"
    START_LOGGING = "start_logging"
    STOP_LOGGING = "stop_logging"
    TEST_OUTPUT = "test_output"
    END_OUTPUT_TEST = "end_output_test"
    RELOAD_CONFIG = "reload_config"
    RESET_COUNTER = "reset_counter"


class IpcProtocolError(ValueError):
    """IPCメッセージの形式が仕様外であることを表す。"""


class IpcRemoteError(RuntimeError):
    """loggerdが要求を拒否したことを表す。"""

    def __init__(self, code: str, message: str) -> None:
        """エラーコードと利用者向けメッセージを保持する。"""
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
# Webからの制御命令はホワイトリスト化し、型と許容フィールドを通信境界で確認する。
class ControlRequest:
    """検証済みのloggerd制御要求。"""

    command: ControlCommand
    channel: int | None = None
    duration_ms: int | None = None
    request_id: str | None = None
    widget_id: str | None = None
    widget: Widget | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ControlRequest:
        """JSON objectを検証してControlRequestへ変換する。"""
        allowed = {
            "command",
            "channel",
            "duration_ms",
            "request_id",
            "widget_id",
            "widget",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise IpcProtocolError(f"unknown request fields: {sorted(unknown)}")
        raw_command = payload.get("command")
        if not isinstance(raw_command, str):
            raise IpcProtocolError("command must be a string")
        try:
            command = ControlCommand(raw_command)
        except ValueError as error:
            raise IpcProtocolError(f"unsupported command: {raw_command}") from error
        channel = _optional_int(payload.get("channel"), "channel")
        if channel is not None and not 1 <= channel <= 6:
            raise IpcProtocolError("channel must be between 1 and 6")
        duration_ms = _optional_int(payload.get("duration_ms"), "duration_ms")
        if duration_ms is not None and not 1 <= duration_ms <= 5_000:
            raise IpcProtocolError("duration_ms must be between 1 and 5000")
        request_id = payload.get("request_id")
        if request_id is not None and (
            not isinstance(request_id, str) or not 0 < len(request_id) <= 64
        ):
            raise IpcProtocolError("request_id must be a non-empty string <= 64 chars")
        if command is ControlCommand.TEST_OUTPUT:
            if channel is None:
                raise IpcProtocolError("test_output requires channel")
        elif channel is not None or duration_ms is not None:
            raise IpcProtocolError(
                "channel and duration_ms are only valid for test_output"
            )
        widget_id = payload.get("widget_id")
        widget = None
        if command is ControlCommand.RESET_COUNTER:
            if (
                not isinstance(widget_id, str)
                or not 0 < len(widget_id) <= 100
                or request_id is None
            ):
                raise IpcProtocolError(
                    "reset_counter requires widget_id and request_id"
                )
            if payload.get("widget") is not None:
                widget = _counter_widget(payload["widget"])
                if widget.id != widget_id:
                    raise IpcProtocolError("widget id does not match reset target")
        elif widget_id is not None or payload.get("widget") is not None:
            raise IpcProtocolError("widget fields are only valid for reset_counter")
        return cls(
            command=command,
            channel=channel,
            duration_ms=duration_ms,
            request_id=request_id,
            widget_id=widget_id if isinstance(widget_id, str) else None,
            widget=widget,
        )

    def to_mapping(self) -> dict[str, object]:
        """JSON送信用のmappingへ変換する。"""
        payload: dict[str, object] = {"command": self.command.value}
        if self.channel is not None:
            payload["channel"] = self.channel
        if self.duration_ms is not None:
            payload["duration_ms"] = self.duration_ms
        if self.request_id is not None:
            payload["request_id"] = self.request_id
        if self.widget_id is not None:
            payload["widget_id"] = self.widget_id
        if self.widget is not None:
            payload["widget"] = {
                "id": self.widget.id,
                "type": self.widget.widget_type.value,
                "title": self.widget.title,
                "config": dict(self.widget.config),
                "size": self.widget.size.value,
                "sort_order": self.widget.sort_order,
                "enabled": self.widget.enabled,
                "updated_at_ms": self.widget.updated_at_ms,
            }
        return payload


ControlHandler = Callable[[ControlRequest], Mapping[str, object]]


# Webプロセスとloggerdを分離し、Unixソケット経由で限定した操作だけ受け取る。
class UnixControlServer:
    """loggerd側のnewline-delimited JSON control socket server。"""

    def __init__(
        self,
        path: str | os.PathLike[str],
        handler: ControlHandler,
        *,
        socket_timeout: float = 1.0,
    ) -> None:
        """socket pathとコマンドhandlerを注入する。"""
        self._path = Path(path)
        self._handler = handler
        self._socket_timeout = socket_timeout
        self._server: socket.socket | None = None
        self._owns_path = False

    def start(self) -> None:
        """Unix socketを作成してlistenする。"""
        if self._server is not None:
            raise RuntimeError("control server is already started")
        if self._path.exists():
            mode = self._path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                raise FileExistsError(f"control path is not a socket: {self._path}")
            self._path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self._path))
            os.chmod(self._path, 0o660)
            server.listen(4)
            server.settimeout(self._socket_timeout)
        except OSError:
            server.close()
            raise
        self._server = server
        self._owns_path = True

    def serve_once(self) -> bool:
        """接続を最大1件処理する。timeoutならFalseを返す。"""
        if self._server is None:
            raise RuntimeError("control server is not started")
        try:
            connection, _ = self._server.accept()
        except TimeoutError:
            return False
        with connection:
            connection.settimeout(self._socket_timeout)
            _serve_connection(connection, self._handler)
        return True

    def close(self) -> None:
        """listen socketと自分が作成したpathを閉じる。"""
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        if self._owns_path:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
            self._owns_path = False


class UnixControlClient:
    """Web側からloggerdへ1要求を送る同期client。"""

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = 1.0) -> None:
        """接続先Unix socket pathを受け取る。"""
        self._path = Path(path)
        self._timeout = timeout

    def request(
        self, request: ControlRequest, *, timeout: float | None = None
    ) -> Mapping[str, object]:
        """要求を送信し、成功時のdata mappingを返す。"""
        encoded = _encode_message(request.to_mapping())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self._timeout if timeout is None else timeout)
            connection.connect(str(self._path))
            connection.sendall(encoded)
            response = _read_message(connection)
        if not isinstance(response, dict):
            raise IpcProtocolError("response must be a JSON object")
        if response.get("ok") is not True:
            error = response.get("error")
            if not isinstance(error, dict):
                raise IpcProtocolError("error response has invalid shape")
            code = error.get("code")
            message = error.get("message")
            if not isinstance(code, str) or not isinstance(message, str):
                raise IpcProtocolError("error response fields are invalid")
            raise IpcRemoteError(code, message)
        data = response.get("data", {})
        if not isinstance(data, dict):
            raise IpcProtocolError("success response data must be an object")
        return data


# 1接続の要求を受信・検証し、例外は通信可能なエラー形式へ整える。
def _serve_connection(connection: socket.socket, handler: ControlHandler) -> None:
    """接続1本を読み、成功・拒否レスポンスを必ず返す。"""
    try:
        payload = _read_message(connection)
        if not isinstance(payload, dict):
            raise IpcProtocolError("request must be a JSON object")
        request = ControlRequest.from_mapping(payload)
        data = handler(request)
        response: Mapping[str, object] = {"ok": True, "data": dict(data)}
    except IpcProtocolError as error:
        response = {
            "ok": False,
            "error": {"code": "INVALID_REQUEST", "message": str(error)},
        }
    except (RuntimeError, ValueError) as error:
        response = {
            "ok": False,
            "error": {"code": "COMMAND_REJECTED", "message": str(error)},
        }
    except (OSError, sqlite3.Error) as error:
        # 設定reload中のDB/OS障害をsocket serverのプロセス終了へ波及させず、
        # Web側に明示的な失敗として返す。入力・出力の安全状態はdaemon側で維持する。
        response = {
            "ok": False,
            "error": {"code": "COMMAND_FAILED", "message": str(error)},
        }
    try:
        connection.sendall(_encode_message(response))
    except OSError:
        # Web側のtimeoutや切断はloggerdの監視処理とは無関係である。
        # 応答を書けない接続だけを破棄し、次のcontrol requestを受け付ける。
        return


# 不正に大きい入力や終端のないメッセージを無制限に読み続けない。
def _read_message(connection: socket.socket) -> Any:
    """改行終端のJSONを最大サイズまで読み取る。"""
    buffer = bytearray()
    while True:
        chunk = connection.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_MESSAGE_BYTES:
            raise IpcProtocolError("IPC message is too large")
        if b"\n" in chunk:
            break
    if not buffer:
        raise IpcProtocolError("IPC message is empty")
    line = bytes(buffer).split(b"\n", 1)[0]
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IpcProtocolError("IPC message is not valid UTF-8 JSON") from error


def _encode_message(payload: Mapping[str, object]) -> bytes:
    """JSON objectをサイズ検証付きnewline messageへ変換する。"""
    try:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as error:
        raise IpcProtocolError("IPC payload is not JSON serializable") from error
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise IpcProtocolError("IPC message is too large")
    return encoded


def _optional_int(value: object, field_name: str) -> int | None:
    """JSON値をintへ変換する。boolはintとして扱わない。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise IpcProtocolError(f"{field_name} must be an integer")
    return value


def _counter_widget(payload: object) -> Widget:
    """固定スキーマのcounter設定だけをwriterへ渡せる形へ変換する。"""
    if not isinstance(payload, dict) or set(payload) != {
        "id",
        "type",
        "title",
        "config",
        "size",
        "sort_order",
        "enabled",
        "updated_at_ms",
    }:
        raise IpcProtocolError("counter widget fields are invalid")
    if not all(
        isinstance(payload[key], str) for key in ("id", "type", "title", "size")
    ):
        raise IpcProtocolError("counter widget text fields are invalid")
    config = payload["config"]
    if not isinstance(config, dict) or not isinstance(payload["enabled"], bool):
        raise IpcProtocolError("counter widget config or enabled is invalid")
    order = _optional_int(payload["sort_order"], "sort_order")
    updated_ms = _optional_int(payload["updated_at_ms"], "updated_at_ms")
    if order is None or updated_ms is None or updated_ms < 0:
        raise IpcProtocolError("counter widget order or timestamp is invalid")
    try:
        widget = Widget(
            id=payload["id"],
            widget_type=WidgetType(payload["type"]),
            title=payload["title"],
            config=normalize_widget_config(config),
            size=WidgetSize(payload["size"]),
            sort_order=order,
            enabled=payload["enabled"],
            updated_at_ms=updated_ms,
        )
    except (TypeError, ValueError) as error:
        raise IpcProtocolError(str(error)) from error
    if (
        widget.widget_type is not WidgetType.EVENT_COUNT
        or config.get("count_mode") != "reset"
    ):
        raise IpcProtocolError("only reset-based counter settings are supported")
    return widget
