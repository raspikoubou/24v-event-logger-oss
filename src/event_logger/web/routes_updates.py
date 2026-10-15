"""GitHub Release確認と更新実行のHTTPルート。"""

from __future__ import annotations

from flask import Flask

from event_logger.maintenance.update import (
    GitHubReleaseClient,
    UpdateError,
    download_and_verify,
    is_newer_version,
    launch_updater,
    request_reboot,
)


def _register_update_routes(app: Flask, demo: bool) -> None:
    """GitHub Release確認・更新依頼のrouteをappへ登録する。"""

    from event_logger.web.app import (
        Response,
        _error,
        _ok,
        _read_update_status,
    )

    @app.post("/api/system/reboot")
    def reboot_system() -> Response:
        """確認後の本体再起動を、root権限のないWebから管理agentへ依頼する。"""
        from flask import request

        # 固定操作に任意引数を付けられると誤解させない。CSRF・共有passwordは
        # 他の変更endpointと同じbefore_request境界で検証済み。
        if request.get_data():
            return _error("INVALID_REQUEST", "再起動要求に引数は指定できません。", 400)
        if demo:
            return _ok({"started": False, "demo": True})
        try:
            request_reboot(str(app.config.get("UPDATE_SOCKET_PATH", "")))
        except UpdateError as error:
            return _error("REBOOT_FAILED", str(error), 503)
        response = _ok({"started": True, "demo": False})
        response.status_code = 202
        return response

    @app.get("/api/update/status")
    def update_status() -> Response:
        """ローカルの更新結果だけを返す。定期確認ではGitHubへ通信しない。"""
        response = _ok(
            {
                "current_version": str(app.config["APP_VERSION"]),
                "status": _read_update_status(app.config.get("UPDATE_STATUS_FILE")),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/update")
    def update_info() -> Response:
        """GitHub Releasesの安定版情報を確認する。"""
        current = str(app.config["APP_VERSION"])
        repository = str(app.config.get("UPDATE_REPOSITORY", ""))
        if demo or not repository:
            return _ok(
                {
                    "configured": False,
                    "current_version": current,
                    "latest_version": None,
                    "available": False,
                    "status": _read_update_status(app.config.get("UPDATE_STATUS_FILE")),
                }
            )
        try:
            release = GitHubReleaseClient(repository).latest()
        except (UpdateError, ValueError) as error:
            return _error("UPDATE_CHECK_FAILED", str(error), 502)
        return _ok(
            {
                "configured": True,
                "current_version": current,
                "latest_version": release.version,
                "available": is_newer_version(current, release.version),
                "release_url": release.url,
                "notes": release.notes,
                "status": _read_update_status(app.config.get("UPDATE_STATUS_FILE")),
            }
        )

    @app.post("/api/update")
    def apply_update() -> Response:
        """ユーザー操作で検証済みReleaseを固定updaterへ渡す。"""
        repository = str(app.config.get("UPDATE_REPOSITORY", ""))
        if demo or not repository:
            return _error(
                "UPDATE_UNAVAILABLE",
                "release updates are not configured",
                503,
            )
        current = str(app.config["APP_VERSION"])
        try:
            release = GitHubReleaseClient(repository).latest()
            if not is_newer_version(current, release.version):
                return _error(
                    "UPDATE_NOT_AVAILABLE",
                    "no newer stable release is available",
                    409,
                )
            archive_path, checksum_path = download_and_verify(
                release,
                work_dir=str(app.config["UPDATE_WORK_DIR"]),
            )
            # agentへ依頼する直前の記録を返し、確認画面を開いた後に更新された
            # 前回結果も、同じ版への再試行の結果として誤認させない。
            previous_status = _read_update_status(app.config.get("UPDATE_STATUS_FILE"))
            attempt_id = launch_updater(
                helper_path=str(app.config["UPDATE_HELPER"]),
                archive_path=archive_path,
                checksum_path=checksum_path,
                version=release.version,
                socket_path=(
                    str(app.config["UPDATE_SOCKET_PATH"])
                    if app.config.get("UPDATE_SOCKET_PATH")
                    else None
                ),
            )
        except (UpdateError, ValueError, OSError) as error:
            return _error("UPDATE_FAILED", str(error), 502)
        response = _ok(
            {
                "started": True,
                "current_version": current,
                "target_version": release.version,
                "previous_status": previous_status,
                "attempt_id": attempt_id,
                "status": _read_update_status(app.config.get("UPDATE_STATUS_FILE")),
            }
        )
        response.status_code = 202
        return response
