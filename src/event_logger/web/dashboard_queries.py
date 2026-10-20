"""Webダッシュボードが必要とするDB履歴取得と集計の調整。

HTTPルートと独立し、DBへのquery範囲を表示項目に応じて制限する。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, cast

from event_logger.common.models import CounterReset, InputEvent, OutputEvent
from event_logger.database.repositories import (
    CounterResetRepository,
    EventRepository,
    OutputEventRepository,
)
from event_logger.domain.widgets import (
    Widget,
    WidgetType,
    widget_end_ms,
    widget_start_ms,
)

_DASHBOARD_TIMELINE_MAX_INTERVALS = 10_000


class DashboardQueryPorts(Protocol):
    """Dashboard集計に必要なRepository portだけを公開する。"""

    events: EventRepository
    counter_resets: CounterResetRepository
    output_events: OutputEventRepository


def _dashboard_events(
    context: DashboardQueryPorts,
    widgets: list[Widget],
    *,
    widget_ranges: Mapping[str, tuple[int, int]],
    current_before_ms: int,
    active_session_id: int | None = None,
) -> list[InputEvent]:
    """Dashboardに必要なeventだけを取得する。

            以前の実装は現在時刻までのeventとactive session全件を毎回読み込んでいた。
            ここではwidgetの実効期間内event、開始直前状態、現在値計算に必要な最新edge
            だけを取得する。reset型counterの件数は別のSQL COUNTで集計し、長期保存
            された生ログをメモリへ展開しない。live inputの経過時間はdaemon snapshotを
            使い、DBへ退避した場合もsessionごとの最後のeventだけを読む。


        Args:
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            widgets: Dashboard計算対象となるwidget設定の順序付き集合。
            widget_ranges: widget IDごとの集計時間範囲。
            current_before_ms: 現在以前を含む上限時刻（UTC Unix epoch millisecond）。
            active_session_id: 対象logger sessionの識別子。

        Returns:
            選択widget rangeに必要なInputEvent列。
                期間外や監視対象外の入力eventは取得しない。
    """
    channel_ranges: dict[int, tuple[int, int]] = {}
    lookup_ranges: dict[int, tuple[int, int]] = {}
    events_by_id: dict[int, InputEvent] = {}
    # widget種別で必要な証跡が異なる。最新N件・特定edgeは専用queryで上限を
    # DB側に適用し、duration/timelineだけ期間履歴と境界状態を後でまとめて取る。
    for widget in widgets:
        if widget.widget_type is WidgetType.EVENT_COUNT:
            # 全カウント方式をRepositoryのCOUNTで集計する。reset/storedも
            # 履歴行は不要で、長期期間の選択によるメモリ消費を増やさない。
            continue
        time_range = widget_ranges.get(widget.id)
        if time_range is None or time_range[0] >= time_range[1]:
            continue
        input_channels = _widget_input_channels(widget)
        if widget.widget_type is WidgetType.EVENT_LIST:
            # IN/OUTそれぞれ最新N件だけ取得すれば、統合後の最新N件を保てる。
            # 先に全期間を展開してからsliceすると、表示件数制限が働かない。
            if input_channels:
                for event in context.events.list_between(
                    start_ms=time_range[0],
                    end_ms=time_range[1],
                    channel_ids=sorted(input_channels),
                    limit=cast(int, widget.config.get("limit", 10)),
                    descending=True,
                ):
                    events_by_id[event.id] = event
            continue
        if widget.widget_type is WidgetType.LAST_EVENT:
            # 複数widgetの期間をまとめると、短い方の最新edgeが消え得る。
            # 各期間へ限定して検索し、NTP逆行時も期間内の最大IDを維持する。
            for channel_id in input_channels:
                last_event = context.events.last_edge_between(
                    channel_id=channel_id,
                    value=0 if widget.config.get("edge") == "falling" else 1,
                    start_ms=time_range[0],
                    end_ms=time_range[1],
                )
                if last_event is not None:
                    events_by_id[last_event.id] = last_event
            continue
        for channel_id in _widget_input_channels(widget):
            _merge_channel_range(lookup_ranges, channel_id, time_range)
            if widget.widget_type in {WidgetType.ON_OFF_DURATION, WidgetType.TIMELINE}:
                # 現在状態はdaemonの確定値を使う。旧daemon/testのfallbackにも
                # 以下の直前値・最新edge・session末尾だけを渡し、全履歴は読まない。
                _merge_channel_range(channel_ranges, channel_id, time_range)

    if channel_ranges:
        # 同じ期間のchannelをまとめて読み、長い固定期間widgetが別channelの
        # 短いdashboard windowまで広げないよう期間ごとにqueryを分ける。
        channels_by_range: dict[tuple[int, int], list[int]] = {}
        for channel_id, time_range in sorted(channel_ranges.items()):
            channels_by_range.setdefault(time_range, []).append(channel_id)
        for (start_ms, end_ms), channel_ids in channels_by_range.items():
            candidates = context.events.list_between(
                start_ms=start_ms,
                end_ms=end_ms,
                channel_ids=channel_ids,
            )
            for event in candidates:
                events_by_id[event.id] = event

    for channel_id, (start_ms, end_ms) in sorted(lookup_ranges.items()):
        previous = context.events.last_before(
            channel_id=channel_id,
            before_ms=start_ms,
        )
        for value in (0, 1):
            latest_edge = context.events.last_edge_before(
                channel_id=channel_id,
                value=value,
                before_ms=end_ms,
            )
            if latest_edge is not None:
                events_by_id[latest_edge.id] = latest_edge
            # elapsed-time/event widgetは選択期間外でも現在の最新edgeを必要とする。
            # 同じedgeならIDで重複を除き、期間内widgetはDomain側の境界filterを使う。
            current_edge = context.events.last_edge_before(
                channel_id=channel_id,
                value=value,
                before_ms=current_before_ms,
            )
            if current_edge is not None:
                events_by_id[current_edge.id] = current_edge
        if previous is not None:
            events_by_id[previous.id] = previous
        if active_session_id is not None:
            # daemon statusがないtest/demo fallbackでも、全session走査せず現在の
            # session状態を復元できるようチャンネルごとの最後のeventだけを足す。
            latest_session_event = context.events.last_for_session_before(
                session_id=active_session_id,
                channel_id=channel_id,
                before_ms=current_before_ms,
            )
            if latest_session_event is not None:
                events_by_id[latest_session_event.id] = latest_session_event

    return sorted(events_by_id.values(), key=lambda event: event.id)


def _dashboard_period_counts(
    context: DashboardQueryPorts,
    widgets: list[Widget],
    widget_ranges: Mapping[str, tuple[int, int]],
) -> dict[str, int]:
    """period型event_countをRepositoryの範囲集計で計算する。

        Args:
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            widgets: Dashboard計算対象となるwidget設定の順序付き集合。
            widget_ranges: widget IDごとの集計時間範囲。

        Returns:
            period型event_count widget IDから件数へのdict。
                Repository aggregateを使い個別event rowsを展開しない。
    """
    counts: dict[str, int] = {}
    cache: dict[tuple[int, int, int, int], int] = {}
    for widget in widgets:
        if (
            widget.widget_type is not WidgetType.EVENT_COUNT
            or widget.config.get("count_mode", "period") != "period"
        ):
            continue
        time_range = widget_ranges.get(widget.id)
        raw_channels = widget.config.get("channels")
        if (
            time_range is None
            or time_range[0] >= time_range[1]
            or not isinstance(raw_channels, list)
            or len(raw_channels) != 1
        ):
            continue
        channel_id = raw_channels[0]
        if (
            isinstance(channel_id, bool)
            or not isinstance(channel_id, int)
            or not 1 <= channel_id <= 6
        ):
            continue
        start_ms, end_ms = time_range
        edge_value = 1 if widget.config.get("edge", "rising") == "rising" else 0
        cache_key = (channel_id, edge_value, start_ms, end_ms)
        if cache_key not in cache:
            cache[cache_key] = context.events.count_edges(
                channel_id=channel_id,
                value=edge_value,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        counts[widget.id] = cache[cache_key]
    return counts


def _merge_channel_range(
    channel_ranges: dict[int, tuple[int, int]],
    channel_id: int,
    time_range: tuple[int, int],
) -> None:
    """同じchannelのwidget期間を、必要な最小の包含windowへまとめる。

