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
    """製品画面とCSRF tokenのrouteを登録する。

        Args:
            app: Flask application。
            context: requestごとのRepository、Clock、設定値を保持するWebContext。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    from event_logger.web.app import (
        Response,
        _ensure_csrf_token,
        _ok,
    )


    @app.get("/")
    def index() -> str:
        """製品Web UIの最小入口を返す。

            Args:
                なし。

            Returns:
                HTML 200のindex画面を返す。demo flagとCSRF tokenをtemplateへ渡す。
        """
        return render_template(
            "index.html",
            demo=context.demo,
            csrf_token=_ensure_csrf_token(),
        )

    @app.get("/api/auth/csrf")
    def csrf_token() -> Response:
        """ブラウザfetch用のCSRF tokenを返す。

            Args:
                なし。

            Returns:
                JSON 200で現在session用の {token: string} を返す。
        """
        return _ok({"token": _ensure_csrf_token()})


def _register_auth_routes(app: Flask, auth: SharedPasswordAuth) -> None:
    """認証・CSRFのbefore_requestとログインrouteを登録する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            auth: 共有パスワード認証の状態を管理するオブジェクト。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

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
        """認証とPOST CSRFをroute実行前に適用する。

            Args:
                なし。

            Returns:
                認証とCSRFが通ればNoneで後続routeへ進む。未認証APIは
                AUTH_REQUIRED/401、HTML要求は/loginへredirectし、CSRF不一致は
                CSRF_FAILED/400を返す。
        """
        if (
            request.path != "/login"
            and not auth.accepts_session(session.get("auth_generation"))
        ):
            session.clear()
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
                # compare_digestのstr比較はASCIIだけを受け付ける。フォームや
                # headerの任意文字を500へ変えず、通常のCSRF拒否として扱う。
                or not expected.isascii()
                or not provided.isascii()
                or not secrets.compare_digest(expected, provided)
            ):
                return _error("CSRF_FAILED", "csrf token is invalid", 400)
        _ensure_csrf_token()
        return None

    @app.get("/login")
    def login_form() -> ResponseReturnValue:
        """共有パスワード入力画面を返す。

            Args:
                なし。

            Returns:
                認証無効時は/へredirectし、有効時はCSRF token付きlogin HTML/200を返す。
        """
        if not auth.enabled:
            return redirect("/")
        return render_template("login.html", csrf_token=_ensure_csrf_token())

    @app.post("/login")
    def login() -> ResponseReturnValue:
        """共有パスワードを検証してsessionへ保存する。

            Args:
                なし。

            Returns:
                認証成功時はsessionを新世代へ更新し/へredirectする。
                    不正passwordではlogin HTMLと誤り表示を401で返す。
        """
        if not auth.enabled:
            return redirect("/")
        password = request.form.get("password", "")
        generation = auth.authenticate(password)
        if generation is None:
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
        session["auth_generation"] = generation
        _ensure_csrf_token()
        return redirect("/")

    @app.post("/logout")
    def logout() -> ResponseReturnValue:
        """共有ログインsessionを破棄する。

            Args:
                なし。

            Returns:
                session cookie内容を破棄し、認証有効なら/login、
                    無効なら/へredirectする。
        """
        session.clear()
        return redirect("/login" if auth.enabled else "/")


def _register_control_routes(app: Flask, context: WebContext) -> None:
    """loggerd制御系のHTTP routeをappへ登録する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

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
        """loggerdへメンテナンス移行を依頼する。

            Args:
                なし。

            Returns:
                loggerdの停止ACKがあれば制御結果をJSON 200で返す。
                    daemon拒否はそのcodeと409、IPC不通は503として返す。
        """
        return _control_endpoint(context, ControlRequest(ControlCommand.STOP_LOGGING))

    @app.post("/api/logging/start")
    def start_logging() -> Response:
        """loggerdへ明示的なロギング再開を依頼する。

            Args:
                なし。

            Returns:
                明示的なロギング再開ACKの結果をJSON 200で返す。daemon拒否は409、
                    IPC不通は503として返す。
        """
        return _control_endpoint(context, ControlRequest(ControlCommand.START_LOGGING))

    @app.post("/api/outputs/test")
    def test_output() -> Response:
        """メンテナンス中だけloggerdへ最大5秒のOUTテストを依頼する。

            Args:
                なし。

            Returns:
                有効なchannel/durationならOUT test要求のACKを返す。
                    JSONや数値が不正ならINVALID_REQUEST/400、
                    mode・owner等の拒否は409、IPC不通は503。
        """
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
        """進行中のOUTテストを早期終了する。

            Args:
                なし。

            Returns:
                OUT test終了ACKをJSON 200で返す。daemon拒否は409、
                    IPC不通は503として返す。
        """
        return _control_endpoint(
            context, ControlRequest(ControlCommand.END_OUTPUT_TEST)
        )

    @app.post("/api/config/reload")
    def reload_config() -> Response:
        """設定commit後にloggerdへ再読込を依頼する。

            Args:
                なし。

            Returns:
                loggerdの設定reload ACKをJSON 200で返す。設定拒否は409、
                    IPC不通は503として返す。
        """
        return _control_endpoint(context, ControlRequest(ControlCommand.RELOAD_CONFIG))


