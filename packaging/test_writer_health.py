"""loggerdのDB writer異常確認を保存処理から分離した回帰テスト。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from event_logger.adapters.clock import FakeClock
from event_logger.loggerd.db_writer import EventWriterPort
from event_logger.loggerd.event_queue import EventQueue
from event_logger.loggerd.service import LoggerService, LoggerStatus


def _service(writer: EventWriterPort) -> LoggerService:
    service = LoggerService(
        clock=FakeClock(1_700_000_000_000, 0),
        session_repository=Mock(),
        event_repository=Mock(),
        event_queue=EventQueue(),
        event_writer=writer,
        app_version="test",
        boot_id="test-boot",
        enabled_channels={1: 0},
    )
    service.status = LoggerStatus.LOGGING
    return service


def test_writer_health_check_does_not_drain_or_consume_counts() -> None:
    """正常確認は書き込みや完了件数の回収を実行しない。"""
    writer = Mock(spec=EventWriterPort)
    service = _service(writer)
    service.check_writer_health()
    writer.raise_if_failed.assert_called_once_with()
    writer.drain_all.assert_not_called()
    assert service.status is LoggerStatus.LOGGING


def test_writer_health_check_enters_critical_on_failure() -> None:
    """DB writerの保存失敗を検出し、Criticalへ遷移して例外を伝える。"""
    writer = Mock(spec=EventWriterPort)
    writer.raise_if_failed.side_effect = RuntimeError("writer failed")
    service = _service(writer)
    with pytest.raises(RuntimeError, match="writer failed"):
        service.check_writer_health()
    assert service.status is LoggerStatus.CRITICAL
    writer.drain_all.assert_not_called()


def test_writer_health_check_skips_when_already_critical() -> None:
    """Critical移行後はwriterへの追加検査をしない。"""
    writer = Mock(spec=EventWriterPort)
    service = _service(writer)
    service.status = LoggerStatus.CRITICAL
    service.check_writer_health()
    writer.raise_if_failed.assert_not_called()
    writer.drain_all.assert_not_called()


def test_legacy_drain_events_still_returns_writer_count() -> None:
    """従来の同期writer向け回収APIの動作は変更しない。"""
    writer = Mock(spec=EventWriterPort)
    writer.drain_all.return_value = 7
    service = _service(writer)
    assert service.drain_events() == 7
    writer.drain_all.assert_called_once_with()
