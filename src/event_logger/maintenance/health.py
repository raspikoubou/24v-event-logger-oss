"""インストール・更新後のSQLite、IPC、HTTP状態を検査するCLI。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from event_logger.database.integrity import check_database, format_integrity_error
from event_logger.loggerd.ipc import (
    ControlCommand,
    ControlRequest,
    UnixControlClient,
)


class HealthCheckError(RuntimeError):
    """サービスが利用可能な状態でないことを表す。"""


# DB接続とスキーマの状態を更新後の健全性確認に使う。
def check_database_ready(database_path: str | Path) -> None:
    """SQLite schemaとintegrityが製品の利用条件を満たすか検査する。"""
    path = Path(database_path)
    if not path.is_file():
        raise HealthCheckError(f"database is missing: {path}")
    try:
        report = check_database(path)
    except (OSError, sqlite3.Error, RuntimeError) as error:
        raise HealthCheckError(f"database check failed: {error}") from error
    if not report.ok:
        raise HealthCheckError(
            f"database integrity check failed: {format_integrity_error(report)}"
        )


# loggerdのUnixソケットへ通信できるか独立して検査する。
def check_control_ready(socket_path: str | Path) -> None:
    """loggerdのstatus応答と監視状態を検査する。"""
    try:
        data = UnixControlClient(socket_path, timeout=2.0).request(
            ControlRequest(ControlCommand.STATUS)
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise HealthCheckError(f"loggerd IPC check failed: {error}") from error
    logger_status = data.get("logger_status")
    if logger_status not in {"logging", "maintenance"}:
        raise HealthCheckError(f"loggerd is not ready: status={logger_status!r}")


# Webが実際にHTTP要求へ応答することを確認する。
def check_http_ready(url: str) -> None:
    """認証前でも到達できるWeb endpointのHTTP応答を検査する。"""
    request = Request(url, headers={"User-Agent": "event-logger-health"})
    try:
        with urlopen(request, timeout=2.0) as response:
            status = response.status
    except HTTPError as error:
        # 認証有効時の401/403は、Web serviceが応答している正常な状態。
        status = error.code
    except (URLError, TimeoutError, OSError) as error:
        raise HealthCheckError(f"Web health check failed: {error}") from error
    # 404は別のWeb serverや誤ったbind先を隠すため、到達できただけでは
    # readiness成功にしない。認証以外の4xxもendpoint不整合として扱う。
    if not (200 <= status < 400 or status in {401, 403}):
        raise HealthCheckError(f"Web health check returned HTTP {status}")


# DB・daemon・Webそれぞれをチェックして部分的な起動を見分ける。
def run_health_check(
    *,
    database_path: str | Path,
    socket_path: str | Path,
    url: str,
    attempts: int = 1,
    interval_seconds: float = 1.0,
) -> None:
    """全health checkを実行し、起動直後の一時的な未準備を再試行する。"""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    if interval_seconds < 0:
        raise ValueError("interval_seconds must not be negative")
    last_error: HealthCheckError | None = None
    for attempt in range(attempts):
        try:
            check_database_ready(database_path)
            check_control_ready(socket_path)
            check_http_ready(url)
            return
        except HealthCheckError as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(interval_seconds)
    if last_error is not None:
        raise last_error
    raise HealthCheckError("health check did not run")


def main() -> None:
    """環境変数・CLI引数からhealth checkを実行する。"""
    parser = argparse.ArgumentParser(description="event logger health check")
    parser.add_argument(
        "--database",
        default="/var/lib/event-logger/logger.db",
    )
    parser.add_argument(
        "--socket",
        default="/run/event-logger/control.sock",
    )
    parser.add_argument("--url", default="http://127.0.0.1/")
    parser.add_argument("--attempts", type=int, default=15)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    try:
        run_health_check(
            database_path=args.database,
            socket_path=args.socket,
            url=args.url,
            attempts=args.attempts,
            interval_seconds=args.interval,
        )
    except (HealthCheckError, ValueError) as error:
        print(f"event-logger health: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print("event-logger health: ok")


if __name__ == "__main__":
    main()


__all__ = [
    "HealthCheckError",
    "check_control_ready",
    "check_database_ready",
    "check_http_ready",
    "run_health_check",
]
