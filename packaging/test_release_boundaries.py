"""カウンタ取得量、入力境界、CSV文字列と更新対象の対応を確認する。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from urllib.request import Request

import pytest

from event_logger.adapters.clock import FakeClock
from event_logger.common.models import ChannelConfig, EventKind, LogRecord, OutputSource
from event_logger.database.repositories import EventRepository
from event_logger.maintenance.update import (
    GitHubReleaseClient,
    ReleaseAsset,
    ReleaseInfo,
    UpdateOutcomeUnknown,
    download_and_verify,
)
from event_logger.web.app import create_app
from event_logger.web.csv_export import generate_csv


def test_download_uses_the_confirmed_asset_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同名の再アップロード先へ切り替わらず、固有IDから本体とchecksumを取る。"""
    body = b"confirmed release"
    payload = {
        "tag_name": "v1.0.2",
        "html_url": "https://github.com/owner/repository/releases/tag/v1.0.2",
        "body": "",
        "assets": [
            {
                "id": asset_id,
                "name": name,
                "size": size,
                "browser_download_url": (
                    f"https://github.com/owner/repository/releases/download/v1.0.2/{name}"
                ),
            }
            for asset_id, name, size in [
                (101, "event-logger-1.0.2.tar.zst", len(body)),
                (102, "SHA256SUMS", 100),
            ]
        ],
    }
    checksum = (
        f"{hashlib.sha256(body).hexdigest()}  event-logger-1.0.2.tar.zst\n"
    ).encode()
    responses = iter([json.dumps(payload).encode(), body, checksum])
    requests: list[Request] = []

    class Response(io.BytesIO):
        """download応答と同じheaders属性を持つメモリstream。"""

        headers: dict[str, str] = {}

    def open_response(request: Request, **_kwargs: object) -> Response:
        requests.append(request)
        response = Response(next(responses))
        return response

    monkeypatch.setattr("event_logger.maintenance.update.urlopen", open_response)
    release = GitHubReleaseClient("owner/repository").latest()
    archive, sums = download_and_verify(release, work_dir=tmp_path)
    assert archive.read_bytes() == body and sums.read_bytes() == checksum
    assert [request.full_url for request in requests[1:]] == [
        "https://api.github.com/repos/owner/repository/releases/assets/101",
        "https://api.github.com/repos/owner/repository/releases/assets/102",
    ]
    assert all(
        request.get_header("Accept") == "application/octet-stream"
        for request in requests[1:]
    )


