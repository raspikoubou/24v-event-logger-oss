"""保存診断の境界・復帰とRelease本文規則の開発用回帰試験。"""

from __future__ import annotations

from typing import TypedDict

import pytest
from check_release_notes import violations

from event_logger.loggerd.db_writer import WriteLatencyMonitor
from event_logger.loggerd.event_queue import (
    EventQueue,
    EventQueueFull,
    PendingInputEvent,
)


class WarningCondition(TypedDict, total=False):
    """診断へ注入する危険状態。"""

    in_progress_ms: int
    oldest_pending_ms: int
    pending_count: int
    failed: bool


def test_normal_and_completed_delays() -> None:
    """単発・断続・連続の完了遅延を欠損警告にしない。

Args:
    なし。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    monitor = WriteLatencyMonitor()
    assert monitor.snapshot(now_ns=0).severity == "normal"
    for second in (1, 2, 3, 40, 60, 100, 150):
        monitor.record(completed_ns=second * 10**9, elapsed_ns=2_702_000_000)
        snapshot = monitor.snapshot(now_ns=second * 10**9)
        assert snapshot.severity == "attention"
        assert not snapshot.warning
    assert snapshot.delayed_count == 7
    assert snapshot.max_delay_ms == 2702
    assert monitor.snapshot(now_ns=450 * 10**9).severity == "normal"


@pytest.mark.parametrize(
    "condition",
    [
        {"in_progress_ms": 5000},
        {"oldest_pending_ms": 5000},
        {"pending_count": 8000},
        {"failed": True},
    ],
)
def test_warning_and_recovery(condition: WarningCondition) -> None:
    """停滞・滞留・容量・失敗と、回復途中の再悪化を確認する。

Args:
    condition: 検証対象の診断条件または入力値。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    monitor = WriteLatencyMonitor()
    assert monitor.snapshot(now_ns=0, **condition).warning
    assert monitor.snapshot(now_ns=10**9).warning
    assert monitor.snapshot(now_ns=10 * 10**9).warning
    assert monitor.snapshot(now_ns=11 * 10**9).severity == "normal"
    assert monitor.snapshot(now_ns=12 * 10**9, **condition).warning
    assert monitor.snapshot(now_ns=13 * 10**9).warning
    assert monitor.snapshot(now_ns=20 * 10**9, oldest_pending_ms=1000).warning
    assert monitor.snapshot(now_ns=21 * 10**9).warning
    assert monitor.snapshot(now_ns=30 * 10**9).warning
    assert not monitor.snapshot(now_ns=31 * 10**9).warning


def test_boundaries_and_latched_failure() -> None:
    """警告直前値と失敗保持では誤解除しない。

Args:
    なし。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    monitor = WriteLatencyMonitor()
    assert not monitor.snapshot(
        now_ns=0, in_progress_ms=4999, oldest_pending_ms=4999, pending_count=7999
    ).warning
    assert monitor.snapshot(now_ns=1, failed=True).warning
    assert monitor.snapshot(now_ns=100 * 10**9, failed=True).warning
    assert EventQueue(maxsize=4).capacity == 4
    assert WriteLatencyMonitor().snapshot(now_ns=0, capacity=4, pending_count=4).warning


def test_release_body_rules() -> None:
    """利用者向け本文を許可し内部情報を検出する。

Args:
    なし。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    assert not violations("一時的な保存遅延による過剰な警告表示を改善しました。")
    for body in (
        "test_logger.pyで12項目のテストを実施しました。",
        "既存テスト42件がすべて成功しました。",
        "保存処理の関数を分割しました。",
        "CI/CDの結果",
        "コミットID",
    ):
        assert violations(body)


def test_queue_overflow_keeps_accepted_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """満杯時の未受理eventと、失敗batchのFIFO保持を区別する。

Args:
    monkeypatch: pytestが提供する一時的な属性・環境差し替えfixture。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    monkeypatch.setattr("event_logger.loggerd.event_queue.monotonic_ns", lambda: 0)
    queue = EventQueue(maxsize=2)
    first = PendingInputEvent(1, 100, 0, 1, 1)
    second = PendingInputEvent(1, 200, 100, 1, 0)
    queue.put(first)
    queue.put(second)
    batch = queue.wait_get_batch(max_count=32, timeout_s=0)
    # 処理中も容量に含む。拒否されたeventは保持されず、欠損し得る。
    with pytest.raises(EventQueueFull):
        queue.put(PendingInputEvent(1, 300, 200, 1, 1))
    monkeypatch.setattr(
        "event_logger.loggerd.event_queue.monotonic_ns", lambda: 6_000_000_000
    )
    assert queue.oldest_pending_ms() == 6000
    queue.requeue_front_many(batch)
    assert queue.wait_get_batch(max_count=32, timeout_s=0) == [first, second]
    queue.task_done(2)
    assert len(queue) == 0
    assert queue.oldest_pending_ms() == 0
