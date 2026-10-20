"""認証・入口画面・loggerd操作のHTTPルート。

依存する共通処理はルート登録時に取得し、app factoryとの循環importを防ぐ。
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from flask import Flask, Response, redirect, render_template, request, session
from flask.typing import ResponseReturnValue

from event_logger.loggerd.ipc import (
    ControlCommand,
    ControlRequest,
    IpcProtocolError,
    IpcRemoteError,
    UnixControlClient,
)
from event_logger.web.auth import SharedPasswordAuth
from event_logger.web.config_payloads import _json_int
from event_logger.web.control_service import (
    _clear_configuration_out_of_sync,
    _configuration_fingerprint,
    _mark_configuration_out_of_sync,
    _stop_for_configuration_recovery,
)
from event_logger.web.http_responses import _error, _ok

if TYPE_CHECKING:
    from event_logger.web.request_context import WebContext



def _ensure_csrf_token() -> str:
    """sessionにCSRF tokenを一度だけ発行する。

        Args:
            なし。

        Returns:
            sessionに保存済み、または新規生成したURL-safe CSRF token。
                session lifetime中は同じ値を返す。
    """
    token = session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token

def _register_page_routes(app: Flask, context: WebContext) -> None:
    """製品画面とCSRF tokenのrouteを登録する。

        Args:
            app: Flask application。
            context: requestごとのRepository、Clock、設定値を保持するWebContext。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

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



def _control_endpoint(
    context: WebContext,
    control_request: ControlRequest,
) -> Response:
    """制御要求をIPCへ渡し、UI向けの安全なHTTP envelopeへ変換する。

        Args:
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            control_request: loggerdへ送る固定スキーマの制御要求。

        Returns:
            成功時はcontrol response dataを含む200 envelope。
                loggerd拒否はcommand error/409、
                接続・protocol障害はLOGGERD_UNAVAILABLE/503。
    """
    if context.control_client is None:
        return _error(
            "LOGGERD_UNAVAILABLE",
            "loggerd control socket is not configured",
            503,
        )
    sync_state = context.settings.get_json("daemon_config_sync", {})
    if (
        control_request.command
        in {
            ControlCommand.START_LOGGING,
            ControlCommand.TEST_OUTPUT,
        }
        and isinstance(sync_state, dict)
        and sync_state.get("out_of_sync") is True
    ):
        return _error(
            "CONFIG_OUT_OF_SYNC",
            "現在の設定がloggerdへ反映済みと確認できるまで再開・出力テストできません",
            409,
        )
    if (
        control_request.command is ControlCommand.RELOAD_CONFIG
        and isinstance(sync_state, dict)
        and sync_state.get("out_of_sync") is True
    ):
        stopped = _stop_for_configuration_recovery(context)
        if not stopped:
            return _error(
                "LOGGERD_UNAVAILABLE",
                "loggerdの停止と出力OFFを確認できません。実機のOUT状態を確認してください",
                503,
            )
        _mark_configuration_out_of_sync(context, stopped=True)
    # reload前に期待fingerprintを作り、ACK後のdigestと照合する。IPC成功だけでは
    # 設定適用を証明しないため、違えばstart/testを許可できる同期状態へ戻さない。
    expected_digest = (
        _configuration_fingerprint(context)
        if control_request.command is ControlCommand.RELOAD_CONFIG
        else None
    )
    try:
        data = context.control_client.request(control_request)
    except IpcRemoteError as error:
        if control_request.command is ControlCommand.RELOAD_CONFIG:
            stopped = _stop_for_configuration_recovery(context)
            _mark_configuration_out_of_sync(context, stopped=stopped)
        return _error(error.code, error.message, 409)
    except TimeoutError as error:
        # timeout後もloggerdが要求を実行済みの場合がある。状態を再照会し、
        # 確認できた操作だけ成功として返す。再送するとsessionやOUTを重複操作する。
        # 再送ではなくSTATUSを照会して副作用の実施有無を確かめる。
        # 不明な結果は成功扱いにせず、危険な二重操作を避けるため503へ返す。
        try:
            status_request = ControlRequest(ControlCommand.STATUS)
            if isinstance(context.control_client, UnixControlClient):
                # 元の要求を処理中のdaemonでも、処理完了後のSTATUSを待てる。
                state = context.control_client.request(status_request, timeout=6.0)
            else:
                state = context.control_client.request(status_request)
        except (OSError, IpcProtocolError, IpcRemoteError):
            if control_request.command is ControlCommand.RELOAD_CONFIG:
                stopped = _stop_for_configuration_recovery(context)
                _mark_configuration_out_of_sync(context, stopped=stopped)
            return _error("LOGGERD_UNAVAILABLE", str(error), 503)
        command = control_request.command
        confirmed = (
            (
                command is ControlCommand.STOP_LOGGING
                and state.get("logger_status") == "maintenance"
            )
            or (
                command is ControlCommand.START_LOGGING
                and state.get("logger_status") == "logging"
            )
            or (
                command is ControlCommand.TEST_OUTPUT
                and state.get("output_test_channel") == control_request.channel
            )
            or (
                command is ControlCommand.END_OUTPUT_TEST
                and state.get("output_test_channel") is None
            )
            or (
                command is ControlCommand.RELOAD_CONFIG
                and state.get("configuration_digest") == expected_digest
            )
        )
        if confirmed:
            if command is ControlCommand.RELOAD_CONFIG:
                _clear_configuration_out_of_sync(context)
            return _ok(state)
        if command is ControlCommand.RELOAD_CONFIG:
            stopped = _stop_for_configuration_recovery(context)
            _mark_configuration_out_of_sync(context, stopped=stopped)
        return _error("LOGGERD_UNAVAILABLE", str(error), 503)
    except (OSError, IpcProtocolError) as error:
        if control_request.command is ControlCommand.RELOAD_CONFIG:
            stopped = _stop_for_configuration_recovery(context)
            _mark_configuration_out_of_sync(context, stopped=stopped)
        return _error("LOGGERD_UNAVAILABLE", str(error), 503)
    if (
        control_request.command is ControlCommand.RELOAD_CONFIG
        and data.get("configuration_digest") != expected_digest
    ):
        stopped = _stop_for_configuration_recovery(context)
        _mark_configuration_out_of_sync(context, stopped=stopped)
        return _error(
            "CONFIG_NOT_APPLIED",
            "loggerdが現在の設定を適用したことを確認できません",
            503,
        )
    if control_request.command is ControlCommand.RELOAD_CONFIG:
        _clear_configuration_out_of_sync(context)
    return _ok(data)

def _register_control_routes(app: Flask, context: WebContext) -> None:
    """loggerd制御系のHTTP routeをappへ登録する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

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


