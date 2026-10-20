"""ウィジェット評価の期間・snapshot・counter境界を固定する。"""

from typing import Any, cast

from event_logger.common.models import (
    EventKind,
    InputEvent,
    OutputEvent,
    OutputSource,
    Session,
)
from event_logger.domain.widget_values import evaluate_widget
from event_logger.domain.widgets import Widget, WidgetSize, WidgetType


def _widget(widget_type: WidgetType, config: dict[str, object]) -> Widget:
    return Widget(
        id="widget-1",
        widget_type=widget_type,
        title="確認用",
        config=config,
        size=WidgetSize.MEDIUM,
        sort_order=0,
        enabled=True,
        updated_at_ms=0,
    )


def _event(
    event_id: int, timestamp_ms: int, value: int, *, mono_ns: int | None = None
) -> InputEvent:
    return InputEvent(
        id=event_id,
        session_id=1,
        ts_utc_ms=timestamp_ms,
        mono_ns=mono_ns if mono_ns is not None else timestamp_ms * 1_000_000,
        channel_id=1,
        value=value,  # type: ignore[arg-type]
        kind=EventKind.EDGE,
        clock_synced=True,
    )


def _output_event(
    event_id: int, timestamp_ms: int, value: int
) -> OutputEvent:
    return OutputEvent(
        id=event_id,
        session_id=1,
        ts_utc_ms=timestamp_ms,
        mono_ns=timestamp_ms * 1_000_000,
        output_channel=1,
        value=value,  # type: ignore[arg-type]
        source=OutputSource.RULE,
        clock_synced=True,
    )


def _session(*, ended_at_ms: int | None = None) -> Session:
    return Session(
        id=1,
        boot_id="boot-1",
        started_at_ms=0,
        ended_at_ms=ended_at_ms,
        app_version="test",
        clock_synced=True,
    )


def test_current_state_uses_live_snapshot_and_monotonic_elapsed() -> None:
    result = evaluate_widget(
        _widget(WidgetType.CURRENT_STATE, {"channels": [1], "show_elapsed": True}),
        events=[_event(1, 10, 0)],
        sessions=[_session()],
        now_ms=20,
        start_ms=0,
        active_session_id=1,
        now_mono_ns=9_000_000,
        current_inputs={1: True},
        current_input_since_ns={1: 4_000_000},
    )

    assert result["state"] == "ON"
    assert result["elapsed_ms"] == 5


def test_disabled_channel_remains_unknown_even_with_live_snapshot() -> None:
    result = evaluate_widget(
        _widget(WidgetType.CURRENT_STATE, {"channels": [1]}),
        events=[],
        sessions=[],
        now_ms=20,
        start_ms=0,
        active_session_id=1,
        current_inputs={1: True},
        disabled_channel_ids=frozenset({1}),
    )

    assert result["state"] == "UNKNOWN"
    assert "elapsed_ms" not in result


def test_reset_count_uses_event_id_boundary_and_retention_carry() -> None:
    result = evaluate_widget(
        _widget(
            WidgetType.EVENT_COUNT,
            {"channels": [1], "count_mode": "reset", "edge": "rising"},
        ),
        # ID 8 has an earlier timestamp than the reset boundary, but is later in
        # persisted event order and must count. Deleted edges are represented by carry.
        events=[_event(8, 5, 1), _event(7, 15, 1)],
        sessions=[],
        now_ms=30,
        start_ms=0,
        active_session_id=None,
        after_event_id=7,
        carried_count=3,
    )

    assert result["value"] == 4


def test_reset_count_uses_saved_value_and_returns_reset_timestamp() -> None:
    result = evaluate_widget(
        _widget(
            WidgetType.EVENT_COUNT,
            {"channels": [1], "count_mode": "reset", "edge": "rising"},
        ),
        events=[],
        sessions=[],
        now_ms=30,
        start_ms=0,
        active_session_id=None,
        after_event_id=12,
        carried_count=4,
        reset_at_ms=23,
        reset_count=9,
    )

    assert result == {
        "widget_id": "widget-1",
        "type": "event_count",
        "title": "確認用",
        "size": "medium",
        "sort_order": 0,
        "value": 9,
        "unit": "回",
        "count_mode": "reset",
        "reset_available": True,
        "reset_at_ms": 23,
    }


def test_fixed_period_and_precomputed_period_count_keep_api_shape() -> None:
    result = evaluate_widget(
        _widget(
            WidgetType.EVENT_COUNT,
            {"channels": [1], "count_mode": "period", "edge": "rising"},
        ),
        events=[_event(1, 15, 1), _event(2, 25, 1)],
        sessions=[],
        now_ms=30,
        start_ms=0,
        fixed_period_range=(10, 20),
        active_session_id=None,
        period_count=6,
    )

    assert result == {
        "widget_id": "widget-1",
        "type": "event_count",
        "title": "確認用",
        "size": "medium",
        "sort_order": 0,
        "value": 6,
        "unit": "回",
        "count_mode": "period",
        "reset_available": False,
        "reset_at_ms": None,
    }


