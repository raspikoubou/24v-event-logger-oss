"""認証・入口画面・loggerd操作のHTTPルート。

依存する共通処理はルート登録時に取得し、app factoryとの循環importを防ぐ。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask, redirect, render_template
from flask.typing import ResponseReturnValue

from event_logger.web.auth import SharedPasswordAuth

if TYPE_CHECKING:
    from event_logger.web.app import WebContext


def _register_page_routes(app: Flask, context: WebContext) -> None:
    """製品画面とCSRF tokenのrouteを登録する。"""

    from event_logger.web.app import (
        Response,
        _ensure_csrf_token,
        _ok,
    )


    @app.get("/")
    def index() -> str:
        """製品Web UIの最小入口を返す。"""
        return render_template(
            "index.html",
            demo=context.demo,
            csrf_token=_ensure_csrf_token(),
        )

    @app.get("/api/auth/csrf")
    def csrf_token() -> Response:
        """ブラウザfetch用のCSRF tokenを返す。"""
        return _ok({"token": _ensure_csrf_token()})


def _register_auth_routes(app: Flask, auth: SharedPasswordAuth) -> None:
    """認証・CSRFのbefore_requestとログインrouteを登録する。"""

    from event_logger.web.app import (
        _ensure_csrf_token,
        _error,
        render_template,
        request,
        secrets,
        session,
    )


    @app.before_request
    def protect_request() -> ResponseReturnValue | None:
        """認証とPOST CSRFをroute実行前に適用する。"""
        if (
            request.path != "/login"
            and auth.enabled
            and not session.get("authenticated", False)
        ):
            if request.path.startswith("/api/"):
                return _error("AUTH_REQUIRED", "login is required", 401)
            return redirect("/login")
        if (
            app.config["CSRF_ENABLED"]
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.path != "/login"
        ):
            expected = session.get("csrf_token")
            provided = request.headers.get("X-CSRF-Token") or request.form.get(
                "csrf_token"
            )
            if (
                not isinstance(expected, str)
                or not isinstance(provided, str)
                or not secrets.compare_digest(expected, provided)
            ):
                return _error("CSRF_FAILED", "csrf token is invalid", 400)
        _ensure_csrf_token()
        return None

    @app.get("/login")
    def login_form() -> ResponseReturnValue:
        """共有パスワード入力画面を返す。"""
        if not auth.enabled:
            return redirect("/")
        return render_template("login.html", csrf_token=_ensure_csrf_token())

    @app.post("/login")
    def login() -> ResponseReturnValue:
        """共有パスワードを検証してsessionへ保存する。"""
        if not auth.enabled:
            return redirect("/")
        password = request.form.get("password", "")
        if not auth.verify(password):
            return (
                render_template(
                    "login.html",
                    csrf_token=_ensure_csrf_token(),
                    error_message="共有パスワードが正しくありません。",
                ),
                401,
            )
        session.clear()
        session["authenticated"] = True
        _ensure_csrf_token()
        return redirect("/")

    @app.post("/logout")
    def logout() -> ResponseReturnValue:
        """共有ログインsessionを破棄する。"""
        session.clear()
        return redirect("/login" if auth.enabled else "/")


def _register_control_routes(app: Flask, context: WebContext) -> None:
    """loggerd制御系のHTTP routeをappへ登録する。"""

    from event_logger.web.app import (
        ControlCommand,
        ControlRequest,
        IpcProtocolError,
        Response,
        _control_endpoint,
        _error,
        _json_int,
        request,
    )


    @app.post("/api/logging/stop")
    def stop_logging() -> Response:
        """loggerdへメンテナンス移行を依頼する。"""
        return _control_endpoint(context, ControlRequest(ControlCommand.STOP_LOGGING))

    @app.post("/api/logging/start")
    def start_logging() -> Response:
        """loggerdへ明示的なロギング再開を依頼する。"""
        return _control_endpoint(context, ControlRequest(ControlCommand.START_LOGGING))

    @app.post("/api/outputs/test")
    def test_output() -> Response:
        """メンテナンス中だけloggerdへ最大5秒のOUTテストを依頼する。"""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("INVALID_REQUEST", "JSON object is required", 400)
        try:
            channel = _json_int(payload.get("channel"), "channel")
            duration_ms = _json_int(payload.get("duration_ms", 5_000), "duration_ms")
            control_request = ControlRequest.from_mapping(
                {
                    "command": ControlCommand.TEST_OUTPUT.value,
                    "channel": channel,
                    "duration_ms": duration_ms,
                }
            )
        except (TypeError, ValueError, IpcProtocolError) as error:
            return _error("INVALID_REQUEST", str(error), 400)
        return _control_endpoint(context, control_request)

    @app.post("/api/outputs/test/end")
    def end_output_test() -> Response:
        """進行中のOUTテストを早期終了する。"""
        return _control_endpoint(
            context, ControlRequest(ControlCommand.END_OUTPUT_TEST)
        )

    @app.post("/api/config/reload")
    def reload_config() -> Response:
        """設定commit後にloggerdへ再読込を依頼する。"""
        return _control_endpoint(context, ControlRequest(ControlCommand.RELOAD_CONFIG))


