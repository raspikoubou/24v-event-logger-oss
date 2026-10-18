"""Web要求単位のSQLite接続・Repository・WSGI応答ライフサイクル。

DB接続は要求ごとに生成し、ストリーミング応答の終了後まで保持する。
元のapp factoryを改変せず、共有状態と明示的なcloseの責務を局所化する。
"""

from __future__ import annotations

import io
import socket
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock
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


class _HttpInputLimitMiddleware:
    """製品UIが送らない転送形式と過大bodyを、解析・DB接続前に拒否する。"""

    MAX_BODY_BYTES = 1024 * 1024
    BODY_TIMEOUT_SECONDS = 10.0

    def __init__(self, application: Callable[..., Iterable[bytes]]) -> None:
        """フォームとJSONの既知長bodyだけを受けるWSGI境界を作る。

Args:
    application: 次に呼び出すWSGI application。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
        self._application = application

    def __call__(
        self, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes]:
        """chunked本文を読まずに拒否し、keepaliveの残body待ちも終了する。

Args:
    environ: WSGIが渡すrequest環境mapping。本文streamやtransportを含む。
    start_response: WSGI serverが渡すresponse開始callback。

Returns:
    呼出元が反復する結果列。要素は注釈に示す型で、生成順に返す。"""
        response = self.prepare_request(environ, start_response)
        if response is not None:
            return response
        return self._application(environ, start_response)

    def prepare_request(
        self, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes] | None:
        """bodyを設定lockの外で受信し、途中停止した接続の占有時間を制限する。

Args:
    environ: WSGIが渡すrequest環境mapping。本文streamやtransportを含む。
    start_response: WSGI serverが渡すresponse開始callback。

Returns:
    呼出元が反復する結果列。要素は注釈に示す型で、生成順に返す。"""
        rejected = self.reject_request(environ, start_response)
        if rejected is not None:
            return rejected
        length = int(environ.get("CONTENT_LENGTH") or "0")
        transport = environ.get("gunicorn.socket")
        if length == 0 or transport is None:
            return None
        deadline_lock = threading.Lock()
        reading = True
        expired = False

        def expire() -> None:
            """小刻みの送信でも全体の受信期限を延長させない。

Args:
    なし。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
            nonlocal expired
            with deadline_lock:
                # cancelだけでは開始済みcallbackを止められない。body受信後に
                # keepaliveで再利用されたsocketを旧timerが閉じないよう同期する。
                if reading:
                    expired = True
                    _shutdown_request_input(transport)

        timer = threading.Timer(self.BODY_TIMEOUT_SECONDS, expire)
        timer.daemon = True
        timer.start()
        body = b""
        try:
            body = environ["wsgi.input"].read(length)
        except OSError:
            # 切断・受信停止はその要求だけを拒否し、変更処理へ到達させない。
            pass
        finally:
            with deadline_lock:
                reading = False
            timer.cancel()
        if expired or len(body) != length:
            return self._reject_response(
                environ,
                start_response,
                "408 Request Timeout" if expired else "400 Bad Request",
            )
        environ["wsgi.input"] = io.BytesIO(body)
        return None

    def reject_request(
        self, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes] | None:
        """不適切なbodyだけを拒否する。認証・変更lockより前に呼ぶ。

Args:
    environ: WSGIが渡すrequest環境mapping。本文streamやtransportを含む。
    start_response: WSGI serverが渡すresponse開始callback。

Returns:
    呼出元が反復する結果列。要素は注釈に示す型で、生成順に返す。"""
        status = None
        if environ.get("HTTP_TRANSFER_ENCODING"):
            status = "400 Bad Request"
        else:
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
            except (ValueError, TypeError):
                status = "400 Bad Request"
            else:
                if length < 0:
                    status = "400 Bad Request"
                elif length > self.MAX_BODY_BYTES:
                    status = "413 Content Too Large"
        if status is None:
            return None
        return self._reject_response(environ, start_response, status)

    @staticmethod
    def _reject_response(
        environ: dict[str, Any], start_response: Callable[..., Any], status: str
    ) -> Iterable[bytes]:
        """未読bodyの再待機を防ぎ、当該要求だけを終了する。

Args:
    environ: WSGIが渡すrequest環境mapping。本文streamやtransportを含む。
    start_response: WSGI serverが渡すresponse開始callback。
    status: HTTP status、loggerd状態、または検証対象のstatus値。

Returns:
    呼出元が反復する結果列。要素は注釈に示す型で、生成順に返す。"""
        # GunicornはWSGIのConnection response headerを無視し、keepalive前に
        # 未読bodyをdrainする。受信側だけ閉じて、未終端chunkの再解析を防ぐ。
        # 送信側は開いておき、拒否応答をブラウザーへ返す。
        transport = environ.get("gunicorn.socket")
        if transport is not None:
            _shutdown_request_input(transport)
        body = (
            b'{"ok":false,"error":{"code":"INVALID_HTTP_BODY",'
            b'"message":"Request body is unsupported or too large"}}'
        )
        start_response(
            status,
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Connection", "close"),
            ],
        )
        return [body]