Args:
    channel_ranges: チャンネルごとの時間範囲。複数widgetの問い合わせを統合する際に使う。
    channel_id: 対象チャンネルの製品上の番号。
    time_range: widgetが集計するUTC millisecondの(start_ms, end_ms)半開範囲。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    current = channel_ranges.get(channel_id)
    if current is None:
        channel_ranges[channel_id] = time_range
        return
    channel_ranges[channel_id] = (
        min(current[0], time_range[0]),
        max(current[1], time_range[1]),
    )


def _dashboard_reset_counts(
    context: DashboardQueryPorts,
    widgets: list[Widget],
    resets: Mapping[str, CounterReset | None],
) -> dict[str, int]:
    """reset型counterをSQLのCOUNTだけで集計する。

        Args:
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            widgets: Dashboard計算対象となるwidget設定の順序付き集合。
            resets: widgetごとのcounter reset基準情報。

        Returns:
            reset型counter widget IDからreset基準以降の件数へのdict。
                基準event IDより後だけを数える。
    """
    counts: dict[str, int] = {}
    for widget in widgets:
        if widget.widget_type is not WidgetType.EVENT_COUNT:
            continue
        if widget.config.get("count_mode", "period") != "reset":
            continue
        reset = resets.get(widget.id)
        if reset is None:
            continue
        raw_channels = widget.config.get("channels")
        if not isinstance(raw_channels, list) or len(raw_channels) != 1:
            continue
        channel_id = raw_channels[0]
        if (
            isinstance(channel_id, bool)
            or not isinstance(channel_id, int)
            or not 1 <= channel_id <= 6
        ):
            continue
        edge_value = 1 if widget.config.get("edge", "rising") == "rising" else 0
        counts[widget.id] = context.counter_resets.count_total(
            reset_id=reset.id,
            channel_id=channel_id,
            value=edge_value,
        )
    return counts


