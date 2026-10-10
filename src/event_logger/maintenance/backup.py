"""更新前SQLite backupを扱う保守CLI。"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from event_logger.database.backup import backup_database, integrity_check


def main() -> None:
    """DBをbackupし、作成先のintegrityを確認する。"""
    parser = argparse.ArgumentParser(description="event logger database backup")
    parser.add_argument(
        "--database",
        default=os.environ.get("EVENT_LOGGER_DB", "/var/lib/event-logger/logger.db"),
    )
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    destination = Path(args.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup_database(args.database, destination)
    ok, errors = integrity_check(destination)
    if not ok:
        raise RuntimeError("backup integrity check failed: " + "; ".join(errors))
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info("database backup created at %s", destination)


if __name__ == "__main__":
    main()
