"""SQLite Repository互換ファサード。

実装は用途別モジュールに分割し、既存importパスを維持する。
"""

from __future__ import annotations

from event_logger.database.channel_repository import (
    ChannelRepository,
)
from event_logger.database.event_repository import (
    EventBatchRepository,
    EventRepository,
    EventWrite,
    InputEventWrite,
    OutputEventWrite,
)
from event_logger.database.log_repository import (
    LogRepository,
    OutputEventRepository,
)
from event_logger.database.repository_helpers import (
    _build_log_query,
    _channel_clause,
    _channel_from_row,
    _counter_config,
    _counter_event_matches,
    _counter_reset_from_row,
    _delete_ids,
    _event_from_row,
    _input_state,
    _lastrowid,
    _log_record_from_row,
    _mapping_int,
    _mapping_text,
    _output_event_from_row,
    _output_rule_config,
    _output_rule_from_row,
    _split_signals,
    _validate_channel_ids,
    _validate_channel_text,
    _widget_from_row,
)
from event_logger.database.retention_repository import (
    CounterResetRepository,
    RetentionRepository,
)
from event_logger.database.session_repository import (
    SessionRepository,
)
from event_logger.database.settings_repository import (
    AuditLogRepository,
    SettingsRepository,
)
from event_logger.database.widget_repository import (
    OutputRuleRepository,
    WidgetRepository,
)

__all__ = [
    "AuditLogRepository",
    "ChannelRepository",
    "CounterResetRepository",
    "EventBatchRepository",
    "EventRepository",
    "EventWrite",
    "InputEventWrite",
    "LogRepository",
    "OutputEventRepository",
    "OutputEventWrite",
    "OutputRuleRepository",
    "RetentionRepository",
    "SessionRepository",
    "SettingsRepository",
    "WidgetRepository",
    "_build_log_query",
    "_channel_clause",
    "_channel_from_row",
    "_counter_config",
    "_counter_event_matches",
    "_counter_reset_from_row",
    "_delete_ids",
    "_event_from_row",
    "_input_state",
    "_lastrowid",
    "_log_record_from_row",
    "_mapping_int",
    "_mapping_text",
    "_output_event_from_row",
    "_output_rule_config",
    "_output_rule_from_row",
    "_split_signals",
    "_validate_channel_ids",
    "_validate_channel_text",
    "_widget_from_row",
]