def _dashboard_output_events(
    context: DashboardQueryPorts,
    widgets: list[Widget],
    *,
    widget_ranges: Mapping[str, tuple[int, int]],
) -> list[OutputEvent]:
    """DashboardのOUT widgetごとに必要な期間履歴だけを取得する。

        Args:
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            widgets: Dashboard計算対象となるwidget設定の順序付き集合。
            widget_ranges: widget IDごとの集計時間範囲。

        Returns:
            Dashboard OUT widgetの表示範囲に必要なOutputEvent列。
    """
    output_ranges: dict[int, tuple[int, int]] = {}
    events_by_id: dict[int, OutputEvent] = {}
    # event-listはDB側で件数を制限し、他の履歴widgetは同じ期間のchannelを
    # まとめて読む。IN側のevent queryと混ぜずOUT repositoryの境界を保つ。
    for widget in widgets:
        time_range = widget_ranges.get(widget.id)
        raw_signals = widget.config.get("signals")
        if time_range is None or time_range[0] >= time_range[1]:
            continue
        if not isinstance(raw_signals, list):
            continue
        output_channels = [
            int(signal[3:])
            for signal in raw_signals
            if isinstance(signal, str) and signal.upper().startswith("OUT")
        ]
        if widget.widget_type is WidgetType.EVENT_LIST:
            if output_channels:
                for event in context.output_events.list_between(
                    start_ms=time_range[0],
                    end_ms=time_range[1],
                    output_channels=output_channels,
                    limit=cast(int, widget.config.get("limit", 10)),
                    descending=True,
                ):
                    events_by_id[event.id] = event
            continue
        for raw_signal in raw_signals:
            if isinstance(raw_signal, str) and raw_signal.upper().startswith("OUT"):
                try:
                    channel_id = int(raw_signal[3:])
                except ValueError:
                    continue
                if 1 <= channel_id <= 6:
                    _merge_channel_range(output_ranges, channel_id, time_range)
    if not output_ranges:
        return sorted(events_by_id.values(), key=lambda event: event.id)
    repository = context.output_events
    channels_by_range: dict[tuple[int, int], list[int]] = {}
    for channel_id, time_range in sorted(output_ranges.items()):
        channels_by_range.setdefault(time_range, []).append(channel_id)
    for (start_ms, end_ms), output_channels in channels_by_range.items():
        candidates = repository.list_between(
            start_ms=start_ms,
            end_ms=end_ms,
            output_channels=output_channels,
        )
        for event in candidates:
            events_by_id[event.id] = event
    # 選択期間の最初の状態を描画するため直前のOUT eventを加える。
    # 開始前の全履歴は不要なので各channelにつき最新1件だけ取得する。
    for channel_id, (start_ms, _) in sorted(output_ranges.items()):
        previous = repository.last_before(
            output_channel=channel_id,
            before_ms=start_ms,
        )
        if previous is not None:
            events_by_id[previous.id] = previous
    return sorted(
        events_by_id.values(),
        key=lambda event: event.id,
    )


