"""ダッシュボードの表示期間をUTCミリ秒の範囲に解決する。

Flask requestやSQLiteへ依存せず、Clockと設定済みWidgetだけで計算する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from event_logger.adapters.clock import Clock
from event_logger.domain.widgets import WIDGET_PERIOD_DURATIONS_MS, Widget


class DashboardSettings(Protocol):
    """保存済み期間を読むための最小設定port。"""

    def get_json(self, key: str, default: object = ...) -> object:
        """JSON設定値を返す。"""


def _stored_dashboard_period(settings: DashboardSettings) -> str:
    """保存済み共通期間を検証して返す。

    Args:
        context: dashboard期間設定を読むためのrequest context。

    Returns:
        許可されたperiod key。欠落・不正値は既定値の`8h`。
    """
    value = settings.get_json("dashboard_time_range", "8h")
    supported_periods = {
        *WIDGET_PERIOD_DURATIONS_MS,
        "today",
        "yesterday",
        "this_week",
        "custom",
    }
    if isinstance(value, str) and value in supported_periods:
        return value
    return "8h"


def _stored_dashboard_custom_range(
    settings: DashboardSettings,
) -> dict[str, int] | None:
    """保存済み任意期間を検証して返す。

    Args:
        context: dashboard期間設定を読むためのrequest context。

    Returns:
        `from_ms`と`to_ms`が有効な範囲。保存値が不正なら`None`。
    """
    value = settings.get_json("dashboard_custom_range")
    if not isinstance(value, Mapping):
        return None
    start_ms = value.get("from_ms")
    end_ms = value.get("to_ms")
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or start_ms < 0
        or start_ms >= end_ms
    ):
        return None
    return {"from_ms": start_ms, "to_ms": end_ms}


def _dashboard_range_ms(
    clock: Clock,
    *,
    timezone_name: str,
    period: str,
    now_ms: int | None = None,
) -> tuple[int, int]:
    """Dashboardの固定・暦期間をUTC millisecond境界へ変換する。

        Args:
            clock: 期間境界とする現在時刻を得るClock。
            timezone_name: ローカル日時解釈・表示に使うIANA timezone名。
            period: 1h/8h/24h/7d/30dまたはtoday/yesterday/this_weekのperiod key。
            now_ms: 期間境界を共有するUTC epoch millisecond。省略時はClockから取得。

        Returns:
            期間の半開境界(start_ms, end_ms)。固定期間は現在時刻基準、
                暦期間はtimezoneの現地日付境界からUTC millisecondへ変換する。
                    不明period/timezoneはValueError。
    """
    if now_ms is None:
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


def _fixed_widget_calendar_ranges(
    widgets: Sequence[Widget],
    clock: Clock,
    *,
    timezone_name: str,
    now_ms: int | None = None,
) -> dict[str, tuple[int, int]]:
    """固定期間widgetの暦境界をtimezoneを持つWeb層で解決する。

        DomainはFlaskやtimezone設定を参照しないため、暦期間だけはWeb adapterで
            UTC境界へ変換してから評価関数へ渡す。

        Args:
            widgets: Dashboard計算対象となるwidget設定の順序付き集合。
            clock: 現在時刻と単調時刻を供給するClock。期間境界の基準に使う。
            timezone_name: 日時表示・暦境界計算に使うIANA timezone名。
            now_ms: Dashboard期間と共有するUTC epoch millisecond。

        Returns:
            暦期間widget IDからUTC半開範囲(start_ms, end_ms)へのmapping。
                固定暦期間以外は含めない。
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
                now_ms=now_ms,
            )
    return ranges


