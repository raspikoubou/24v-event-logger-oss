"""Webデモ用の固定入力イベント・表示項目の初期化処理。

実機のGPIOやloggerdには接続せず、渡されたWebContextのRepositoryに投入する。
既存の初期化条件とイベント日時・順序を維持する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from event_logger.common.models import EventKind
from event_logger.domain.widgets import (
    Widget,
    WidgetSize,
    WidgetType,
    normalize_widget_config,
)
from event_logger.version import VERSION

if TYPE_CHECKING:
    from event_logger.web.app import WebContext


def _seed_demo_data(context: WebContext) -> None:
    """固定fixtureを一度だけ投入する。"""
    if context.events.count_all() == 0:
        base_ms = context.clock.now_utc_ms() - 8 * 60 * 60 * 1000
        session = context.sessions.create(
            boot_id="demo-boot",
            started_at_ms=base_ms,
            app_version=f"{VERSION}-demo",
            clock_synced=True,
        )
        rows = [
            (base_ms, 1, 0, EventKind.SNAPSHOT),
            (base_ms + 30 * 60 * 1000, 1, 1, EventKind.EDGE),
            (base_ms + 2 * 60 * 60 * 1000, 1, 0, EventKind.EDGE),
            (base_ms + 3 * 60 * 60 * 1000, 2, 1, EventKind.SNAPSHOT),
            (base_ms + 3 * 60 * 60 * 1000 + 5_000, 2, 0, EventKind.EDGE),
            (base_ms + 5 * 60 * 60 * 1000, 3, 1, EventKind.SNAPSHOT),
        ]
        for timestamp_ms, channel_id, value, kind in rows:
            context.events.append(
                session_id=session.id,
                ts_utc_ms=timestamp_ms,
                mono_ns=(timestamp_ms - base_ms) * 1_000_000,
                channel_id=channel_id,
                value=value,
                kind=kind,
                clock_synced=True,
            )
        context.sessions.end(session.id, context.clock.now_utc_ms())
    if not context.widgets.list_all():
        base_ms = context.clock.now_utc_ms() - 8 * 60 * 60 * 1000
        context.widgets.create(
            Widget(
                id="widget_001",
                widget_type=WidgetType.CURRENT_STATE,
                title="運転状態（demo）",
                config=normalize_widget_config({"channels": [1], "show_elapsed": True}),
                size=WidgetSize.SMALL,
                sort_order=0,
                enabled=True,
                updated_at_ms=base_ms,
            )
        )
        context.widgets.create(
            Widget(
                id="widget_002",
                widget_type=WidgetType.EVENT_COUNT,
                title="立上り回数（demo）",
                config=normalize_widget_config(
                    {"channels": [1], "edge": "rising", "count_mode": "period"}
                ),
                size=WidgetSize.SMALL,
                sort_order=1,
                enabled=True,
                updated_at_ms=base_ms,
            )
        )


