"""同時要求による設定の巻戻りと共有認証の不整合を再現する。"""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread, current_thread
from typing import Any

import pytest

from event_logger.loggerd.ipc import ControlCommand, ControlRequest
from event_logger.web.app import create_app
from event_logger.web.auth import hash_password, verify_password
from event_logger.web.control_service import _configuration_fingerprint


def test_failed_reload_does_not_undo_another_successful_save(tmp_path: Path) -> None:
    """反映確認中の保存と後続の無効化を競合させ、無効化の成功を守る。"""
    reloading, release_first, second_entered = Event(), Event(), Event()

    class Control:
        """最初の要求だけ、daemonの反映応答を失う状態にする。"""

        failed = False

        def request(self, request: ControlRequest) -> dict[str, object]:
            if (
                request.command is ControlCommand.RELOAD_CONFIG
                and current_thread().name == "first-save"
                and not self.failed
            ):
                reloading.set()
                assert release_first.wait(5)
                self.failed = True
                raise TimeoutError("injected lost acknowledgement")
            return {
                "configuration_digest": _configuration_fingerprint(context),
                "logger_status": "logging",
                "outputs": {str(i): 0 for i in range(1, 7)},
            }

    app = create_app(
        {
            "TESTING": True,
            "DEMO": False,
            "CSRF_ENABLED": False,
            "DATABASE_PATH": tmp_path / "logger.sqlite",
            "THREAD_SAFE_REQUESTS": True,
            "CONTROL_CLIENT": Control(),
        }
    )
    context = app.extensions["event_logger.web_context"]
    channel = context.channels.get(1)
    context.channels.update(
        channel_id=1,
        enabled=True,
        name=channel.name,
        debounce_ms=channel.debounce_ms,
        on_label=channel.on_label,
        off_label=channel.off_label,
        updated_at_ms=0,
    )
    serialized = app.wsgi_app

    def observed(environ: dict[str, Any], start: Any) -> Any:
        if current_thread().name == "second-save":
            second_entered.set()
        return serialized(environ, start)

    app.wsgi_app = observed
    results: dict[str, int] = {}

    def save(name: str, payload: dict[str, object]) -> None:
        with app.test_client() as client:
            results[name] = client.post("/api/channels/1", json=payload).status_code

    first = Thread(target=save, args=("first", {"name": "first"}), name="first-save")
    second = Thread(
        target=save,
        args=("second", {"name": "second", "enabled": False}),
        name="second-save",
    )
    first.start()
    try:
        assert reloading.wait(5)
        # 反映確認待ちも変更要求の排他範囲に含む。読取り要求は通る。
        acquired = serialized._lock.acquire(blocking=False)  # type: ignore[attr-defined]
        if acquired:
            serialized._lock.release()  # type: ignore[attr-defined]
        assert not acquired
        assert app.test_client().get("/api/status").status_code == 200
        second.start()
        assert second_entered.wait(5)
    finally:
        release_first.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert results == {"first": 503, "second": 200}
    final = context.channels.get(1)
    assert final.enabled is False and final.name == "second"


def test_password_save_and_authentication_switch_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB保存後に認証切替を止めても、別要求は旧Cookieで保存できない。"""
    initial = hash_password("original-password")
    app = create_app(
        {
            "TESTING": True,
            "DEMO": True,
            "DATABASE_PATH": tmp_path / "logger.sqlite",
            "THREAD_SAFE_REQUESTS": True,
            "AUTH_ENABLED": True,
            "PASSWORD_HASH": initial,
        }
    )
    context = app.extensions["event_logger.web_context"]
    context.settings.set_many_json(
        values={"password_protection": True, "password_hash": initial},
        updated_at_ms=0,
    )
    auth = app.extensions["event_logger.auth"]
    committed, finish_first, second_entered = Event(), Event(), Event()
    configure = auth.configure

    def delayed_configure(*, enabled: bool, password_hash: str | None) -> None:
        if password_hash and verify_password("first-password", password_hash):
            committed.set()
            assert finish_first.wait(5)
        configure(enabled=enabled, password_hash=password_hash)

    monkeypatch.setattr(auth, "configure", delayed_configure)
    clients = [app.test_client(), app.test_client()]
    tokens = []
    for client in clients:
        assert (
            client.post("/login", data={"password": "original-password"}).status_code
            == 302
        )
        tokens.append(client.get("/api/auth/csrf").get_json()["data"]["token"])
    serialized = app.wsgi_app

    def observed(environ: dict[str, Any], start: Any) -> Any:
        if current_thread().name == "second-password":
            second_entered.set()
        return serialized(environ, start)

    app.wsgi_app = observed
    results: dict[int, int] = {}

    def save(index: int, password: str) -> None:
        results[index] = (
            clients[index]
            .post(
                "/api/settings/system",
                headers={"X-CSRF-Token": tokens[index]},
                json={
                    "device_name": "設備",
                    "hostname": "event-logger-test",
                    "password_protection": True,
                    "password": password,
                },
            )
            .status_code
        )

    first = Thread(target=save, args=(0, "first-password"))
    second = Thread(target=save, args=(1, "second-password"), name="second-password")
    first.start()
    try:
        assert committed.wait(5)
        second.start()
        assert second_entered.wait(5)
        acquired = serialized._lock.acquire(blocking=False)  # type: ignore[attr-defined]
        if acquired:
            serialized._lock.release()  # type: ignore[attr-defined]
        assert not acquired
    finally:
        finish_first.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert results == {0: 200, 1: 401}
    stored = context.settings.get_json("password_hash")
    assert verify_password("first-password", stored) and auth.verify("first-password")
    assert not verify_password("second-password", stored)
    assert not auth.verify("second-password")
    # 失効した要求は保存せず、同じ保存値で再起動しても認証結果が変わらない。
    fresh = app.test_client()
    assert fresh.post("/login", data={"password": "first-password"}).status_code == 302