def _dashboard_timeline_event_count_exceeds_limit(
    context: DashboardQueryPorts,
    widgets: list[Widget],
    *,
    start_ms: int,
    period_end_ms: int,
    now_ms: int,
    fixed_calendar_ranges: Mapping[str, tuple[int, int]],
) -> bool:
    """巨大timelineをevent payloadへ展開する前にDB countで検出する。

Args:
    context: 現在のWebContext。関数が使うRepository、Clock、
        設定値や制御clientを所有する。
    widgets: Dashboard計算対象となるwidget設定の順序付き集合。
    start_ms: 期間の開始を表すUTC Unix epoch millisecond。
    period_end_ms: 期間の排他的な終端を表すUTC Unix epoch millisecond。
    now_ms: 処理時点のUTC Unix epoch millisecond。
    fixed_calendar_ranges: 暦境界を解決済みのwidget range mapping。

Returns:
    判定結果。条件を満たす場合はTrueを返し、
        満たさない場合はFalse（または注釈に示すNone）を返す。"""
    candidate_count = 0
    for widget in widgets:
        if widget.widget_type is not WidgetType.TIMELINE:
            continue
        fixed_range = fixed_calendar_ranges.get(widget.id)
        if fixed_range is not None:
            widget_start_ms_value, widget_end_ms_value = fixed_range
        else:
            widget_start_ms_value = widget_start_ms(
                widget,
                dashboard_start_ms=start_ms,
                now_ms=now_ms,
            )
            widget_end_ms_value = widget_end_ms(
                widget,
                dashboard_end_ms=period_end_ms,
                now_ms=now_ms,
            )
        if widget_start_ms_value >= widget_end_ms_value:
            continue
        for signal in _timeline_widget_signals(widget):
            try:
                if signal.startswith("IN"):
                    channel_id = int(signal[2:])
                    candidate_count += context.events.count_between_limited(
                        start_ms=widget_start_ms_value,
                        end_ms=widget_end_ms_value,
                        channel_id=channel_id,
                        limit=_DASHBOARD_TIMELINE_MAX_INTERVALS - candidate_count + 1,
                    )
                elif signal.startswith("OUT"):
                    channel_id = int(signal[3:])
                    candidate_count += context.output_events.count_between_limited(
                        start_ms=widget_start_ms_value,
                        end_ms=widget_end_ms_value,
                        output_channel=channel_id,
                        limit=_DASHBOARD_TIMELINE_MAX_INTERVALS - candidate_count + 1,
                    )
            except ValueError:
                # WidgetRepositoryのvalidationが通常は拒否するが、破損DBの値を
                # limit計算中に500へ変えず、後続のDomain validationに委ねる。
                continue
            if candidate_count > _DASHBOARD_TIMELINE_MAX_INTERVALS:
                return True
    # active session全体をCOUNTすると、短い期間を選んでも古いeventだけで
    # 上限超過と誤判定する。ここでは期間内の候補だけで早期拒否し、時計補正や
    # session境界を含む正確な区間数はbuild後の検査で必ず確認する。
    return False


