"""共有パスワード変更と既存ログインの失効を確認する。"""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient

from event_logger.web.app import create_app
from event_logger.web.auth import hash_password


def _app() -> Flask:
    """実GPIOを使用しない共有認証付きWebを作る。"""
    password_hash = hash_password("original-password")
    app = create_app(
        {
            "TESTING": True,
            "DEMO": True,
            "AUTH_ENABLED": True,
            "PASSWORD_HASH": password_hash,
            "SECRET_KEY": "session-regression-secret",
        }
    )
    context = app.extensions["event_logger.web_context"]
    context.settings.set_many_json(
        values={"password_hash": password_hash, "password_protection": True},
        updated_at_ms=0,
    )
    return app


def _login(app: Flask, password: str = "original-password") -> FlaskClient:
    """独立したCookieを持つclientで共有認証を通す。"""
    client = app.test_client()
    assert client.post("/login", data={"password": password}).status_code == 302
    assert client.get("/api/status").status_code == 200
    return client


def _save(client: FlaskClient, **changes: object) -> int:
    """実際のCSRF tokenで本体設定を保存する。"""
    token = client.get("/api/auth/csrf").get_json()["data"]["token"]
    response = client.post(
        "/api/settings/system",
        json={
            "device_name": "設備",
            "hostname": "event-logger-test",
            "timezone": "Asia/Tokyo",
            **changes,
        },
        headers={"X-CSRF-Token": token},
    )
    return response.status_code


@pytest.mark.parametrize("path", ["/", "/api/status", "/api/logs.csv"])
def test_password_change_revokes_existing_clients(path: str) -> None:
    """変更した端末と他端末の旧Cookieをともに拒否する。"""
    app = _app()
    first, second = _login(app), _login(app)
    old_token = second.get("/api/auth/csrf").get_json()["data"]["token"]
    assert _save(first, password="replacement-password") == 200
    expected = 302 if path == "/" else 401
    assert first.get(path).status_code == expected
    assert second.get(path).status_code == expected
    assert (
        second.post(
            "/api/settings/system",
            json={"device_name": "unauthorized"},
            headers={"X-CSRF-Token": old_token},
        ).status_code
        == 401
    )
    fresh = app.test_client()
    assert (
        fresh.post("/login", data={"password": "original-password"}).status_code == 401
    )
    assert _login(app, "replacement-password").get("/api/status").status_code == 200


def test_other_settings_preserve_logins() -> None:
    """パスワードを変えない通常の設定保存では再認証を要求しない。"""
    app = _app()
    first, second = _login(app), _login(app)
    assert _save(first, device_name="新しい設備名") == 200
    assert first.get("/api/status").status_code == 200
    assert second.get("/api/status").status_code == 200


def test_disable_and_reenable_does_not_revive_old_cookie() -> None:
    """同じパスワードで保護を再有効化しても古いログインは失効する。"""
    app = _app()
    first, second = _login(app), _login(app)
    assert _save(first, password_protection=False) == 200
    assert _save(first, password_protection=True) == 200
    assert second.get("/api/status").status_code == 401
    assert _login(app).get("/api/status").status_code == 200


def test_web_restart_requires_reauthentication() -> None:
    """同じCookie署名鍵でもWeb再起動後の認証世代は別にする。"""
    client = _login(_app())
    cookie = client.get_cookie("session")
    assert cookie is not None
    restarted = _app().test_client()
    restarted.set_cookie("session", cookie.value)
    assert restarted.get("/api/status").status_code == 401


@pytest.mark.parametrize("generation", [None, 0, True, "別の世代", "other-generation"])
def test_legacy_or_invalid_authenticated_cookie_is_rejected(
    generation: object,
) -> None:
    """世代を持たない旧形式のログインには新たな認証を要求する。"""
    client = _app().test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        if generation is not None:
            session["auth_generation"] = generation
    assert client.get("/api/status").status_code == 401
