"""Web要求単位のSQLite接続・Repository・WSGI応答ライフサイクル。

DB接続は要求ごとに生成し、ストリーミング応答の終了後まで保持する。
元のapp factoryを改変せず、共有状態と明示的なcloseの責務を局所化する。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from event_logger.adapters.clock import Clock
from event_logger.database.connection import connect_database
from event_logger.database.repositories import (
    ChannelRepository,
    CounterResetRepository,
    EventRepository,
    LogRepository,
    OutputEventRepository,
    OutputRuleRepository,
    SessionRepository,
    SettingsRepository,
    WidgetRepository,
)
from event_logger.maintenance.storage import StorageMonitor

if TYPE_CHECKING:
    from event_logger.web.control_service import ControlClient


@dataclass(frozen=True)
class WebContext:
    """Web routeが参照する注入済みadapterとRepository。"""

    connection: sqlite3.Connection
    database_path: str
    clock: Clock
    events: EventRepository
    logs: LogRepository
    sessions: SessionRepository
    channels: ChannelRepository
    settings: SettingsRepository
    output_events: OutputEventRepository
    output_rules: OutputRuleRepository
    widgets: WidgetRepository
    counter_resets: CounterResetRepository
    demo: bool
    status_provider: Callable[[], str]
    timezone_name: str
    control_client: ControlClient | None
    storage_monitor: StorageMonitor


_REQUEST_WEB_CONTEXT: ContextVar[WebContext | None] = ContextVar(
    "event_logger_request_web_context", default=None
)


class _RequestScopedWebContext:
    """各HTTP要求のSQLite connectionをrouteへ透過的に渡す。"""

    def __init__(self, fallback: WebContext) -> None:
        """HTTP要求外で参照する初期化済みcontextを保持する。"""
        self._fallback = fallback

    def __getattr__(self, name: str) -> Any:
        """現在のrequest context、なければ初期contextのattributeを返す。"""
        context = _REQUEST_WEB_CONTEXT.get() or self._fallback
        return getattr(context, name)


class _DatabaseScopedResponse:
    """streaming responseの完了までrequest DB接続を保持する。"""

    def __init__(
        self,
        response: Iterable[bytes],
        connection: sqlite3.Connection,
        context_token: Token[WebContext | None],
    ) -> None:
        """WSGI response iterableと対応するconnection scopeを保持する。"""
        self._response = response
        self._connection = connection
        self._context_token = context_token
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        """元responseをyieldし、例外・正常終了の両方でscopeを閉じる。"""
        try:
            yield from self._response
        finally:
            self.close()

    def close(self) -> None:
        """responseとrequest専用DB接続を一度だけ閉じる。"""
        if self._closed:
            return
        self._closed = True
        try:
            close_response = getattr(self._response, "close", None)
            if callable(close_response):
                close_response()
        finally:
            try:
                _REQUEST_WEB_CONTEXT.reset(self._context_token)
            finally:
                self._connection.close()


class _DatabaseRequestMiddleware:
    """Gunicorn threadごとにSQLite接続を分け、応答終了時に閉じる。"""

    def __init__(
        self,
        application: Callable[..., Iterable[bytes]],
        database_path: str,
        context_factory: Callable[[sqlite3.Connection], WebContext],
    ) -> None:
        """WSGI appとrequestごとのWebContext生成方法を受け取る。"""
        self._application = application
        self._database_path = database_path
        self._context_factory = context_factory

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: Callable[..., Any],
    ) -> Iterable[bytes]:
        """request専用接続を設定し、response iterableの終了まで維持する。"""
        connection = connect_database(self._database_path)
        try:
            context = self._context_factory(connection)
        except BaseException:
            connection.close()
            raise
        context_token = _REQUEST_WEB_CONTEXT.set(context)
        try:
            response = self._application(environ, start_response)
        except BaseException:
            try:
                _REQUEST_WEB_CONTEXT.reset(context_token)
            finally:
                connection.close()
            raise
        return _DatabaseScopedResponse(response, connection, context_token)


def _create_web_context(
    *,
    connection: sqlite3.Connection,
    database_path: str,
    clock: Clock,
    demo: bool,
    status_provider: Callable[[], str],
    timezone_name: str,
    control_client: ControlClient | None,
) -> WebContext:
    """一つのSQLite connectionを所有するWebContextを作成する。"""
    return WebContext(
        connection=connection,
        database_path=database_path,
        clock=clock,
        events=EventRepository(connection),
        logs=LogRepository(connection),
        sessions=SessionRepository(connection),
        channels=ChannelRepository(connection),
        settings=SettingsRepository(connection),
        output_events=OutputEventRepository(connection),
        output_rules=OutputRuleRepository(connection),
        widgets=WidgetRepository(connection),
        counter_resets=CounterResetRepository(connection),
        demo=demo,
        status_provider=status_provider,
        timezone_name=timezone_name,
        control_client=control_client,
        storage_monitor=StorageMonitor(),
    )