def test_period_count_uses_half_open_fixed_range() -> None:
    result = evaluate_widget(
        _widget(
            WidgetType.EVENT_COUNT,
            {"channels": [1], "count_mode": "period", "edge": "rising"},
        ),
        events=[_event(1, 10, 1), _event(2, 19, 1), _event(3, 20, 1)],
        sessions=[],
        now_ms=30,
        start_ms=0,
        fixed_period_range=(10, 20),
        active_session_id=None,
    )

    assert result["value"] == 2


def test_dashboard_following_and_fixed_periods_count_different_ranges() -> None:
    events = [
        _event(1, 10_000, 1),
        _event(2, 15_000, 1),
        _event(3, 20_000, 1),
        _event(4, 60_000, 1),
        _event(5, 70_000, 1),
        _event(6, 119_999, 1),
        _event(7, 120_000, 1),
    ]
    following = evaluate_widget(
        _widget(
            WidgetType.EVENT_COUNT,
            {"channels": [1], "count_mode": "period", "edge": "rising"},
        ),
        events=events,
        sessions=[],
        now_ms=120_000,
        start_ms=10_000,
        period_end_ms=20_000,
        active_session_id=None,
    )
    fixed = evaluate_widget(
        _widget(
            WidgetType.EVENT_COUNT,
            {
                "channels": [1],
                "count_mode": "period",
                "edge": "rising",
                "time_range": {"mode": "fixed", "duration_ms": 60_000},
            },
        ),
        events=events,
        sessions=[],
        now_ms=120_000,
        start_ms=10_000,
        period_end_ms=20_000,
        active_session_id=None,
    )

    assert following["value"] == 2
    assert fixed["value"] == 3


def test_all_widget_types_keep_expected_payloads() -> None:
    events = [_event(1, 0, 0), _event(2, 10, 1), _event(3, 20, 0)]
    sessions = [_session()]
    common = {
        "events": events,
        "sessions": sessions,
        "now_ms": 30,
        "start_ms": 0,
        "active_session_id": 1,
        "now_mono_ns": 30_000_000,
    }
    cases: tuple[
        tuple[WidgetType, dict[str, object], dict[str, object], dict[str, object]], ...
    ] = (
        (
            WidgetType.CURRENT_STATE,
            {"channels": [1], "show_elapsed": True},
            {
                "current_inputs": {1: False},
                "current_input_since_ns": {1: 20_000_000},
            },
            {
                "widget_id": "widget-1",
                "type": "current_state",
                "title": "確認用",
                "size": "medium",
                "sort_order": 0,
                "state": "OFF",
                "on_label": "ON",
                "off_label": "OFF",
                "elapsed_ms": 10,
            },
        ),
        (
            WidgetType.EVENT_COUNT,
            {"channels": [1], "count_mode": "period", "edge": "rising"},
            {},
            {
                "widget_id": "widget-1",
                "type": "event_count",
                "title": "確認用",
                "size": "medium",
                "sort_order": 0,
                "value": 1,
                "unit": "回",
                "count_mode": "period",
                "reset_available": False,
                "reset_at_ms": None,
            },
        ),
        (
            WidgetType.ON_OFF_DURATION,
            {"channels": [1], "state": "ON"},
            {},
            {
                "widget_id": "widget-1",
                "type": "on_off_duration",
                "title": "確認用",
                "size": "medium",
                "sort_order": 0,
                "state": "ON",
                "duration_ms": 10,
            },
        ),
        (
            WidgetType.LAST_EVENT,
            {"channels": [1], "edge": "rising"},
            {},
            {
                "widget_id": "widget-1",
                "type": "last_event",
                "title": "確認用",
                "size": "medium",
                "sort_order": 0,
                "timestamp_ms": 10,
                "elapsed_ms": 20,
            },
        ),
        (
            WidgetType.ELAPSED_TIME,
            {"channels": [1], "source": "state", "state": "OFF"},
            {},
            {
                "widget_id": "widget-1",
                "type": "elapsed_time",
                "title": "確認用",
                "size": "medium",
                "sort_order": 0,
                "elapsed_ms": 10,
            },
        ),
        (
            WidgetType.ELAPSED_TIME,
            {"channels": [1], "source": "event", "edge": "rising"},
            {},
            {
                "widget_id": "widget-1",
                "type": "elapsed_time",
                "title": "確認用",
                "size": "medium",
                "sort_order": 0,
                "elapsed_ms": 20,
            },
        ),
        (
            WidgetType.TIMELINE,
            {"signals": ["IN1", "OUT1"]},
            {
                "output_events": [
                    _output_event(1, 5, 1),
                    _output_event(2, 25, 0),
                ]
            },
            {
                "widget_id": "widget-1",
                "type": "timeline",
                "title": "確認用",
                "size": "medium",
                "sort_order": 0,
                "channels": [
                    {
                        "signal": "IN1",
                        "channel_id": 1,
                        "intervals": [
                            {
                                "start_ms": 0,
                                "end_ms": 10,
                                "state": "OFF",
                                "duration_ms": 10,
                            },
                            {
                                "start_ms": 10,
                                "end_ms": 20,
                                "state": "ON",
                                "duration_ms": 10,
                            },
                            {
                                "start_ms": 20,
                                "end_ms": 30,
                                "state": "OFF",
                                "duration_ms": 10,
                            },
                        ],
                    },
                    {
                        "signal": "OUT1",
                        "channel_id": 1,
                        "intervals": [
                            {
                                "start_ms": 0,
                                "end_ms": 5,
                                "state": "UNKNOWN",
                                "duration_ms": 5,
                            },
                            {
                                "start_ms": 5,
                                "end_ms": 25,
                                "state": "ON",
                                "duration_ms": 20,
                                "source": "rule",
                            },
                            {
                                "start_ms": 25,
                                "end_ms": 30,
                                "state": "OFF",
                                "duration_ms": 5,
                                "source": "rule",
                            },
                        ],
                    },
                ],
            },
        ),
        (
            WidgetType.EVENT_LIST,
            {"signals": ["IN1", "OUT1"], "limit": 10},
            {
                "output_events": [
                    _output_event(1, 5, 1),
                    _output_event(2, 25, 0),
                ]
            },
            {
                "widget_id": "widget-1",
                "type": "event_list",
                "title": "確認用",
                "size": "medium",
                "sort_order": 0,
                "events": [
                    {
                        "id": "output:2",
                        "signal": "OUT1",
                        "timestamp_ms": 25,
                        "value": "OFF",
                        "kind": "output",
                        "source": "rule",
                    },
                    {
                        "id": "input:3",
                        "signal": "IN1",
                        "timestamp_ms": 20,
                        "value": "OFF",
                        "kind": "edge",
                    },
                    {
                        "id": "input:2",
                        "signal": "IN1",
                        "timestamp_ms": 10,
                        "value": "ON",
                        "kind": "edge",
                    },
                    {
                        "id": "output:1",
                        "signal": "OUT1",
                        "timestamp_ms": 5,
                        "value": "ON",
                        "kind": "output",
                        "source": "rule",
                    },
                    {
                        "id": "input:1",
                        "signal": "IN1",
                        "timestamp_ms": 0,
                        "value": "OFF",
                        "kind": "edge",
                    },
                ],
            },
        ),
    )

    for widget_type, config, extra, expected in cases:
        result = evaluate_widget(
            _widget(widget_type, config), **cast(Any, common | extra)
        )

        assert result == expected