def _timeline_widget_signals(widget: Widget) -> list[str]:
    """検証済みtimeline設定からIN/OUT信号名を取り出す。

        Args:
            widget: 対象のDashboard widget設定。

        Returns:
            widget configの検証済みINn/OUTn signal ID列。
    """
    raw_signals = widget.config.get("signals")
    if isinstance(raw_signals, list):
        return [signal.upper() for signal in raw_signals if isinstance(signal, str)]
    raw_channels = widget.config.get("channels")
    if isinstance(raw_channels, list):
        return [f"IN{channel}" for channel in raw_channels if isinstance(channel, int)]
    return []


def _dashboard_timeline_interval_count(items: list[dict[str, object]]) -> int:
    """timeline群のinterval合計数を返す。

        Args:
            items: 描画・集計対象となる順序付き項目。

        Returns:
            指定timeline payload内のinterval総数。
    """
    count = 0
    for item in items:
        if item.get("type") != WidgetType.TIMELINE.value:
            continue
        channels = item.get("channels")
        if not isinstance(channels, list):
            continue
        for channel in channels:
            if isinstance(channel, Mapping):
                intervals = channel.get("intervals")
                if isinstance(intervals, list):
                    count += len(intervals)
    return count


def _dashboard_timeline_limit_warning(
    *,
    start_ms: int,
    end_ms: int,
    period: str,
    timezone_name: str,
) -> dict[str, object]:
    """上限超過時に部分timelineを返さず、絞り込み方法を案内する。

        Args:
            start_ms: 期間の開始を表すUTC Unix epoch millisecond。
            end_ms: 期間の終端を表すUTC Unix epoch millisecond。
            period: Dashboardまたは検索で選択された期間キー。
            timezone_name: 日時表示・暦境界計算に使うIANA timezone名。

        Returns:
            timeline過大時の警告dict。部分timelineは含めず、選択期間・絞込み案内を含む。
    """
    return {
        "from_ms": start_ms,
        "to_ms": end_ms,
        "period": period,
        "timezone": timezone_name,
        "warning": {
            "code": "TIMELINE_LIMIT_EXCEEDED",
            "message": (
                "タイムラインの表示上限を超えています。期間または表示信号を "
                "絞り込んでください。データの間引きはしていません。"
            ),
        },
    }


def _widget_input_channels(widget: Widget) -> set[int]:
    """widget設定からDashboardが参照するIN番号だけを取り出す。

        Args:
            widget: 対象のDashboard widget設定。

        Returns:
            widget configが参照するIN channel IDのset。OUT signalは含めない。
    """
    raw_channels = widget.config.get("channels")
    if isinstance(raw_channels, list):
        return {
            channel
            for channel in raw_channels
            if (
                isinstance(channel, int)
                and not isinstance(channel, bool)
                and 1 <= channel <= 6
            )
        }
    raw_signals = widget.config.get("signals")
    if not isinstance(raw_signals, list):
        return set()
    channels: set[int] = set()
    for raw_signal in raw_signals:
        if not isinstance(raw_signal, str) or not raw_signal.upper().startswith("IN"):
            continue
        try:
            channel_id = int(raw_signal[2:])
        except ValueError:
            continue
        if 1 <= channel_id <= 6:
            channels.add(channel_id)
    return channels


def _stored_widget_count(context: DashboardQueryPorts, widget: Widget) -> int:
    """保存期間内累計widgetを全保存eventから数える。

        Args:
            context: 現在のWebContext。関数が使うRepository、Clock、
                設定値や制御clientを所有する。
            widget: 対象のDashboard widget設定。

        Returns:
            counter reset値を差し引かずに数えた保存event総数。
                retention済みの現存eventを対象とする。
    """
    raw_channels = widget.config.get("channels")
    if not isinstance(raw_channels, list) or len(raw_channels) != 1:
        return 0
    channel_id = raw_channels[0]
    if isinstance(channel_id, bool) or not isinstance(channel_id, int):
        return 0
    edge = widget.config.get("edge", "rising")
    value = 1 if edge == "rising" else 0
    return context.events.count_edges(channel_id=channel_id, value=value)