def _shutdown_request_input(transport: Any) -> None:
    """拒否応答を書けるよう、socketの受信側だけを閉じる。

Args:
    transport: 現在の要求のsocket transport。要求終了時の受信側shutdownに使う。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    try:
        transport.shutdown(socket.SHUT_RD)
    except OSError:
        # 切断済みの場合も通常の応答終了処理へ進む。
        pass


class _ConfigurationRequestMiddleware:
    """設定の保存・daemon反映・復元を、認証確認前から要求単位で直列化する。"""

    def __init__(self, application: Callable[..., Iterable[bytes]]) -> None:
        """製品の単一Gunicorn worker内で、設定と共有認証の整合性を守る。

Args:
    application: 次に呼び出すWSGI application。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
        self._application = application
        self._lock = RLock()
        self._input_limits = _HttpInputLimitMiddleware(application)

    def __call__(
        self, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes]:
        """読取りは並行させ、変更要求の認証・保存・反映確認を分離しない。

Args:
    environ: WSGIが渡すrequest環境mapping。本文streamやtransportを含む。
    start_response: WSGI serverが渡すresponse開始callback。

Returns:
    呼出元が反復する結果列。要素は注釈に示す型で、生成順に返す。"""
        rejected = self._input_limits.prepare_request(environ, start_response)
        if rejected is not None:
            return rejected
        if environ.get("REQUEST_METHOD") in {"GET", "HEAD", "OPTIONS"} or (
            environ.get("PATH_INFO") in {"/api/update", "/api/system/reboot"}
        ):
            # 更新の外部downloadは長時間になり得る。管理agent側の専用lockに
            # 任せ、記録停止など通常の変更要求をdownload待ちへ巻き込まない。
            return self._application(environ, start_response)
        # Repositoryのtransactionだけでは、commit後のIPC失敗による旧値復元が
        # 別要求の成功を消す。認証もlock取得後に実行し、待機中に失効したCookie
        # で設定を上書きしない。JSON変更応答はroute内で確定し、streamはGETのみ。
        # lock順序はこのlock → request DB → SharedPasswordAuth内部lockとする。
        with self._lock:
            return self._application(environ, start_response)


class _RequestScopedWebContext:
    """各HTTP要求のSQLite connectionをrouteへ透過的に渡す。"""

    def __init__(self, fallback: WebContext) -> None:
        """HTTP要求外で参照する初期化済みcontextを保持する。

Args:
    fallback: request contextがない時に参照する初期化済みobject。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
        self._fallback = fallback

    def __getattr__(self, name: str) -> Any:
        """現在のrequest context、なければ初期contextのattributeを返す。

            Args:
                name: 項目名、選択肢名、または表示用の文字列。

            Returns:
                現在requestのcontext attribute。
                    request外なら初期contextへ委譲したattributeを返し、
                    存在しなければAttributeError。
        """
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
        """WSGI response iterableと対応するconnection scopeを保持する。

Args:
    response: WSGI response iterable。完了時にrequest connectionを閉じる対象。
    connection: この処理が利用するSQLite connection。呼出元が所有する接続を前提とする。
    context_token: ContextVarへ設定したrequest context token。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
        self._response = response
        self._connection = connection
        self._context_token = context_token
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        """元responseをyieldし、例外・正常終了の両方でscopeを閉じる。

Args:
    なし。

Yields:
    逐次生成する値。呼出元が反復すると、必要な範囲の結果を順に受け取る。"""
        try:
            yield from self._response
        finally:
            self.close()

    def close(self) -> None:
        """responseとrequest専用DB接続を一度だけ閉じる。

Args:
    なし。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
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
        """WSGI appとrequestごとのWebContext生成方法を受け取る。

Args:
    application: 次に呼び出すWSGI application。
    database_path: SQLite database fileのpath。メモリDBの場合はその指定値。
    context_factory: 新しいrequestごとにWebContextを作る関数。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
        self._application = application
        self._database_path = database_path
        self._context_factory = context_factory

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: Callable[..., Any],
    ) -> Iterable[bytes]:
        """request専用接続を設定し、response iterableの終了まで維持する。

Args:
    environ: WSGIが渡すrequest環境mapping。本文streamやtransportを含む。
    start_response: WSGI serverが渡すresponse開始callback。

Returns:
    呼出元が反復する結果列。要素は注釈に示す型で、生成順に返す。"""
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
    """一つのSQLite connectionを所有するWebContextを作成する。

        Args:
            connection: この処理が利用するSQLite connection。
                呼出元が所有する接続を前提とする。
            database_path: SQLite database fileのpath。メモリDBの場合はその指定値。
            clock: 現在時刻と単調時刻を供給するClock。期間境界の基準に使う。
            demo: GPIO等を使わない固定fixture modeかを表す真偽値。
            status_provider: loggerdの現在状態を読むprovider callable。
            timezone_name: 日時表示・暦境界計算に使うIANA timezone名。
            control_client: loggerd制御要求を送るclient。

        Returns:
            渡されたconnectionを所有するWebContext。
                connection lifecycleはrequest responseのcloseまで維持する。
    """
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