def test_realtime_state_snapshot_is_not_limited_by_historical_period() -> None:
    result = evaluate_widget(
        _widget(WidgetType.CURRENT_STATE, {"channels": [1]}),
        events=[_event(1, 15, 0)],
        sessions=[_session()],
        now_ms=30,
        start_ms=0,
        fixed_period_range=(5, 10),
        active_session_id=1,
        current_inputs={1: True},
    )

    assert result["state"] == "ON"


def test_inactive_snapshot_falls_back_to_saved_history_or_unknown() -> None:
    widget = _widget(
        WidgetType.CURRENT_STATE,
        {"channels": [1], "show_elapsed": True},
    )
    history_result = evaluate_widget(
        widget,
        events=[_event(1, 0, 0), _event(2, 10, 1), _event(3, 20, 0)],
        sessions=[_session(ended_at_ms=30)],
        now_ms=30,
        start_ms=0,
        active_session_id=None,
        now_mono_ns=30_000_000,
        current_inputs={1: True},
        current_input_since_ns={1: 20_000_000},
    )
    unknown_result = evaluate_widget(
        widget,
        events=[],
        sessions=[],
        now_ms=30,
        start_ms=0,
        active_session_id=None,
        current_inputs={1: True},
        current_input_since_ns={1: 20_000_000},
    )

    assert history_result["state"] == "OFF"
    assert history_result["elapsed_ms"] == 10
    assert unknown_result["state"] == "UNKNOWN"
    assert "elapsed_ms" not in unknown_result


def test_unobserved_timeline_period_is_unknown() -> None:
    result = evaluate_widget(
        _widget(WidgetType.TIMELINE, {"signals": ["IN1"]}),
        events=[],
        sessions=[],
        now_ms=30,
        start_ms=10,
        active_session_id=None,
    )

    channels = cast(list[dict[str, object]], result["channels"])
    intervals = cast(list[dict[str, object]], channels[0]["intervals"])
    assert intervals == [
        {"start_ms": 10, "end_ms": 30, "state": "UNKNOWN", "duration_ms": 20}
    ]
