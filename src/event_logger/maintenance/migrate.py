"""更新処理から呼ぶschema migration CLI。"""

from __future__ import annotations

import argparse
import logging
import os

from event_logger.database.migrations import migrate_database


def main() -> None:
    """指定DBを明示的に最新schemaへ移行する。"""
    parser = argparse.ArgumentParser(description="event logger database migration")
    parser.add_argument(
        "--database",
        default=os.environ.get("EVENT_LOGGER_DB", "/var/lib/event-logger/logger.db"),
    )
    args = parser.parse_args()
    result = migrate_database(args.database)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info(
        "schema migration %d -> %d changed=%s",
        result.old_version,
        result.new_version,
        result.changed,
    )


if __name__ == "__main__":
    main()
