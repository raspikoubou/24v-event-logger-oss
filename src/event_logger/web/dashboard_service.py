"""HTTP表現を持たずDashboardの取得・live解釈・計算をまとめるapplication service。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from event_logger.adapters.clock import Clock
from event_logger.common.models import CounterReset
from event_logger.database.repositories import (
    ChannelRepository,
    CounterResetRepository,
    EventRepository,
    OutputEventRepository,
    SessionRepository,
    WidgetRepository,
)
from event_logger.domain.widget_values import evaluate_widget
from event_logger.domain.widgets import (
    widget_end_ms,
    widget_start_ms,
)
from event_logger.web.control_service import ControlClient, _read_daemon_snapshot
from event_logger.web.dashboard_periods import (
    DashboardSettings,
    _dashboard_range_ms,
    _fixed_widget_calendar_ranges,
    _stored_dashboard_custom_range,
    _stored_dashboard_period,
)
from event_logger.web.dashboard_queries import (
    _DASHBOARD_TIMELINE_MAX_INTERVALS,
    DashboardQueryPorts,
    _dashboard_events,
    _dashboard_output_events,
    _dashboard_period_counts,
    _dashboard_reset_counts,
    _dashboard_timeline_event_count_exceeds_limit,
    _dashboard_timeline_interval_count,
    _stored_widget_count,
)


@dataclass(frozen=True)
class DashboardWindow:
    """Dashboard要求で共有する半開期間と表示キー。"""

    start_ms: int
    end_ms: int
    period: str


@dataclass(frozen=True)
class DashboardReadDependencies(DashboardQueryPorts):
    """Dashboard計算に必要なClockとRepositoryだけを束ねる。"""

    clock: Clock
    settings: DashboardSettings
    widgets: WidgetRepository
    channels: ChannelRepository
    events: EventRepository
    counter_resets: CounterResetRepository
    output_events: OutputEventRepository
    sessions: SessionRepository
    status_provider: Callable[[], str]
    control_client: ControlClient | None


@dataclass(frozen=True)
class DashboardValues:
    """HTTP表現へ変換する前のwidget計算結果。"""

    window: DashboardWindow
    items: list[dict[str, object]]
    timeline_too_large: bool


def resolve_dashboard_window(
    *,
    clock: Clock,
    settings: DashboardSettings,
    timezone_name: str,
    period: str | None,
    from_ms: int | None,
    to_ms: int | None,
    now_ms: int | None = None,
) -> DashboardWindow:
    """URL指定と保存済み選択から、過去を指すDashboard期間を解決する。

    Args:
        clock: 現在時刻を供給するClock。
        settings: 保存済み共通期間とcustom範囲を読む設定port。
        timezone_name: 暦期間の境界を解釈するIANA timezone。
        period: URLで指定された期間key。省略時は保存済み選択を使う。
        from_ms: URLの開始epoch millisecond。省略時はcustom既定値を使う。
        to_ms: URLの終了epoch millisecond。省略時は現在時刻を使う。
        now_ms: 現在値とwidget期間で共有するUTC epoch millisecond。

    Returns:
        現在時刻以下で、開始を含み終了を含まないDashboard期間。

    Raises:
        ValueError: period、timezone、または空の過去期間が不正な場合。
    """
    if now_ms is None:
        now_ms = clock.now_utc_ms()
    selected_period = period
    if selected_period is None and from_ms is None:
        selected_period = _stored_dashboard_period(settings)
    if selected_period is None or selected_period == "custom":
        stored_range = _stored_dashboard_custom_range(settings)
        start = from_ms
        end = to_ms
        if start is None and stored_range is not None:
            start = stored_range["from_ms"]
        if end is None and stored_range is not None and from_ms is None:
            end = stored_range["to_ms"]
        if start is None:
            raise ValueError("custom range requires a non-negative from_ms")
        if end is None:
            end = now_ms
    else:
        start, end = _dashboard_range_ms(
            clock,
            timezone_name=timezone_name,
            period=selected_period,
            now_ms=now_ms,
        )
    start = max(0, min(start, now_ms - 1))
    end = min(end, now_ms)
    if start >= end:
        raise ValueError("from_ms must be before to_ms and both must include past time")
    return DashboardWindow(start, end, selected_period or "custom")


def load_dashboard_values(
    dependencies: DashboardReadDependencies,
    *,
    timezone_name: str,
    period: str | None,
    from_ms: int | None,
    to_ms: int | None,
) -> DashboardValues:
    """Dashboardの期間・Repository・live状態を解釈してwidget値を計算する。

    Args:
        dependencies: Dashboardで必要なClock、設定、Repository、status provider。
        timezone_name: 暦期間境界を解決するIANA timezone。
        period: URLの期間key。
        from_ms: URLの開始epoch millisecond。
        to_ms: URLの終了epoch millisecond。

    Returns:
        表示用timestampへ変換する前の計算済みitemsと期間。timeline上限超過時は
        itemsを空にしてtimeline_too_largeをTrueにする。

    Raises:
        ValueError: 期間またはwidgetの固定暦範囲が不正な場合。
    """
    # Query時刻を先に固定し、期間解決が暦時計を読む場合も各widgetに同じnowを渡す。
    now_ms = dependencies.clock.now_utc_ms()
    window = resolve_dashboard_window(
        clock=dependencies.clock,
        settings=dependencies.settings,
        timezone_name=timezone_name,
        period=period,
        from_ms=from_ms,
        to_ms=to_ms,
        now_ms=now_ms,
    )
    now_mono_ns = dependencies.clock.now_monotonic_ns()
    widgets = dependencies.widgets.list_all(include_disabled=False)
    channels = dependencies.channels.list_all()
    enabled_channel_ids = {channel.id for channel in channels if channel.enabled}
    disabled_channel_ids = frozenset(
        channel.id for channel in channels if not channel.enabled
    )
    fixed_calendar_ranges = _fixed_widget_calendar_ranges(
        widgets,
        dependencies.clock,
        timezone_name=timezone_name,
        now_ms=now_ms,
    )
    if _dashboard_timeline_event_count_exceeds_limit(
        dependencies,
        widgets,
        start_ms=window.start_ms,
        period_end_ms=window.end_ms,
        now_ms=now_ms,
        fixed_calendar_ranges=fixed_calendar_ranges,
    ):
        return DashboardValues(window, [], True)
    # timelineのCOUNT上限を確認してからIPCを行う。重い応答はdaemonや通常件数queryを
    # 伴わせず、request単位のRepository負荷を先に確定させる。
    daemon_status, _, daemon_data = _read_daemon_snapshot(
        dependencies.control_client,
        dependencies.status_provider,
    )
    widget_ranges = {
        widget.id: (
            fixed_calendar_ranges[widget.id]
            if widget.id in fixed_calendar_ranges
            else (
                widget_start_ms(
                    widget,
                    dashboard_start_ms=window.start_ms,
                    now_ms=now_ms,
                ),
                widget_end_ms(
                    widget,
                    dashboard_end_ms=window.end_ms,
                    now_ms=now_ms,
                ),
            )
        )
        for widget in widgets
    }
    resets = {
        widget.id: dependencies.counter_resets.latest(widget.id) for widget in widgets
    }
    reset_counts = _dashboard_reset_counts(dependencies, widgets, resets)
    period_counts = _dashboard_period_counts(dependencies, widgets, widget_ranges)
    stored_counts = {
        widget.id: _stored_widget_count(dependencies, widget)
        for widget in widgets
        if widget.config.get("count_mode", "period") == "stored"
    }
    sessions = dependencies.sessions.list_all()
    active_session_id = _active_session_id(
        daemon_status,
        daemon_data,
    )
    current_inputs, current_input_since_ns = _live_input_snapshot(
        daemon_data,
        active_session_id=active_session_id,
        enabled_channel_ids=enabled_channel_ids,
    )
    if (
        active_session_id is None
        and daemon_status == "unknown"
        and dependencies.control_client is None
    ):
        # Web単体fixtureはdaemonを持たないため、未終了sessionが一つだけなら
        # 表示上activeとして扱いOUT履歴をUNKNOWNに潰さない。
        open_sessions = [
            session.id for session in sessions if session.ended_at_ms is None
        ]
        if len(open_sessions) == 1:
            active_session_id = open_sessions[0]
    events = _dashboard_events(
        dependencies,
        widgets,
        widget_ranges=widget_ranges,
        current_before_ms=now_ms + 1,
        active_session_id=active_session_id,
    )
    output_events = _dashboard_output_events(
        dependencies,
        widgets,
        widget_ranges=widget_ranges,
    )
    items: list[dict[str, object]] = []
    for widget in widgets:
        reset: CounterReset | None = resets[widget.id]
        try:
            value = evaluate_widget(
                widget,
                events=events,
                sessions=sessions,
                now_ms=now_ms,
                start_ms=window.start_ms,
                period_end_ms=window.end_ms,
                fixed_period_range=fixed_calendar_ranges.get(widget.id),
                active_session_id=active_session_id,
                now_mono_ns=now_mono_ns,
                current_inputs=current_inputs,
                current_input_since_ns=current_input_since_ns,
                disabled_channel_ids=disabled_channel_ids,
                output_events=output_events,
                carried_count=reset.carried_count if reset else 0,
                after_event_id=reset.after_event_id if reset else None,
                reset_at_ms=reset.reset_at_ms if reset else None,
                reset_count=reset_counts.get(widget.id),
                period_count=period_counts.get(widget.id),
                stored_count=stored_counts.get(widget.id),
            )
        except ValueError as error:
            value = {
                "widget_id": widget.id,
                "type": widget.widget_type.value,
                "title": widget.title,
                "error": str(error),
            }
        items.append(value)
    return DashboardValues(
        window,
        items,
        _dashboard_timeline_interval_count(items) > _DASHBOARD_TIMELINE_MAX_INTERVALS,
    )


def _active_session_id(
    daemon_status: str,
    daemon_data: Mapping[str, object],
) -> int | None:
    """STATUS snapshotから記録中と確認できたsession IDだけを読む。"""
    raw_session_id = daemon_data.get("session_id")
    if daemon_status == "logging" and isinstance(raw_session_id, int):
        return raw_session_id
    return None


def _live_input_snapshot(
    daemon_data: Mapping[str, object],
    *,
    active_session_id: int | None,
    enabled_channel_ids: set[int],
) -> tuple[dict[int, bool], dict[int, int]]:
    """STATUS応答から監視中channelの論理値とmonotonic開始値を読む。

    daemon応答にchannelがあってもWeb設定で無効な入力は、記録対象の証跡がなく
    DomainでUNKNOWNにするため含めない。bool値と不正値も入力論理値に変換しない。
    """
    current_inputs: dict[int, bool] = {}
    current_since: dict[int, int] = {}
    raw_inputs = daemon_data.get("inputs")
    if active_session_id is not None and isinstance(raw_inputs, Mapping):
        for raw_channel, raw_value in raw_inputs.items():
            try:
                channel_id = int(raw_channel)
            except (TypeError, ValueError):
                continue
            if channel_id not in enabled_channel_ids:
                continue
            if isinstance(raw_value, bool) or raw_value not in (0, 1):
                continue
            current_inputs[channel_id] = bool(raw_value)
    raw_since = daemon_data.get("input_since_mono_ns")
    if active_session_id is not None and isinstance(raw_since, Mapping):
        for raw_channel, value in raw_since.items():
            try:
                channel_id = int(raw_channel)
            except (TypeError, ValueError):
                continue
            if channel_id not in enabled_channel_ids:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            current_since[channel_id] = value
    return current_inputs, current_since
