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
    リセット基準であり、 壁時計の時刻を代わりに使ってはいけない。

    Args:
        events: 入力event列。 event IDを保存順の基準とし、 EDGEとSNAPSHOTを区別する。
        channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
        value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。
        after_event_id: このIDより大きいeventだけを数える排他的reset境界。

    Returns:
        条件に一致するevent/row件数。 該当がない場合は0。

    Raises:
        ValueError: channel_idが1〜6の範囲外の場合。 論理値が0または1以外の場合。
            after_event_idが負の場合。
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
