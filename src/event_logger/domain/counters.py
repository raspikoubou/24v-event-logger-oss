"""生イベントから回数表示を計算するDomain関数。"""

from __future__ import annotations

from collections.abc import Iterable

from event_logger.common.models import EventKind, InputEvent


def count_edges(
    events: Iterable[InputEvent],
    *,
    channel_id: int,
    value: int,
    after_event_id: int | None = None,
) -> int:
    """指定入力のedgeイベントを数える。

    `after_event_id` は時計補正や同一ミリ秒のイベントに影響されない
    リセット基準であり、壁時計の時刻を代わりに使ってはいけない。
    """
    if not 1 <= channel_id <= 6:
        raise ValueError("channel_id must be between 1 and 6")
    if value not in (0, 1):
        raise ValueError("value must be 0 or 1")
    if after_event_id is not None and after_event_id < 0:
        raise ValueError("after_event_id must not be negative")
    return sum(
        1
        for event in events
        if event.channel_id == channel_id
        and event.value == value
        and event.kind is EventKind.EDGE
        and (after_event_id is None or event.id > after_event_id)
    )
