"""更新依頼前の状態と、更新プロセスの識別を確認する。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from event_logger.maintenance.update import (
    ReleaseAsset,
    ReleaseInfo,
    UpdateError,
    UpdateOutcomeUnknown,
    launch_updater,
)
from event_logger.maintenance.update_agent import UpdateAgent
from event_logger.web.app import create_app


@pytest.mark.parametrize(
    "failure", [TimeoutError("lost ack"), UpdateError("bad reply")]
)
def test_sent_update_with_lost_reply_has_unknown_outcome(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接続後に応答が失われても、未起動として再適用させない。"""
    connection = MagicMock()
    monkeypatch.setattr("event_logger.maintenance.update.socket.socket", connection)
    monkeypatch.setattr(
        "event_logger.maintenance.update._read_agent_response",
        Mock(side_effect=failure),
    )
    with pytest.raises(UpdateOutcomeUnknown):
        launch_updater(
            helper_path="unused",
            archive_path="unused",
            checksum_path="unused",
            version="1.0.2",
            socket_path="unused",
        )
    connection.return_value.__enter__.return_value.sendall.assert_called_once()


def test_unconnected_update_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """agentへ接続できない場合は、起動結果不明と区別する。"""
    socket_factory = MagicMock()
    connection = socket_factory.return_value.__enter__.return_value
    connection.connect.side_effect = FileNotFoundError("agent unavailable")
    monkeypatch.setattr("event_logger.maintenance.update.socket.socket", socket_factory)
    with pytest.raises(UpdateError) as captured:
        launch_updater(
            helper_path="unused",
            archive_path="unused",
            checksum_path="unused",
            version="1.0.2",
            socket_path="unused",
        )
    assert not isinstance(captured.value, UpdateOutcomeUnknown)
    connection.sendall.assert_not_called()


def test_update_response_preserves_pre_launch_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動後に状態が変化しても、依頼前の記録を別に保持する。"""
    status_path = tmp_path / "update.json"
    previous = {"status": "failed", "version": "1.0.2", "attempt_id": "previous"}
    started = {"status": "started", "version": "1.0.2", "attempt_id": "current"}
    status_path.write_text(json.dumps(previous))
    release = ReleaseInfo(
        "v1.0.2",
        "1.0.2",
        "https://github.com/example/logger/releases/v1.0.2",
        "",
        ReleaseAsset("event-logger-1.0.2.tar.zst", "", 1),
        ReleaseAsset("SHA256SUMS", "", 1),
    )
    client_class = Mock()
    client_class.return_value.latest.return_value = release
    monkeypatch.setattr(
        "event_logger.web.routes_updates.GitHubReleaseClient", client_class
    )
    monkeypatch.setattr(
        "event_logger.web.routes_updates.download_and_verify",
        Mock(return_value=(tmp_path / "archive", tmp_path / "SHA256SUMS")),
    )

    def launch(**_kwargs: object) -> str:
        """実際のupdaterを起動せず、状態の置き換えだけを再現する。"""
        status_path.write_text(json.dumps(started))
        return "current"

    monkeypatch.setattr("event_logger.web.routes_updates.launch_updater", launch)
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": tmp_path / "logger.db",
            "APP_VERSION": "1.0.1",
            "UPDATE_REPOSITORY": "example/logger",
            "UPDATE_WORK_DIR": str(tmp_path),
            "UPDATE_STATUS_FILE": str(status_path),
        }
    )
    client = app.test_client()
    token = client.get("/api/auth/csrf").get_json()["data"]["token"]
    confirmed = client.get("/api/update").get_json()["data"]
    response = client.post(
        "/api/update",
        headers={"X-CSRF-Token": token},
        json={
            "expected_version": confirmed["latest_version"],
            "release_token": confirmed["release_token"],
        },
    )
    assert response.status_code == 202
    data = response.get_json()["data"]
    assert data["previous_status"] == previous
    assert data["attempt_id"] == "current"
    assert data["status"] == started
    status = client.get("/api/update/status")
    assert status.get_json()["data"]["status"] == started
    assert status.headers["Cache-Control"] == "no-store"


def test_shell_status_keeps_one_id_per_process(tmp_path: Path) -> None:
    """1回の処理のIDは共通で、同じ版への次の処理では変わる。"""
    root = Path(__file__).resolve().parents[1]
    source = (root / "packaging/update.sh").read_text()
    assignment = next(
        line for line in source.splitlines() if line.startswith("UPDATE_ATTEMPT_ID=")
    )
    function = source[source.index("write_status() {") : source.index("\ndie() {")]
    # 状態writerだけを取り出し、root権限やservice操作に到達させない。
    script = "\n".join(
        [
            "set -e",
            assignment,
            function,
            'UPDATE_STATUS_FILE="$1"',
            'VERSION="1.0.2"',
            'SERVICE_USER="unused"',
            "chown() { :; }",
            'write_status started "starting"',
            'cp "$1" "$1.started"',
            'write_status failed "failed"',
        ]
    )
    ids = []
    for index in range(2):
        path = tmp_path / f"status-{index}.json"
        subprocess.run(["bash", "-c", script, "status-writer", str(path)], check=True)
        started = json.loads(Path(f"{path}.started").read_text())
        failed = json.loads(path.read_text())
        assert started["attempt_id"] == failed["attempt_id"]
        assert failed["status"] == "failed"
        ids.append(failed["attempt_id"])
    assert ids[0] != ids[1]


def test_agent_returns_id_assigned_to_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agentの応答と、起動したhelperの環境変数は同じIDを持つ。"""
    agent = UpdateAgent(
        socket_path=tmp_path / "socket",
        helper_path=tmp_path / "helper",
        work_dir=tmp_path,
    )
    archive, sums = tmp_path / "archive", tmp_path / "sums"
    monkeypatch.setattr(agent, "_validate_request", lambda _: (archive, sums, "1.0.2"))
    monkeypatch.setattr(agent, "_validate_helper", lambda: None)
    monkeypatch.setattr(
        agent, "_stage_trusted_archive", lambda *_: (archive, sums, tmp_path)
    )
    monkeypatch.setattr(
        "event_logger.maintenance.update_agent._read_request", lambda _: {}
    )
    process = Mock()
    monkeypatch.setattr(
        "event_logger.maintenance.update_agent.subprocess.Popen", process
    )
    monkeypatch.setattr(
        "event_logger.maintenance.update_agent.threading.Thread", Mock()
    )
    response = agent._handle_connection(Mock())
    assert response["ok"] is True
    attempt_id = response["attempt_id"]
    assert isinstance(attempt_id, str) and len(attempt_id) == 32
    assert (
        process.call_args.kwargs["env"]["EVENT_LOGGER_UPDATE_ATTEMPT_ID"] == attempt_id
    )


@pytest.mark.parametrize("attempt_id", [None, "current-attempt"])
def test_agent_reply_id_reaches_web_caller(
    monkeypatch: pytest.MonkeyPatch, attempt_id: str | None
) -> None:
    """旧agentのIDなし応答と、新agentのID付き応答を扱える。"""
    monkeypatch.setattr("event_logger.maintenance.update.socket.socket", MagicMock())
    monkeypatch.setattr(
        "event_logger.maintenance.update._read_agent_response",
        lambda _: {"ok": True, "attempt_id": attempt_id},
    )
    assert (
        launch_updater(
            helper_path="unused",
            archive_path="unused",
            checksum_path="unused",
            version="1.0.2",
            socket_path="unused",
        )
        == attempt_id
    )
