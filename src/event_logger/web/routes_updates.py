"""GitHub Release確認と更新実行のHTTPルート。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from flask import Flask, request

from event_logger.maintenance.update import (
    GitHubReleaseClient,
    ReleaseInfo,
    UpdateError,
    UpdateOutcomeUnknown,
    discard_update_download,
    download_and_verify,
    is_newer_version,
    launch_updater,
    request_reboot,
)


def _release_token(release: ReleaseInfo) -> str:
    """確認時の版とasset情報を、適用時の再取得結果へ対応付ける。

        Args:
            release: 確認済みのReleaseInfo。

        Returns:
            SHA-256 hex token。Release tag/versionとarchive/checksum assetのname、URL、
                size、
                API URLをJSON化してhashする。
    """
    identity = [
        release.tag,
        release.version,
        release.archive.name,
        release.archive.url,
        release.archive.size,
        release.archive.api_url,
        release.checksums.name,
        release.checksums.url,
        release.checksums.size,
        release.checksums.api_url,
    ]
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


def _register_update_routes(app: Flask, demo: bool) -> None:
    """GitHub Release確認・更新依頼のrouteをappへ登録する。

        Args:
            app: 対象のFlask application。設定値、extension、route登録先として使う。
            demo: GPIO等を使わない固定fixture modeかを表す真偽値。

        Returns:
            None。関数の概要に記したroute登録・状態更新を行う。
    """

    from event_logger.web.app import (
        Response,
        _error,
        _ok,
        _read_update_status,
    )

    @app.post("/api/system/reboot")
    def reboot_system() -> Response:
        """確認後の本体再起動を、root権限のないWebから管理agentへ依頼する。

            Args:
                なし。

            Returns:
                bodyが空ならdemo時は{started:false,demo:true}を200で返し、実機では
                agent受付後に{started:true,demo:false}を202で返す。任意bodyは400、
                agent不通は503。
        """
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
        """ローカルの更新結果だけを返す。定期確認ではGitHubへ通信しない。

            Args:
                なし。

            Returns:
                current_versionとlocal statusを含むJSON 200を返し、
                    Cache-Controlをno-storeにする。
        """
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
        """GitHub Releasesの安定版情報を確認する。

            Args:
                なし。

            Returns:
                未構成またはdemo時はconfigured:falseのJSON 200を返す。
                    Release確認成功時は版、更新可否、notes、
                    確認token、local statusを返し、
                        外部確認失敗はUPDATE_CHECK_FAILED/502。
        """
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
                "release_token": _release_token(release),
                "status": _read_update_status(app.config.get("UPDATE_STATUS_FILE")),
            }
        )

    @app.post("/api/update")
    def apply_update() -> Response:
        """ユーザー操作で検証済みReleaseを固定updaterへ渡す。

            Args:
                なし。

            Returns:
                updater起動を受理した結果をJSON 202で返す。未構成は503、
                    確認情報不足・対象変更は409、
                    downloadや検証失敗は該当UPDATE errorで返し、部分downloadを削除する。
        """
        repository = str(app.config.get("UPDATE_REPOSITORY", ""))
        if demo or not repository:
            return _error(
                "UPDATE_UNAVAILABLE",
                "release updates are not configured",
                503,
            )
        current = str(app.config["APP_VERSION"])
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("expected_version"), str)
            or not isinstance(payload.get("release_token"), str)
        ):
            return _error(
                "UPDATE_CONFIRMATION_REQUIRED",
                "更新内容を確認し直してから適用してください。",
                409,
            )
        archive_path: Path | None = None
        checksum_path: Path | None = None
        try:
            release = GitHubReleaseClient(repository).latest()
            if (
                release.version != payload["expected_version"]
                or _release_token(release) != payload["release_token"]
            ):
                return _error(
                    "UPDATE_TARGET_CHANGED",
                    "配布内容が変更されました。更新内容を確認し直してください。",
                    409,
                )
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
        except UpdateOutcomeUnknown:
            # helperの起動と応答の配送は別。応答消失を確定した失敗と返さず、
            # 依頼直前の状態を除外しながら今回の結果を監視させる。
            response = _ok(
                {
                    "started": False,
                    "outcome_unknown": True,
                    "current_version": current,
                    "target_version": release.version,
                    "previous_status": previous_status,
                    "attempt_id": None,
                }
            )
            response.status_code = 202
            return response
        except (UpdateError, ValueError, OSError) as error:
            if archive_path is not None and checksum_path is not None:
                discard_update_download(archive_path, checksum_path)
            return _error("UPDATE_NOT_STARTED", str(error), 502)
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
