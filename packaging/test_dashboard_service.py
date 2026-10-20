"""Dashboard期間serviceの契約を検証する。"""

from __future__ import annotations

from event_logger.adapters.clock import FakeClock
from event_logger.web.dashboard_service import resolve_dashboard_window


class _Settings:
    """期間設定だけを持つservice用fake。"""

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get_json(self, key: str, default: object = ...) -> object:
        """保存値または呼出元既定値を返す。"""
        return self.values.get(key, default)


def test_saved_period_and_custom_range_resolve_consistently() -> None:
    """URL未指定時は保存済み期間を、明示custom時は保存範囲を使う。"""
    clock = FakeClock(utc_ms=1_700_000_000_000, monotonic_ns=0)
    settings = _Settings(
        {
            "dashboard_time_range": "8h",
            "dashboard_custom_range": {"from_ms": 100, "to_ms": 200},
        }
    )

    saved = resolve_dashboard_window(
        clock=clock,
        settings=settings,
        timezone_name="UTC",
        period=None,
        from_ms=None,
        to_ms=None,
    )
    assert (saved.start_ms, saved.end_ms, saved.period) == (
        1_699_971_200_000,
        1_700_000_000_000,
        "8h",
    )

    custom = resolve_dashboard_window(
        clock=clock,
        settings=settings,
        timezone_name="UTC",
        period="custom",
        from_ms=None,
        to_ms=None,
    )
    assert (custom.start_ms, custom.end_ms, custom.period) == (100, 200, "custom")


def test_explicit_custom_range_does_not_mix_saved_end() -> None:
    """URLで開始境界を指定したら保存済み終了値で補完しない。"""
    clock = FakeClock(utc_ms=1_000, monotonic_ns=0)
    settings = _Settings({"dashboard_custom_range": {"from_ms": 100, "to_ms": 200}})
    window = resolve_dashboard_window(
        clock=clock,
        settings=settings,
        timezone_name="UTC",
        period="custom",
        from_ms=300,
        to_ms=None,
    )
    assert (window.start_ms, window.end_ms) == (300, 1_000)


def test_empty_range_is_rejected_and_future_range_is_clamped() -> None:
    """空区間は拒否し、未来だけの範囲は過去の最小窓へ丸める。"""
    clock = FakeClock(utc_ms=1_000, monotonic_ns=0)
    settings = _Settings({})
    try:
        resolve_dashboard_window(
            clock=clock,
            settings=settings,
            timezone_name="UTC",
            period="custom",
            from_ms=500,
            to_ms=500,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("empty range should be rejected")
    future = resolve_dashboard_window(
        clock=clock,
        settings=settings,
        timezone_name="UTC",
        period="custom",
        from_ms=1_100,
        to_ms=1_200,
    )
    assert (future.start_ms, future.end_ms) == (999, 1_000)