@pytest.mark.parametrize("mode", ["period", "reset", "stored"])
def test_counter_dashboard_does_not_materialize_event_rows(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """カウンタだけなら件数に関係なくCOUNTで集計し、履歴行を読まない。"""
    app = create_app(
        {
            "TESTING": True,
            "CSRF_ENABLED": False,
            "CLOCK": FakeClock(utc_ms=100_000, monotonic_ns=100_000_000_000),
        }
    )
    context = app.extensions["event_logger.web_context"]
    session = context.sessions.create(
        boot_id="counter",
        started_at_ms=0,
        app_version="test",
        clock_synced=True,
    )
    client = app.test_client()
    created = client.post(
        "/api/widgets",
        json={
            "type": "event_count",
            "title": "回数",
            "config": {
                "channels": [1],
                "count_mode": mode,
                "edge": "rising",
            },
        },
    )
    assert created.status_code == 200
    widget_id = created.get_json()["data"]["id"]
    if mode == "reset":
        assert client.post(f"/api/widgets/{widget_id}/reset").status_code == 200
    for index in range(20):
        context.events.append(
            session_id=session.id,
            ts_utc_ms=index * 1000,
            mono_ns=index * 1_000_000_000,
            channel_id=1,
            value=index % 2,
            kind=EventKind.EDGE,
            clock_synced=True,
        )

    def reject_rows(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("counter must not read event rows")

    monkeypatch.setattr(EventRepository, "list_between", reject_rows)
    response = client.get("/api/dashboard?period=8h")
    assert response.status_code == 200
    assert response.get_json()["data"]["items"][0]["value"] == 10


@pytest.mark.parametrize("path", ["/api/logs", "/api/logs.csv"])
@pytest.mark.parametrize(
    "query",
    [
        "offset=" + str(10**50),
        "from_ms=0&to_ms=" + str(10**50),
        "from_ms=0&to_ms=999999999999999999",
    ],
)
def test_log_numeric_bounds_return_400(path: str, query: str) -> None:
    """不正な巨大数はSQL bindingやCSV streamへ届く前に拒否する。"""
    app = create_app({"TESTING": True, "DEMO": True})
    assert app.test_client().get(f"{path}?{query}").status_code == 400


def test_widget_sort_order_outside_sqlite_range_returns_400() -> None:
    """JSON整数がPythonで表せても、SQLiteの範囲外なら保存しない。"""
    app = create_app({"TESTING": True, "CSRF_ENABLED": False})
    response = app.test_client().post(
        "/api/widgets",
        json={
            "type": "event_count",
            "title": "回数",
            "config": {"channels": [1]},
            "sort_order": 10**50,
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "name", ["=1+1", "+123", "-123", "@text", "  =1+1", "\ttext", "\r=1+1"]
)
def test_csv_names_are_exported_as_text(name: str) -> None:
    """入力名とルール名の両方を保護し、保存値や列構成は変えない。"""
    event = LogRecord(
        id=1,
        record_type="input",
        ts_utc_ms=1_700_000_000_000,
        mono_ns=1,
        channel_id=1,
        value=1,
        kind="edge",
        source=None,
        clock_synced=True,
    )
    channel = ChannelConfig(1, True, name, 20, "ON", "OFF", 0)
    text = "".join(
        generate_csv(
            [
                event,
                replace(
                    event, record_type="output", kind="output", source=OutputSource.RULE
                ),
            ],
            {1: channel},
            {1: name},
            "Asia/Tokyo",
        )
    )
    rows = list(csv.reader(io.StringIO(text.removeprefix("\ufeff"))))
    assert rows[1][2] == "'" + name and rows[2][2] == "'" + name
    assert all(len(row) == 7 for row in rows)
    assert channel.name == name


def _release() -> ReleaseInfo:
    """外部通信しない更新確認用Releaseを返す。"""
    return ReleaseInfo(
        "v1.0.2",
        "1.0.2",
        "https://example.invalid/release",
        "案内",
        ReleaseAsset("event-logger-1.0.2.tar.zst", "https://example.invalid/asset", 10),
        ReleaseAsset("SHA256SUMS", "https://example.invalid/sums", 80),
    )


@pytest.mark.parametrize("changed_field", ["version", "size", "asset_id"])
def test_update_rejects_a_changed_confirmation(
    changed_field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """版だけでなく同じ版のasset情報が変わっても再確認させる。"""
    release = _release()
    latest = Mock(return_value=release)
    monkeypatch.setattr(
        "event_logger.web.routes_updates.GitHubReleaseClient.latest", latest
    )
    download, launch = Mock(), Mock()
    monkeypatch.setattr("event_logger.web.routes_updates.download_and_verify", download)
    monkeypatch.setattr("event_logger.web.routes_updates.launch_updater", launch)
    app = create_app(
        {
            "TESTING": True,
            "CSRF_ENABLED": False,
            "APP_VERSION": "1.0.1",
            "UPDATE_REPOSITORY": "owner/repository",
        }
    )
    client = app.test_client()
    checked = client.get("/api/update").get_json()["data"]
    if changed_field == "version":
        latest.return_value = replace(release, tag="v1.0.3", version="1.0.3")
    elif changed_field == "size":
        latest.return_value = replace(
            release, archive=replace(release.archive, size=11)
        )
    else:
        latest.return_value = replace(
            release,
            archive=replace(
                release.archive,
                api_url="https://api.github.com/repos/owner/repository/releases/assets/2",
            ),
        )
    response = client.post(
        "/api/update",
        json={
            "expected_version": checked["latest_version"],
            "release_token": checked["release_token"],
        },
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "UPDATE_TARGET_CHANGED"
    download.assert_not_called()
    launch.assert_not_called()


def test_update_unknown_acknowledgement_keeps_monitoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """送信後の応答消失は開始失敗と断定せず、直前結果を除外して監視する。"""
    release = _release()
    monkeypatch.setattr(
        "event_logger.web.routes_updates.GitHubReleaseClient.latest", lambda _: release
    )
    monkeypatch.setattr(
        "event_logger.web.routes_updates.download_and_verify",
        Mock(return_value=(tmp_path / "asset", tmp_path / "sums")),
    )
    monkeypatch.setattr(
        "event_logger.web.routes_updates.launch_updater",
        Mock(side_effect=UpdateOutcomeUnknown("lost ack")),
    )
    status = tmp_path / "status.json"
    status.write_text('{"status":"failed","version":"1.0.2"}')
    app = create_app(
        {
            "TESTING": True,
            "CSRF_ENABLED": False,
            "APP_VERSION": "1.0.1",
            "UPDATE_REPOSITORY": "owner/repository",
            "UPDATE_STATUS_FILE": str(status),
        }
    )
    client = app.test_client()
    checked = client.get("/api/update").get_json()["data"]
    response = client.post(
        "/api/update",
        json={
            "expected_version": checked["latest_version"],
            "release_token": checked["release_token"],
        },
    )
    assert response.status_code == 202
    data = response.get_json()["data"]
    assert data["started"] is False and data["outcome_unknown"] is True
    assert data["previous_status"]["status"] == "failed"
    assert data["target_version"] == "1.0.2"
