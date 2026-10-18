"""ダッシュボードの表示期間をUTCミリ秒の範囲に解決する。

Flask requestやSQLiteへ依存せず、Clockと設定済みWidgetだけで計算する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from event_logger.adapters.clock import Clock
from event_logger.domain.widgets import WIDGET_PERIOD_DURATIONS_MS, Widget


# カレンダー期間と直近N時間では期間境界の決め方が異なる。
def _dashboard_range_ms(
    clock: Clock,
    *,
    timezone_name: str,
    period: str,
) -> tuple[int, int]:
    """Dashboardの固定・暦期間をUTC millisecond境界へ変換する。"""
    now_ms = clock.now_utc_ms()
    if period in WIDGET_PERIOD_DURATIONS_MS:
        return now_ms - WIDGET_PERIOD_DURATIONS_MS[period], now_ms
    try:
        timezone = ZoneInfo(timezone_name)
    except (KeyError, ZoneInfoNotFoundError) as error:
        raise ValueError("timezone is invalid") from error
    local_now = datetime.fromtimestamp(now_ms / 1000, tz=timezone)
    today = local_now.date()
    today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone)
    if period == "today":
        return round(today_start.timestamp() * 1000), now_ms
    if period == "yesterday":
        yesterday = today - timedelta(days=1)
        start = datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone)
        return round(start.timestamp() * 1000), round(today_start.timestamp() * 1000)
    if period == "this_week":
        monday = today - timedelta(days=today.weekday())
        start = datetime.combine(monday, datetime.min.time(), tzinfo=timezone)
        return round(start.timestamp() * 1000), now_ms
    raise ValueError("period is not a supported dashboard period")


# 日・週などの区切りは表示用タイムゾーンの暦を基準にする。
def _fixed_widget_calendar_ranges(
    widgets: Sequence[Widget],
    clock: Clock,
    *,
    timezone_name: str,
) -> dict[str, tuple[int, int]]:
    """固定期間widgetの暦境界をtimezoneを持つWeb層で解決する。

    DomainはFlaskやtimezone設定を参照しないため、暦期間だけはWeb adapterで
    UTC境界へ変換してから評価関数へ渡す。
    """
    calendar_periods = {"today", "yesterday", "this_week"}
    ranges: dict[str, tuple[int, int]] = {}
    for widget in widgets:
        raw_time_range = widget.config.get("time_range", {"mode": "dashboard"})
        ui_period = widget.config.get("ui_period")
        if (
            isinstance(raw_time_range, Mapping)
            and raw_time_range.get("mode") == "fixed"
            and isinstance(ui_period, str)
            and ui_period in calendar_periods
        ):
            ranges[widget.id] = _dashboard_range_ms(
                clock,
                timezone_name=timezone_name,
                period=ui_period,
            )
    return ranges


