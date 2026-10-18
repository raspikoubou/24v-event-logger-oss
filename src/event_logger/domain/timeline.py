"""入力イベントからON/OFF/UNKNOWN区間を構築する。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from event_logger.common.models import (
    DisplayState,
    InputEvent,
    Session,
    StateInterval,
)


# セッション境界をまたいで直前状態を引き継がない。記録のない区間はUNKNOWNとして扱う。
def build_state_intervals(
    events: Iterable[InputEvent],
    sessions: Sequence[Session],
    *,
    channel_id: int,
    start_ms: int,
    end_ms: int,
    active_session_id: int | None = None,
    end_mono_ns: int | None = None,
) -> list[StateInterval]:
    """指定期間の確定状態を返す。

    session間、入力無効区間、異常終了したsessionの最後の保存event以降は
    UNKNOWNとする。前後の状態をつないで監視できなかった時間を補間しない。
    現在監視中のsessionだけは `active_session_id` として明示された場合に限り
    `end_ms` まで状態を延長する。
    """
    if not 1 <= channel_id <= 6:
        raise ValueError("channel_id must be between 1 and 6")
    if start_ms >= end_ms:
        raise ValueError("start_ms must be before end_ms")

    grouped_events: dict[int, list[InputEvent]] = defaultdict(list)
    for event in events:
        if event.channel_id == channel_id:
            grouped_events[event.session_id].append(event)
    for session_events in grouped_events.values():
        # wall clockはNTP補正で逆行するため、同一session内の発生順は
        # events.idを正とする。timestampは表示軸としてのみ利用する。
        session_events.sort(key=lambda event: event.id)

    intervals: list[StateInterval] = []
    for session in sorted(sessions, key=lambda item: item.id):
        session_events = grouped_events.get(session.id, [])
        session_end = _known_session_end(
            session,
            session_events,
            end_ms=end_ms,
            active_session_id=active_session_id,
        )
        if session_end <= start_ms or session.started_at_ms >= end_ms:
            continue
        session_start = max(start_ms, session.started_at_ms)
        session_limit = min(end_ms, session_end)
        if session_start >= session_limit:
            continue
        intervals.extend(
            _build_session_intervals(
                session_events,
                session=session,
                start_ms=session_start,
                end_ms=session_limit,
                end_mono_ns=(end_mono_ns if session.id == active_session_id else None),
            )
        )

    return _fill_unknown_gaps(
        intervals,
        channel_id=channel_id,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def total_duration_ms(
    intervals: Iterable[StateInterval],
    *,
    state: DisplayState,
) -> int:
    """指定状態の確定区間だけを合計する。UNKNOWNは加算しない。"""
    if state == "UNKNOWN":
        raise ValueError("UNKNOWN duration is not a confirmed ON/OFF duration")
    return sum(
        interval.duration_ms
        if interval.duration_ms is not None
        else interval.end_ms - interval.start_ms
        for interval in intervals
        if interval.state == state
    )


# 終了記録のないセッションは観測可能なイベントと現行セッション情報で上限を決める。
def _known_session_end(
    session: Session,
    session_events: Sequence[InputEvent],
    *,
    end_ms: int,
    active_session_id: int | None,
) -> int:
    """sessionの状態を確定できるwall clock上の終端を返す。"""
    if session.id == active_session_id:
        return end_ms
    if session.ended_at_ms is not None:
        return min(session.ended_at_ms, end_ms)
    if not session_events:
        return session.started_at_ms
    # 異常終了sessionでは最後の保存event以降をUNKNOWNにする。ただし壁時計が
    # NTP補正で逆行している場合、event ID順の最後が最小timestampになり得るため、
    # 表示軸上では保存済みeventをすべて含める終端を使う。
    return min(
        max(session.started_at_ms, max(event.ts_utc_ms for event in session_events)),
        end_ms,
    )


# UTC時刻が補正されても、同一セッション内の長さはmonotonic時計を優先する。
def _build_session_intervals(
    session_events: Sequence[InputEvent],
    *,
    session: Session,
    start_ms: int,
    end_ms: int,
    end_mono_ns: int | None,
) -> list[StateInterval]:
    """1 session内のON/OFF区間を作る。"""
    state: DisplayState | None = None
    cursor_ms = start_ms
    state_started_mono_ns: int | None = None
    effective_previous_ms = session.started_at_ms
    raw_previous_ms = session.started_at_ms
    previous_mono_ns: int | None = None
    clock_adjusted = False
    result: list[StateInterval] = []
    for event in session_events:
        # 表示上のtimestampは時計補正で逆行し得る。状態遷移の順番はevent ID
        # で維持し、timeline上の座標だけを単調に進める。
        wall_delta_ms = event.ts_utc_ms - raw_previous_ms
        if previous_mono_ns is not None:
            mono_delta_ms = (event.mono_ns - previous_mono_ns) // 1_000_000
            # 長時間状態が続いただけでは時計補正とみなさない。wall clockと
            # monotonicの差が実際に食い違った場合だけ、同一sessionの時間差を
            # monotonicへ切り替える。
            if mono_delta_ms < 0 or (
                mono_delta_ms >= 1_000 and abs(wall_delta_ms - mono_delta_ms) > 1_000
            ):
                clock_adjusted = True
        elif wall_delta_ms < 0:
            clock_adjusted = True
        raw_previous_ms = event.ts_utc_ms
        previous_mono_ns = event.mono_ns
        effective_event_ms = max(event.ts_utc_ms, effective_previous_ms)
        effective_previous_ms = effective_event_ms
        if effective_event_ms <= start_ms and state is None:
            state = "ON" if event.value else "OFF"
            # 検索範囲の開始点とeventが一致する場合だけ、範囲内の経過時間を
            # monotonicで直接測れる。範囲外から継続した状態は、開始点時点の
            # monotonic値を保存していないため表示軸のwall時間で切り取る。
            state_started_mono_ns = (
                event.mono_ns if effective_event_ms == start_ms else None
            )
            continue
        if effective_event_ms <= start_ms:
            if not clock_adjusted:
                # 範囲開始前のeventは開始時点の状態を決めるためだけに使う。
                # 後発eventをstart+1へ移すと、既に終了した状態を期間内へ
                # 1ms加算してしまう。
                state = "ON" if event.value else "OFF"
                state_started_mono_ns = None
                continue
            # 時計が逆行した場合はevent ID順の状態遷移を捨てず、表示上の
            # 座標だけを範囲開始直後へ寄せる。
            effective_event_ms = min(start_ms + 1, end_ms - 1)
        if effective_event_ms > end_ms:
            break
        if state is not None:
            display_end_ms = max(effective_event_ms, cursor_ms + 1)
            display_end_ms = min(display_end_ms, end_ms)
            fallback_ms = max(0, display_end_ms - cursor_ms)
            monotonic_available = state_started_mono_ns is not None
            duration_ms = (
                _duration_with_monotonic(
                    state_started_mono_ns,
                    event.mono_ns,
                    fallback_ms=fallback_ms,
                )
                if monotonic_available
                else fallback_ms
            )
            result.append(
                StateInterval(
                    channel_id=event.channel_id,
                    start_ms=cursor_ms,
                    end_ms=display_end_ms,
                    state=state,
                    session_id=session.id,
                    duration_ms=duration_ms if monotonic_available else None,
                )
            )
        if effective_event_ms == end_ms:
            # 異常終了sessionの終端は最後の保存event時刻である。このedgeは
            # 直前状態の区間を閉じるために使い、edge後はUNKNOWNのままにする。
            state = None
            cursor_ms = end_ms
            break
        state = "ON" if event.value else "OFF"
        cursor_ms = min(max(effective_event_ms, cursor_ms), end_ms - 1)
        state_started_mono_ns = event.mono_ns
    tail_monotonic_available = (
        state_started_mono_ns is not None and end_mono_ns is not None
    )
    if state is not None and cursor_ms < end_ms:
        fallback_ms = max(0, end_ms - cursor_ms)
        final_duration_ms = (
            _duration_with_monotonic(
                state_started_mono_ns,
                end_mono_ns if end_mono_ns is not None else 0,
                fallback_ms=fallback_ms,
            )
            if tail_monotonic_available
            else fallback_ms
        )
        result.append(
            StateInterval(
                channel_id=session_events[0].channel_id,
                start_ms=cursor_ms,
                end_ms=end_ms,
                state=state,
                session_id=session.id,
                duration_ms=(final_duration_ms if tail_monotonic_available else None),
            )
        )
    return result


def _monotonic_duration_ms(
    start_ns: int | None,
    end_ns: int,
    *,
    fallback_ms: int,
) -> int:
    """同一session内の状態継続時間をmonotonic優先で求める。"""
    if start_ns is None or end_ns < start_ns:
        return fallback_ms
    return max(0, (end_ns - start_ns) // 1_000_000)


# 表示用の時刻差と積算用の継続時間は別に扱う。
def _duration_with_monotonic(
    start_ns: int | None,
    end_ns: int,
    *,
    fallback_ms: int,
) -> int:
    """monotonic値が実時間として妥当な場合だけ区間へ適用する。"""
    if start_ns is None or end_ns < start_ns:
        return fallback_ms
    monotonic_ms = _monotonic_duration_ms(
        start_ns,
        end_ns,
        fallback_ms=fallback_ms,
    )
    # 実装上のeventは同じClockから得たmonotonicを保存するが、旧fixtureや
    # 不正な保存値が桁違いの場合まで採用すると表示時間を壊す。100ms未満の
    # 小さな候補はwall時間との差が大きければ表示軸へ戻し、明確なNTP補正
    # （秒単位以上の差）はmonotonicを優先する。
    if abs(monotonic_ms - fallback_ms) <= 100:
        return monotonic_ms
    if monotonic_ms >= 100:
        return monotonic_ms
    return fallback_ms


# 確認できない時間帯を前後の状態で埋めず、明示的にUNKNOWN区間として返す。
def _fill_unknown_gaps(
    intervals: Sequence[StateInterval],
    *,
    channel_id: int,
    start_ms: int,
    end_ms: int,
) -> list[StateInterval]:
    """確定区間の間をUNKNOWNで埋め、隣接同状態をまとめる。"""
    result: list[StateInterval] = []
    cursor_ms = start_ms
    for interval in sorted(intervals, key=lambda item: (item.start_ms, item.end_ms)):
        if interval.start_ms > cursor_ms:
            _append_interval(
                result,
                StateInterval(
                    channel_id=channel_id,
                    start_ms=cursor_ms,
                    end_ms=interval.start_ms,
                    state="UNKNOWN",
                    session_id=None,
                ),
            )
        if interval.start_ms < cursor_ms:
            interval = StateInterval(
                channel_id=interval.channel_id,
                start_ms=cursor_ms,
                end_ms=interval.end_ms,
                state=interval.state,
                session_id=interval.session_id,
                duration_ms=interval.duration_ms,
            )
        if interval.start_ms < interval.end_ms:
            _append_interval(result, interval)
            cursor_ms = max(cursor_ms, interval.end_ms)
    if cursor_ms < end_ms:
        _append_interval(
            result,
            StateInterval(
                channel_id=channel_id,
                start_ms=cursor_ms,
                end_ms=end_ms,
                state="UNKNOWN",
                session_id=None,
            ),
        )
    return result


def _append_interval(intervals: list[StateInterval], interval: StateInterval) -> None:
    """同じ状態の隣接区間を結合する。"""
    if intervals:
        previous = intervals[-1]
        if (
            previous.end_ms == interval.start_ms
            and previous.state == interval.state
            and previous.session_id == interval.session_id
        ):
            intervals[-1] = StateInterval(
                channel_id=interval.channel_id,
                start_ms=previous.start_ms,
                end_ms=interval.end_ms,
                state=interval.state,
                session_id=interval.session_id,
                duration_ms=_merged_duration(previous, interval),
            )
            return
    intervals.append(interval)


def _merged_duration(
    previous: StateInterval,
    current: StateInterval,
) -> int | None:
    """隣接区間を結合したときのmonotonic durationを合算する。"""
    if previous.duration_ms is None or current.duration_ms is None:
        return None
    return previous.duration_ms + current.duration_ms
