"""Flask app factoryとWeb routeの組み立て。"""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, cast

from flask import Flask

from event_logger.adapters.clock import Clock, FakeClock, SystemClock
from event_logger.database.connection import connect_database, initialize_connection
from event_logger.version import VERSION
from event_logger.web.auth import SharedPasswordAuth
from event_logger.web.control_service import (
    _configuration_fingerprint,
    _control_client,
    _DemoControlClient,
)
from event_logger.web.demo_fixtures import _seed_demo_data
from event_logger.web.request_context import (
    WebContext,
    _ConfigurationRequestMiddleware,
    _create_web_context,
    _DatabaseRequestMiddleware,
    _RequestScopedWebContext,
)
from event_logger.web.routes_access import (
    _register_auth_routes,
    _register_control_routes,
    _register_page_routes,
)
from event_logger.web.routes_data import (
    _register_dashboard_routes,
    _register_log_routes,
    _register_status_routes,
)
from event_logger.web.routes_settings import (
    _register_basic_settings_routes,
    _register_channel_routes,
    _register_setup_routes,
    _register_system_settings_routes,
)
from event_logger.web.routes_updates import _register_update_routes
from event_logger.web.routes_widgets import (
    _register_output_rule_routes,
    _register_widget_routes,
)


def create_app(config: Mapping[str, Any] | None = None) -> Flask:
    """Flask appを作成する。

            `EVENT_LOGGER_DEMO=1` 相当の `DEMO=True` を渡すと、外部DBやGPIOなしで
            固定fixtureを作成する。module import時にはDB・GPIO・threadを初期化しない。


        Args:
            config: Flask factoryへ渡す設定mapping。明示値が既定値を上書きする。

        Returns:
            登録済みroute、extension、WebContextを持つFlask app。
                構築中失敗時はfactoryがbase SQLite connectionを閉じる。
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    # 先に全設定のfallbackを置き、次に呼出元値を上書きする。
    # factoryの後段が環境変数やmodule-global状態へ直接依存しない形に揃える。
    app.config.from_mapping(
        DEMO=False,
        APP_VERSION=VERSION,
        DATABASE_PATH=":memory:",
        TIMEZONE="Asia/Tokyo",
        DEVICE_NAME="",
        HOSTNAME=None,
        LOGGER_STATUS="unknown",
        LOGGER_STATUS_PROVIDER=None,
        CONTROL_SOCKET_PATH=None,
        CONTROL_CLIENT=None,
        AUTH_ENABLED=False,
        PASSWORD_HASH=None,
        SECRET_KEY=None,
        CSRF_ENABLED=True,
        THREAD_SAFE_REQUESTS=False,
        UPDATE_REPOSITORY="",
        UPDATE_HELPER="/opt/event-logger/current/packaging/update.sh",
        UPDATE_WORK_DIR="/var/lib/event-logger/updates",
        UPDATE_SOCKET_PATH="",
        UPDATE_STATUS_FILE="/var/lib/event-logger/update-status.json",
    )
    if config is not None:
        app.config.update(config)
    app.secret_key = app.config["SECRET_KEY"] or secrets.token_urlsafe(32)
    auth = SharedPasswordAuth(
        enabled=bool(app.config["AUTH_ENABLED"]),
        password_hash=app.config["PASSWORD_HASH"],
    )
    app.extensions["event_logger.auth"] = auth
    _register_auth_routes(app, auth)

    # 認証routeを登録後にclock/databaseを選ぶ。thread-safe requestsではfile DBが
    # 必須なので、接続を作成する前にmemory設定を拒否する。
    demo = bool(app.config["DEMO"])
    clock = app.config.get("CLOCK") or _default_clock(demo)
    database_path = app.config["DATABASE_PATH"]
    database_path_text = str(database_path)
    thread_safe_requests = bool(app.config["THREAD_SAFE_REQUESTS"])
    if thread_safe_requests and database_path_text == ":memory:":
        raise ValueError("thread-safe requests require a file-backed SQLite database")
    connection = connect_database(database_path_text)
    # ownershipは_finish_app_creationの成功時にWebContextへ移る。失敗時だけこの
    # factoryが閉じるため、構築例外でSQLite connectionを漏らさない。
    try:
        return _finish_app_creation(
            app,
            connection=connection,
            database_path_text=database_path_text,
            clock=clock,
            demo=demo,
            thread_safe_requests=thread_safe_requests,
            auth=auth,
        )
    except BaseException:
        # 成功時はWebContextがapp寿命の接続を所有する。構築途中で失敗した
        # 場合だけ、返却先のないbase connectionをfactoryが閉じる。
        connection.close()
        raise


def _finish_app_creation(
    app: Flask,
    *,
    connection: sqlite3.Connection,
    database_path_text: str,
    clock: Clock,
    demo: bool,
    thread_safe_requests: bool,
    auth: SharedPasswordAuth,
) -> Flask:
    """DB接続後のapp構築を完了する。失敗時の接続解放はfactoryが担う。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            connection: この処理が利用するSQLite connection。
                呼出元が所有する接続を前提とする。
            database_path_text: SQLite接続へ渡す文字列化済みdatabase path。
            clock: 現在時刻と単調時刻を供給するClock。期間境界の基準に使う。
            demo: GPIO等を使わない固定fixture modeかを表す真偽値。
            thread_safe_requests: requestごとにSQLite接続を分けるかを表す真偽値。
            auth: 共有パスワード認証の状態を管理するオブジェクト。

        Returns:
            demo fixtureやrequest context middlewareを登録したFlask app。
                受け取ったconnectionの寿命はWebContextが所有する。
    """
    initialize_connection(connection)
    # 共有connectionから依存を組み立て、設定・認証を復元してからrequest境界と
    # routeを登録する。未完成のcontextをFlaskへ公開しない。
    control_client = _control_client(app, demo)
    status_provider = _status_provider(app)
    timezone_name = str(app.config["TIMEZONE"])
    base_context = _create_web_context(
        connection=connection,
        database_path=database_path_text,
        clock=clock,
        demo=demo,
        status_provider=status_provider,
        timezone_name=timezone_name,
        control_client=control_client,
    )
    stored_protection = base_context.settings.get_json(
        "password_protection", auth.enabled
    )
    stored_hash = base_context.settings.get_json(
        "password_hash", app.config.get("PASSWORD_HASH")
    )
    if not isinstance(stored_protection, bool):
        raise ValueError("stored password_protection setting is invalid")
    if stored_hash is not None and not isinstance(stored_hash, str):
        raise ValueError("stored password hash is invalid")
    auth.configure(enabled=stored_protection, password_hash=stored_hash)
    context = base_context
    # file DBはrequestごとにconnectionを持たせる。memory demoは同じconnectionを
    # 共有しないとrequestごとに別databaseになりfixtureを参照できない。
    if thread_safe_requests:
        context = cast(WebContext, _RequestScopedWebContext(base_context))
        # Flask上のWSGI middleware設定なので、methodをinstance側でwrapする。
        app.wsgi_app = _DatabaseRequestMiddleware(  # type: ignore[method-assign]
            app.wsgi_app,
            database_path_text,
            lambda request_connection: _create_web_context(
                connection=request_connection,
                database_path=database_path_text,
                clock=clock,
                demo=demo,
                status_provider=status_provider,
                timezone_name=timezone_name,
                control_client=control_client,
            ),
        )
    app.wsgi_app = _ConfigurationRequestMiddleware(  # type: ignore[method-assign]
        app.wsgi_app
    )
    app.extensions["event_logger.web_context"] = context
    if isinstance(control_client, _DemoControlClient):
        control_client.set_configuration_digest_provider(
            lambda: _configuration_fingerprint(context)
        )
    if demo:
        _seed_demo_data(context)

    # middleware/contextを先に確定してからrouteを登録する。全routeは同じWebContext
    # 境界を通り、request終了時に接続を閉じられる。
    _register_page_routes(app, context)

    _register_status_routes(app, context, auth)

    _register_update_routes(app, demo)

    _register_dashboard_routes(app, context)

    _register_control_routes(app, context)
    _register_channel_routes(app, context)
    _register_basic_settings_routes(app, context)
    _register_system_settings_routes(app, context, auth)
    _register_setup_routes(app, context, auth)
    _register_output_rule_routes(app, context)
    _register_widget_routes(app, context)
    _register_log_routes(app, context)

    return app


def create_production_app() -> Flask:
    """Gunicorn用に環境変数から本番Web設定を読み込む。

        Args:
            なし。

        Returns:
            本番environment設定、SQLite connection、
                Unix loggerd control clientを持つFlask app。
    """
    demo = os.environ.get("EVENT_LOGGER_DEMO") == "1"
    database_path = os.environ.get(
        "EVENT_LOGGER_DB",
        ":memory:" if demo else "/var/lib/event-logger/logger.db",
    )
    return create_app(
        {
            "DEMO": demo,
            "DATABASE_PATH": database_path,
            # ファイルDBでは各gthread要求が別connectionを所有する。
            # in-memory demoは単一connectionのままにしてschemaを共有する。
            "THREAD_SAFE_REQUESTS": database_path != ":memory:",
            "CONTROL_SOCKET_PATH": (
                None
                if demo
                else os.environ.get(
                    "EVENT_LOGGER_SOCKET", "/run/event-logger/control.sock"
                )
            ),
            "TIMEZONE": os.environ.get("EVENT_LOGGER_TIMEZONE", "Asia/Tokyo"),
            "APP_VERSION": os.environ.get("EVENT_LOGGER_VERSION", VERSION),
            "AUTH_ENABLED": os.environ.get("EVENT_LOGGER_AUTH_ENABLED") == "1",
            "PASSWORD_HASH": os.environ.get("EVENT_LOGGER_PASSWORD_HASH"),
            "SECRET_KEY": os.environ.get("EVENT_LOGGER_SECRET_KEY"),
            "UPDATE_REPOSITORY": os.environ.get("EVENT_LOGGER_UPDATE_REPOSITORY", ""),
            "UPDATE_HELPER": os.environ.get(
                "EVENT_LOGGER_UPDATE_HELPER",
                "/opt/event-logger/current/packaging/update.sh",
            ),
            "UPDATE_WORK_DIR": os.environ.get(
                "EVENT_LOGGER_UPDATE_WORK_DIR",
                "/var/lib/event-logger/updates",
            ),
            "UPDATE_SOCKET_PATH": os.environ.get(
                "EVENT_LOGGER_UPDATE_SOCKET", "/run/event-logger-update/update.sock"
            ),
            "UPDATE_STATUS_FILE": os.environ.get(
                "EVENT_LOGGER_UPDATE_STATUS_FILE",
                "/var/lib/event-logger/update-status.json",
            ),
        }
    )


def _default_clock(demo: bool) -> Clock:
    """demo fixtureと本番で異なるClockを選ぶ。

        Args:
            demo: GPIO等を使わない固定fixture modeかを表す真偽値。

        Returns:
            demoなら固定時刻fixture、本番ならsystem clockを実装するClock。
    """
    if demo:
        return FakeClock(utc_ms=1_700_000_000_000, monotonic_ns=0, synchronized=True)
    return SystemClock()






def _status_provider(app: Flask) -> Callable[[], str]:
    """loggerd状態providerを設定値から構築する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。

        Returns:
            LOGGER_STATUS_PROVIDER、control client、
                または固定設定からlogger status文字列を返すcallable。
    """
    configured = app.config.get("LOGGER_STATUS_PROVIDER")
    if callable(configured):
        return lambda: str(configured())
    return lambda: str(app.config.get("LOGGER_STATUS", "unknown"))
















def main() -> None:
    """開発・demo確認用にlocalhostだけでFlask appを起動する。

Args:
    なし。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    app = create_production_app()
    app.run(
        host=os.environ.get("EVENT_LOGGER_WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("EVENT_LOGGER_WEB_PORT", "5000")),
        debug=False,
        # WebContextはapp生成時に作ったSQLite connectionをrepositoriesと共有
        # する。標準Flask serverのthreaded=Trueでは別threadから同じconnection
        # を触ってProgrammingErrorになるため、demo/開発起動は直列化する。
        threaded=False,
    )


if __name__ == "__main__":
    main()
