"""再起動時にDB保存済みhostnameをOSへ反映する保守CLI。"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
from pathlib import Path

from event_logger.common.hostname import default_hostname, validate_hostname
from event_logger.database.connection import connect_database, initialize_connection
from event_logger.database.repositories import SettingsRepository

_LOGGER = logging.getLogger(__name__)


# 保存済みのhostnameをOS側へ反映し、設定値の検証を先に行う。
def apply_hostname(database_path: str | Path) -> bool:
    """DBのhostname設定をhostnamectlへ渡し、変更したか返す。"""
    connection = connect_database(database_path)
    try:
        initialize_connection(connection)
        configured = SettingsRepository(connection).get_json("hostname")
    finally:
        connection.close()
    if configured is None:
        hostname = default_hostname()
    elif not isinstance(configured, str):
        raise ValueError("stored hostname setting is invalid")
    else:
        hostname = configured.strip().lower()
    validate_hostname(hostname)
    current = socket.gethostname().split(".", 1)[0].lower()
    if current == hostname:
        return False
    # Web userへroot権限を与えず、起動時のroot oneshot serviceだけが固定引数の
    # hostnamectlを実行する。shell=Trueは使わず、設定値は検証後に渡す。
    subprocess.run(
        ["hostnamectl", "set-hostname", hostname],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return True


def main() -> None:
    """指定DBのhostnameをOSへ反映する。"""
    parser = argparse.ArgumentParser(description="apply event logger hostname")
    parser.add_argument(
        "--database",
        default=os.environ.get("EVENT_LOGGER_DB", "/var/lib/event-logger/logger.db"),
    )
    args = parser.parse_args()
    changed = apply_hostname(args.database)
    logging.basicConfig(level=logging.INFO)
    _LOGGER.info("hostname applied changed=%s", changed)


if __name__ == "__main__":
    main()
