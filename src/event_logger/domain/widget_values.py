"""生イベントからDashboard widgetの表示値を計算する。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import cast

from event_logger.common.models import (
    DisplayState,
    EventKind,
    InputEvent,
    OutputEvent,
    Session,
    StateInterval,
)
from event_logger.domain.counters import count_edges
from event_logger.domain.timeline import build_state_intervals, total_duration_ms
from event_logger.domain.widgets import (
    Widget,
    WidgetType,
    widget_end_ms,
    widget_start_ms,
)


def evaluate_widget(
    widget: Widget,
    *,
    events: Sequence[InputEvent],
    sessions: Sequence[Session],
    now_ms: int,
    start_ms: int,
    period_end_ms: int | None = None,
    fixed_period_range: tuple[int, int] | None = None,
    active_session_id: int | None,
    now_mono_ns: int | None = None,
    current_inputs: Mapping[int, bool] | None = None,
    current_input_since_ns: Mapping[int, int] | None = None,
    disabled_channel_ids: frozenset[int] = frozenset(),
    output_events: Sequence[OutputEvent] = (),
    carried_count: int = 0,
    after_event_id: int | None = None,
    reset_at_ms: int | None = None,
    reset_count: int | None = None,
    period_count: int | None = None,
    stored_count: int | None = None,
) -> dict[str, object]:
    """1 widgetの設定に応じた表示値を返す。

    UNKNOWN区間はON/OFFへ補間しない。 DBに保存されたイベントとsessionだけを
    用いて計算し、 表示層で別の集計ロジックを複製しない。 暦に基づく固定期間は
    timezoneを扱うWeb adapterがUTCの開始・終了境界をまとめて渡す。

    Args:
        widget: 表示・集計対象のWidget設定。 信号と期間はconfigで決まる。
        events: 入力event列。 event IDを保存順の基準とし、 EDGEとSNAPSHOTを区別する。
        sessions: 監視session列。 異なるsessionをまたぐ状態継続を作らない。
        now_ms: 現在時刻（UTC epoch ms）。 期間やwall-clock表示の基準。
        start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
        period_end_ms: 選択期間の排他的終了時刻（UTC epoch ms）。
        fixed_period_range: Web層で暦境界を解決済みの場合の(start_ms, end_ms)。
            両値はUTC epoch ms、 開始包含・終了排他。
            Noneならwidget設定から期間を決める。
        active_session_id: 現在監視中session ID。
            明示されたsessionだけ現在時刻まで延長する。
        now_mono_ns: 現在monotonic timestamp（ns）。 active
            session内の経過時間計算に使う。
        current_inputs: IN番号ごとの現在論理状態。 widgetのlive表示に使う。
        current_input_since_ns: IN番号から状態継続開始monotonic時刻（ns）へのmapping。
        disabled_channel_ids: 無効状態としてwidget表示や集計から除くchannel番号集合。
        output_events: loggerが保存したOUT履歴。 入力eventとは別のsignalとして扱う。
        carried_count: 最新resetより後のedgeのうちretentionで削除済みの件数。
            残存event件数へ加算してcounter totalを維持する。
        after_event_id: このIDより大きいeventだけを数える排他的reset境界。
        reset_at_ms: counter reset記録の時刻（UTC epoch ms）。
        reset_count: reset後countの事前計算値。 Noneならevent ID境界から算出する。
        period_count: 期間内edge件数の事前計算値。 Noneならevent列から算出する。
        stored_count: DB保存済みcounter値。 Noneならevent履歴へfallbackする。

    Returns:
        widget APIへ返す表示field/value mapping。

    Raises:
        ValueError: 「start_ms must be before now_ms」に該当すると判定した場合。
    """
    # Web層が渡した暦期間を優先し、それ以外はwidget設定の期間を同じ境界形式へ解決する。
    effective_start_ms = (
        fixed_period_range[0]
        if fixed_period_range is not None
        else widget_start_ms(
            widget,
            dashboard_start_ms=start_ms,
            now_ms=now_ms,
        )
    )
    effective_end_ms = min(
        now_ms,
        fixed_period_range[1]
        if fixed_period_range is not None
        else widget_end_ms(
            widget,
            dashboard_end_ms=period_end_ms or now_ms,
            now_ms=now_ms,
        ),
    )
    if effective_start_ms >= effective_end_ms:
        raise ValueError("start_ms must be before now_ms")
    # 終了境界より後の履歴を先に除外し、以後のwidget分岐へ同じsnapshotを渡す。
    period_events = [event for event in events if event.ts_utc_ms < effective_end_ms]
    period_output_events = [
        event for event in output_events if event.ts_utc_ms < effective_end_ms
    ]
    period_session_id = active_session_id if effective_end_ms >= now_ms else None
    # 全widgetで共通の識別・並び替え情報を先に作り、個別表示値へ合成する。
    base: dict[str, object] = {
        "widget_id": widget.id,
        "type": widget.widget_type.value,
        "title": widget.title,
        "size": widget.size.value,
        "sort_order": widget.sort_order,
    }
    # 回数はevent ID境界とretention carryで求めるため、
    # 状態区間を使うwidgetとは別経路で計算する。
    if widget.widget_type is WidgetType.EVENT_COUNT:
        # event_count自身はconfigを読むが、他の単一入力widgetと同じ境界検証を
        # ここでも通し、不正なchannels設定を表示計算まで持ち込まない。
        _single_channel(widget)
        count_mode = str(widget.config.get("count_mode", "period"))
        return {
            **base,
            "value": _event_count(
                widget,
                period_events,
                start_ms=effective_start_ms,
                end_ms=effective_end_ms,
                carried_count=carried_count,
                after_event_id=after_event_id,
                reset_count=reset_count,
                period_count=period_count,
                stored_count=stored_count,
            ),
            "unit": str(widget.config.get("unit", "回")),
            "count_mode": count_mode,
            "reset_available": count_mode == "reset",
            # reset_at_msは表示用の派生情報であり、集計境界そのものは
            # after_event_idに保持する。リセット前の値を時刻で再計算しない。
            "reset_at_ms": reset_at_ms if count_mode == "reset" else None,
        }
    # timelineは設定されたIN/OUT signalごとに時系列区間を構成する。
    if widget.widget_type is WidgetType.TIMELINE:
        return {
            **base,
            "channels": _timeline_rows(
                widget,
                period_events,
                period_output_events,
                sessions,
                start_ms=effective_start_ms,
                end_ms=effective_end_ms,
                active_session_id=period_session_id,
                now_mono_ns=(now_mono_ns if period_session_id is not None else None),
            ),
        }
    # event listは状態区間へ集約せず、期間内の元eventを並べて返す。
    if widget.widget_type is WidgetType.EVENT_LIST:
        return {
            **base,
            "events": _event_list_rows(
                widget,
                period_events,
                period_output_events,
                start_ms=effective_start_ms,
                end_ms=effective_end_ms,
            ),
        }
    # ここからは単一入力channelを使うwidget群。
    # 複数signal対応のtimeline/listは上で処理済み。
    channel_id = _single_channel(widget)
    # 監視中は現在値snapshotを優先し、無効channelはUNKNOWNのまま表示する。
    if widget.widget_type is WidgetType.CURRENT_STATE:
        if channel_id in disabled_channel_ids:
            return {
                **base,
                "state": "UNKNOWN",
                "on_label": str(widget.config.get("on_label", "ON")),
                "off_label": str(widget.config.get("off_label", "OFF")),
            }
        if (
            active_session_id is not None
            and current_inputs is not None
            and channel_id in current_inputs
        ):
            current_state: DisplayState = "ON" if current_inputs[channel_id] else "OFF"
            direct_since_ns = (
                current_input_since_ns.get(channel_id)
                if current_input_since_ns is not None
                else None
            )
            result = {
                **base,
                "state": current_state,
                "on_label": str(widget.config.get("on_label", "ON")),
                "off_label": str(widget.config.get("off_label", "OFF")),
            }
            if (
                widget.config.get("show_elapsed", False)
                and now_mono_ns is not None
                and direct_since_ns is not None
            ):
                result["elapsed_ms"] = max(
                    0, (now_mono_ns - direct_since_ns) // 1_000_000
                )
            return result
    # event基準は最後の一致edge、state基準は対象状態の継続区間から経過時間を求める。
    if widget.widget_type is WidgetType.ELAPSED_TIME:
        if widget.config.get("source", "state") == "event":
            event = _last_edge(events, channel_id, _edge_value(widget))
            return {
                **base,
                "elapsed_ms": (
                    _event_elapsed_ms(
                        event,
                        now_ms=now_ms,
                        now_mono_ns=now_mono_ns,
                        active_session_id=active_session_id,
                    )
                    if event
                    else None
                ),
            }
        if (
            active_session_id is not None
            and current_inputs is not None
            and channel_id in current_inputs
            and current_input_since_ns is not None
            and channel_id in current_input_since_ns
            and now_mono_ns is not None
        ):
            current_state = "ON" if current_inputs[channel_id] else "OFF"
            state = str(widget.config.get("state", "ON"))
            since_ns = current_input_since_ns[channel_id]
            return {
                **base,
                "elapsed_ms": (
                    max(0, (now_mono_ns - since_ns) // 1_000_000)
                    if current_state == state
                    else None
                ),
            }
    # last eventは選択期間内の一致edgeを探し、記録時刻と経過時間を返す。
    if widget.widget_type is WidgetType.LAST_EVENT:
        event = _last_edge(
            period_events,
            channel_id,
            _edge_value(widget),
            start_ms=effective_start_ms,
            end_ms=effective_end_ms,
        )
        return {
            **base,
            "timestamp_ms": event.ts_utc_ms if event is not None else None,
            "elapsed_ms": (
                _event_elapsed_ms(
                    event,
                    now_ms=now_ms,
                    now_mono_ns=now_mono_ns,
                    active_session_id=active_session_id,
                )
                if event
                else None
            ),
        }
    # ON/OFF時間・継続時間の集計に使う状態区間を生成する。
    intervals = build_state_intervals(
        events,
        sessions,
        channel_id=channel_id,
        start_ms=effective_start_ms,
        end_ms=effective_end_ms,
        active_session_id=period_session_id,
        end_mono_ns=(now_mono_ns if period_session_id is not None else None),
    )
    if widget.widget_type is WidgetType.CURRENT_STATE:
        current_intervals = build_state_intervals(
            events,
            sessions,
            channel_id=channel_id,
            start_ms=0,
            end_ms=now_ms,
            active_session_id=active_session_id,
            end_mono_ns=now_mono_ns,
        )
        interval = _current_interval(current_intervals, now_ms, channel_id)
        result = {
            **base,
            "state": interval.state,
            "on_label": str(widget.config.get("on_label", "ON")),
            "off_label": str(widget.config.get("off_label", "OFF")),
        }
        if widget.config.get("show_elapsed", False) and interval.state != "UNKNOWN":
            result["elapsed_ms"] = (
                interval.duration_ms
                if interval.end_ms == now_ms and interval.duration_ms is not None
                else max(0, now_ms - interval.start_ms)
            )
        return result
    if widget.widget_type is WidgetType.ON_OFF_DURATION:
        state = cast(DisplayState, str(widget.config.get("state", "ON")))
        return {
            **base,
            "state": state,
            "duration_ms": total_duration_ms(intervals, state=state),
        }
    if widget.widget_type is WidgetType.ELAPSED_TIME:
        # 経過時間widgetは期間内だけでなくsession開始以降の区間が必要なため、
        # 全履歴範囲を作り直す。
        elapsed_intervals = build_state_intervals(
            events,
            sessions,
            channel_id=channel_id,
            start_ms=0,
            end_ms=now_ms,
            active_session_id=active_session_id,
            end_mono_ns=now_mono_ns,
        )
        return {
            **base,
            "elapsed_ms": _elapsed_time(
                widget,
                events,
                elapsed_intervals,
                channel_id=channel_id,
                now_ms=now_ms,
                now_mono_ns=now_mono_ns,
                active_session_id=active_session_id,
            ),
        }
    raise ValueError(f"unsupported widget type: {widget.widget_type.value}")


def _single_channel(widget: Widget) -> int:
    """入力専用widgetから単一の入力チャンネルを取得する。

    Args:
        widget: 表示・集計対象のWidget設定。 信号と期間はconfigで決まる。

    Returns:
        条件に従って算出した整数値。

    Raises:
        ValueError: 「widget channel must be an integer」に該当すると判定した場合。
    """
    channels = widget.config.get("channels")
    if not isinstance(channels, list) or len(channels) != 1:
        raise ValueError(f"{widget.widget_type.value} requires one input channel")
    channel_id = channels[0]
    if isinstance(channel_id, bool) or not isinstance(channel_id, int):
        raise ValueError("widget channel must be an integer")
    return cast(int, channel_id)


def _widget_signals(widget: Widget) -> list[str]:
    """timeline/event_listの対象信号をIN/OUT表記へ正規化する。

    Args:
        widget: 表示・集計対象のWidget設定。 信号と期間はconfigで決まる。

    Returns:
        指定signalを入力順に正規化したlist。

    Raises:
        ValueError: 「widget channels must be a non-empty
            list」に該当すると判定した場合。 「widget signals must be a non-empty
            list」に該当すると判定した場合。
    """
    raw_signals = widget.config.get("signals")
    if raw_signals is not None:
        if not isinstance(raw_signals, list) or not raw_signals:
            raise ValueError("widget signals must be a non-empty list")
        return [str(signal).upper() for signal in raw_signals]
    raw_channels = widget.config.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ValueError("widget channels must be a non-empty list")
    return [f"IN{channel}" for channel in raw_channels]


def _timeline_rows(
    widget: Widget,
    events: Sequence[InputEvent],
    output_events: Sequence[OutputEvent],
    sessions: Sequence[Session],
    *,
    start_ms: int,
    end_ms: int,
    active_session_id: int | None,
    now_mono_ns: int | None,
) -> list[dict[str, object]]:
    """IN/OUT対象信号を区間へ変換する。

    Args:
        widget: 表示・集計対象のWidget設定。 信号と期間はconfigで決まる。
        events: 入力event列。 event IDを保存順の基準とし、 EDGEとSNAPSHOTを区別する。
        output_events: loggerが保存したOUT履歴。 入力eventとは別のsignalとして扱う。
        sessions: 監視session列。 異なるsessionをまたぐ状態継続を作らない。
        start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
        end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。
        active_session_id: 現在監視中session ID。
            明示されたsessionだけ現在時刻まで延長する。
        now_mono_ns: 現在monotonic timestamp（ns）。 active
            session内の経過時間計算に使う。

    Returns:
        条件に一致したrowまたは表示値列。 該当なしは空list。
    """
    # 信号ごとにINのtimeline区間またはOUT event由来区間を生成して行へまとめる。
    rows: list[dict[str, object]] = []
    for signal in _widget_signals(widget):
        if signal.startswith("IN"):
            channel_id = int(signal[2:])
            intervals = build_state_intervals(
                events,
                sessions,
                channel_id=channel_id,
                start_ms=start_ms,
                end_ms=end_ms,
                active_session_id=active_session_id,
                end_mono_ns=now_mono_ns,
            )
            rows.append(
                {
                    "signal": signal,
                    "channel_id": channel_id,
                    "intervals": [
                        _interval_payload(interval) for interval in intervals
                    ],
                }
            )
            continue
        channel_id = int(signal[3:])
        rows.append(
            {
                "signal": signal,
                "channel_id": channel_id,
                "intervals": _output_interval_payloads(
                    output_events,
                    sessions=sessions,
                    channel_id=channel_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    active_session_id=active_session_id,
                ),
            }
        )
    return rows


def _interval_payload(interval: StateInterval) -> dict[str, object]:
    """StateIntervalをWeb用JSONへ変換する。

    Args:
        interval: Heartbeat出力の周期（ms）。

    Returns:
        widget APIへ返す表示field/value mapping。
    """
    return {
        "start_ms": interval.start_ms,
        "end_ms": interval.end_ms,
        "state": interval.state,
        "duration_ms": (
            interval.duration_ms
            if interval.duration_ms is not None
            else interval.end_ms - interval.start_ms
        ),
    }


def _output_interval_payloads(
    output_events: Sequence[OutputEvent],
    *,
    sessions: Sequence[Session],
    channel_id: int,
    start_ms: int,
    end_ms: int,
    active_session_id: int | None,
) -> list[dict[str, object]]:
    """OUT eventから状態区間を作る。

    OUTは入力sessionのsnapshotを持たないため、 対象期間の直前に保存された
    出力変更を初期値として使う。 履歴が存在しない区間はUNKNOWNのままにする。

    Args:
        output_events: loggerが保存したOUT履歴。 入力eventとは別のsignalとして扱う。
        sessions: 監視session列。 異なるsessionをまたぐ状態継続を作らない。
        channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
        start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
        end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。
        active_session_id: 現在監視中session ID。
            明示されたsessionだけ現在時刻まで延長する。

    Returns:
        条件に一致したrowまたは表示値列。 該当なしは空list。
    """
    # ID順で隣接eventを確定してから、次eventまたはsession終端までの既知状態を作る。
    channel_events = sorted(
        (event for event in output_events if event.output_channel == channel_id),
        key=lambda event: event.id,
    )
    session_by_id = {session.id: session for session in sessions}
    known: list[dict[str, object]] = []
    for index, event in enumerate(channel_events):
        if event.ts_utc_ms >= end_ms:
            break
        next_event = (
            channel_events[index + 1] if index + 1 < len(channel_events) else None
        )
        segment_end = end_ms
        if next_event is not None:
            if _same_output_session(event, next_event):
                segment_end = next_event.ts_utc_ms
            else:
                segment_end = _output_session_end(
                    event,
                    session_by_id=session_by_id,
                    end_ms=end_ms,
                )
        elif active_session_id is not None and event.session_id == active_session_id:
            segment_end = end_ms
        else:
            segment_end = _output_session_end(
                event,
                session_by_id=session_by_id,
                end_ms=end_ms,
            )
        # 検索期間で切り詰めた既知区間を追加する。
        # session境界の空白はUNKNOWNとして保持する。
        segment_start = max(start_ms, event.ts_utc_ms)
        segment_end = min(end_ms, max(segment_end, event.ts_utc_ms))
        payload: dict[str, object] = {
            "start_ms": segment_start,
            "end_ms": segment_end,
            "state": "ON" if event.value else "OFF",
            "duration_ms": max(0, segment_end - segment_start),
            "source": event.source.value,
        }
        # 検索開始前の最後のeventも、同じsessionで期間内まで継続していれば
        # 初期状態として採用する。期間外eventを無条件に除外すると、検索開始
        # 直後がUNKNOWNになり、実際には継続していたOUT状態を失う。
        if segment_start < segment_end or (
            segment_start == segment_end and start_ms <= event.ts_utc_ms < end_ms
        ):
            known.append(payload)
        if next_event is not None and not _same_output_session(event, next_event):
            gap_start = max(start_ms, segment_end)
            gap_end = min(end_ms, next_event.ts_utc_ms)
            if gap_start < gap_end:
                known.append(
                    {
                        "start_ms": gap_start,
                        "end_ms": gap_end,
                        "state": "UNKNOWN",
                        "duration_ms": gap_end - gap_start,
                    }
                )
    return _fill_output_unknown_gaps(known, start_ms=start_ms, end_ms=end_ms)


def _same_output_session(first: OutputEvent, second: OutputEvent) -> bool:
    """連続するOUT履歴が同じ監視区間に属するか返す。

    Args:
        first: 比較する先行OUT event。
        second: 比較する後続OUT event。

    Returns:
        条件が成立している場合True、 未成立または未設定の場合False。
    """
    # session_id=NULLはdaemon起動時・停止中などsession境界を特定できない
    # 履歴であり、NULL同士を同一と扱うと別bootの状態を誤って連結する。
    return (
        first.session_id is not None
        and second.session_id is not None
        and first.session_id == second.session_id
    )


def _output_session_end(
    event: OutputEvent,
    *,
    session_by_id: dict[int, Session],
    end_ms: int,
) -> int:
    """異常終了したOUT sessionの既知終端を返す。

    Args:
        event: 処理するevent値。 DB書込み関数では保存前のwrite modelを受け取る。
        session_by_id: session IDからSession値を引くlookup mapping。
        end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。

    Returns:
        条件に従って算出した整数値。
    """
    if event.session_id is None:
        return event.ts_utc_ms
    session = session_by_id.get(event.session_id)
    if session is None or session.ended_at_ms is None:
        return event.ts_utc_ms
    return min(end_ms, max(event.ts_utc_ms, session.ended_at_ms))


def _fill_output_unknown_gaps(
    intervals: Sequence[dict[str, object]],
    *,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, object]]:
    """OUTの既知区間以外をUNKNOWNで埋める。

    Args:
        intervals: 状態区間列。 UNKNOWNは確定ON/OFF継続時間へ加算しない。
        start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
        end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。

    Returns:
        条件に一致したrowまたは表示値列。 該当なしは空list。
    """
    # 入力区間を時刻順に走査し、重複を切り詰めながら未観測の前後をUNKNOWNで補う。
    result: list[dict[str, object]] = []
    cursor = start_ms
    for interval in sorted(
        intervals,
        key=lambda value: (
            cast(int, value["start_ms"]),
            cast(int, value["end_ms"]),
        ),
    ):
        interval_start = cast(int, interval["start_ms"])
        interval_end = cast(int, interval["end_ms"])
        if interval_start > cursor:
            # 最初の履歴より前も観測根拠がない。途中の欠落と同じUNKNOWNで
            # 埋め、表示期間の先頭を暗黙の既知状態として省略しない。
            result.append(
                {
                    "start_ms": cursor,
                    "end_ms": interval_start,
                    "state": "UNKNOWN",
                    "duration_ms": interval_start - cursor,
                }
            )
        if interval_start < cursor:
            interval = dict(interval)
            interval["start_ms"] = cursor
            interval_start = cursor
        if interval_start == interval_end and start_ms <= interval_start < end_ms:
            result.append(interval)
            cursor = max(cursor, interval_start)
        elif interval_start < interval_end:
            result.append(interval)
            cursor = max(cursor, interval_end)
    if cursor < end_ms:
        result.append(
            {
                "start_ms": cursor,
                "end_ms": end_ms,
                "state": "UNKNOWN",
                "duration_ms": end_ms - cursor,
            }
        )
    return result


def _event_list_rows(
    widget: Widget,
    events: Sequence[InputEvent],
    output_events: Sequence[OutputEvent],
    *,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, object]]:
    """対象IN/OUTのイベント一覧を新しい順で返す。

    Args:
        widget: 表示・集計対象のWidget設定。 信号と期間はconfigで決まる。
        events: 入力event列。 event IDを保存順の基準とし、 EDGEとSNAPSHOTを区別する。
        output_events: loggerが保存したOUT履歴。 入力eventとは別のsignalとして扱う。
        start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
        end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。

    Returns:
        条件に一致したrowまたは表示値列。 該当なしは空list。

    Raises:
        ValueError: 「event_list limit must be an integer」に該当すると判定した場合。
    """
    # limitを検証してから、各signalの期間内eventを同じrow形式で集める。
    raw_limit = widget.config.get("limit", 10)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise ValueError("event_list limit must be an integer")
    rows: list[tuple[int, dict[str, object]]] = []
    for signal in _widget_signals(widget):
        if signal.startswith("IN"):
            channel_id = int(signal[2:])
            for event in events:
                if (
                    event.channel_id == channel_id
                    and start_ms <= event.ts_utc_ms < end_ms
                ):
                    rows.append(
                        (
                            event.ts_utc_ms,
                            {
                                "id": f"input:{event.id}",
                                "signal": signal,
                                "timestamp_ms": event.ts_utc_ms,
                                "value": "ON" if event.value else "OFF",
                                "kind": (
                                    "edge"
                                    if event.kind is EventKind.EDGE
                                    else "snapshot"
                                ),
                            },
                        )
                    )
        else:
            channel_id = int(signal[3:])
            for output_event in output_events:
                if (
                    output_event.output_channel == channel_id
                    and start_ms <= output_event.ts_utc_ms < end_ms
                ):
                    rows.append(
                        (
                            output_event.ts_utc_ms,
                            {
                                "id": f"output:{output_event.id}",
                                "signal": signal,
                                "timestamp_ms": output_event.ts_utc_ms,
                                "value": "ON" if output_event.value else "OFF",
                                "kind": "output",
                                "source": output_event.source.value,
                            },
                        )
                    )
    # 同一msではログ画面と同じrecord type・数値ID順にする。文字列比較では
    # input:9がinput:10より新しくなり、表示件数の境界で新しい記録を落とす。
    rows.sort(
        key=lambda row: (
            row[0],
            str(row[1]["id"]).partition(":")[0],
            int(str(row[1]["id"]).partition(":")[2]),
        ),
        reverse=True,
    )
    return [row[1] for row in rows[:raw_limit]]


def _event_count(
    widget: Widget,
    events: Iterable[InputEvent],
    *,
    start_ms: int,
    end_ms: int,
    carried_count: int,
    after_event_id: int | None,
    reset_count: int | None,
    period_count: int | None,
    stored_count: int | None,
) -> int:
    """event_countの3モードをevent ID基準で計算する。

    Args:
        widget: 表示・集計対象のWidget設定。 信号と期間はconfigで決まる。
        events: 入力event列。 event IDを保存順の基準とし、 EDGEとSNAPSHOTを区別する。
        start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
        end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。
        carried_count: 最新resetより後のedgeのうちretentionで削除済みの件数。
            残存event件数へ加算してcounter totalを維持する。
        after_event_id: このIDより大きいeventだけを数える排他的reset境界。
        reset_count: reset後countの事前計算値。 Noneならevent ID境界から算出する。
        period_count: 期間内edge件数の事前計算値。 Noneならevent列から算出する。
        stored_count: DB保存済みcounter値。 Noneならevent履歴へfallbackする。

    Returns:
        条件に一致するevent/row件数。 該当がない場合は0。

    Raises:
        ValueError: configのchannelsがlistでない、 または空listの場合。
            channelsの先頭要素が整数でない場合。
    """
    # mode別分岐の前にcount対象とedge方向を確定し、事前計算値があれば履歴走査を省く。
    channels = widget.config["channels"]
    if not isinstance(channels, list) or not channels:
        raise ValueError("event count channels are missing")
    channel_id = channels[0]
    if not isinstance(channel_id, int):
        raise ValueError("event count channel is invalid")
    edge_value = _edge_value(widget)
    mode = widget.config.get("count_mode", "period")
    event_list = list(events)
    if mode == "period":
        # 期間モードだけはtimestamp境界でevent列を絞り、期間内edge数を求める。
        if period_count is not None:
            return period_count
        event_list = [
            event for event in event_list if start_ms <= event.ts_utc_ms < end_ms
        ]
        return count_edges(event_list, channel_id=channel_id, value=edge_value)
    if mode == "reset":
        # resetモードは時刻ではなくevent ID境界を使い、削除済みedge carryも合算する。
        if reset_count is not None:
            return reset_count
        if after_event_id is None:
            return carried_count
        return carried_count + count_edges(
            event_list,
            channel_id=channel_id,
            value=edge_value,
            after_event_id=after_event_id,
        )
    # stored modeではDB値を優先し、それ以外のfallbackは残存履歴から同じ条件で数える。
    if mode == "stored" and stored_count is not None:
        return stored_count
    return count_edges(event_list, channel_id=channel_id, value=edge_value)


def _edge_value(widget: Widget) -> int:
    """rising/falling設定を入力値へ変換する。

    Args:
        widget: 表示・集計対象のWidget設定。 信号と期間はconfigで決まる。

    Returns:
        条件に従って算出した整数値。
    """
    return 1 if widget.config.get("edge", "rising") == "rising" else 0


def _last_edge(
    events: Iterable[InputEvent],
    channel_id: int,
    value: int,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> InputEvent | None:
    """指定channel・edgeの最新eventをevent ID順で返す。

    `ts_utc_ms`はNTP補正で逆行する可能性があるため、 同一DB内での発生順を
    表すevent IDを正とする。 表示用timestampと発生順を混同しない。

    Args:
        events: 入力event列。 event IDを保存順の基準とし、 EDGEとSNAPSHOTを区別する。
        channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
        value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。
        start_ms: 期間の開始時刻（UTC epoch ms、 包含境界）。
        end_ms: 期間の終了時刻（UTC epoch ms、 排他的境界）。

    Returns:
        条件に一致する最新InputEvent。 該当eventがなければNone。
    """
    matched = [
        event
        for event in events
        if event.channel_id == channel_id
        and event.value == value
        and event.kind is EventKind.EDGE
        and (start_ms is None or event.ts_utc_ms >= start_ms)
        and (end_ms is None or event.ts_utc_ms < end_ms)
    ]
    return max(matched, key=lambda event: event.id, default=None)


def _current_interval(
    intervals: Sequence[StateInterval], now_ms: int, channel_id: int
) -> StateInterval:
    """現在時刻を含むintervalを返し、なければUNKNOWN相当を返す。

    Args:
        intervals: 状態区間列。 UNKNOWNは確定ON/OFF継続時間へ加算しない。
        now_ms: 現在時刻（UTC epoch ms）。 期間やwall-clock表示の基準。
        channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。

    Returns:
        StateInterval型の結果。 戻り値の型に応じ、
            該当なしはNoneまたは空collectionとなる。
    """
    for interval in reversed(intervals):
        # build_state_intervalsは現在監視中の最後の区間をend_ms == now_ms
        # で閉じる。右端を含めて扱わないと、監視中でも必ずUNKNOWNになる。
        if interval.start_ms <= now_ms <= interval.end_ms:
            return interval
    return StateInterval(
        channel_id=channel_id,
        start_ms=now_ms,
        end_ms=now_ms + 1,
        state="UNKNOWN",
        session_id=None,
    )


def _elapsed_time(
    widget: Widget,
    events: Sequence[InputEvent],
    intervals: Sequence[StateInterval],
    *,
    channel_id: int,
    now_ms: int,
    now_mono_ns: int | None,
    active_session_id: int | None,
) -> int | None:
    """状態継続または最後のedgeからの経過時間を返す。

    Args:
        widget: 表示・集計対象のWidget設定。 信号と期間はconfigで決まる。
        events: 入力event列。 event IDを保存順の基準とし、 EDGEとSNAPSHOTを区別する。
        intervals: 状態区間列。 UNKNOWNは確定ON/OFF継続時間へ加算しない。
        channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
        now_ms: 現在時刻（UTC epoch ms）。 期間やwall-clock表示の基準。
        now_mono_ns: 現在monotonic timestamp（ns）。 active
            session内の経過時間計算に使う。
        active_session_id: 現在監視中session ID。
            明示されたsessionだけ現在時刻まで延長する。

    Returns:
        int | None。 該当rowまたは有効値がない場合はNone。
    """
    if widget.config.get("source", "state") == "event":
        event = _last_edge(events, channel_id, _edge_value(widget))
        return (
            _event_elapsed_ms(
                event,
                now_ms=now_ms,
                now_mono_ns=now_mono_ns,
                active_session_id=active_session_id,
            )
            if event
            else None
        )
    state = str(widget.config.get("state", "ON"))
    interval = _current_interval(intervals, now_ms, channel_id)
    if interval.state != state:
        return None
    if interval.duration_ms is not None:
        return interval.duration_ms
    return max(0, now_ms - interval.start_ms)


def _event_elapsed_ms(
    event: InputEvent,
    *,
    now_ms: int,
    now_mono_ns: int | None,
    active_session_id: int | None,
) -> int:
    """現sessionのevent経過時間をmonotonic優先で求める。

    Args:
        event: 処理するevent値。 DB書込み関数では保存前のwrite modelを受け取る。
        now_ms: 現在時刻（UTC epoch ms）。 期間やwall-clock表示の基準。
        now_mono_ns: 現在monotonic timestamp（ns）。 active
            session内の経過時間計算に使う。
        active_session_id: 現在監視中session ID。
            明示されたsessionだけ現在時刻まで延長する。

    Returns:
        経過時間または継続時間（ms）。 UNKNOWNは確定時間に含めない。
    """
    if (
        now_mono_ns is not None
        and active_session_id is not None
        and event.session_id == active_session_id
        and now_mono_ns >= event.mono_ns
    ):
        return (now_mono_ns - event.mono_ns) // 1_000_000
    return max(0, now_ms - event.ts_utc_ms)


__all__ = ["evaluate_widget"]
