"""永続化層とDomain層の間で使う不変モデル。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Literal


# 開始時のsnapshotと監視中の確定edgeを区別して保存する。
class EventKind(IntEnum):
    """入力イベントの種別。"""

    EDGE = 0
    SNAPSHOT = 1


# OUT操作の原因をHeartbeat・条件ルール・テストで識別する。
class OutputSource(StrEnum):
    """出力状態を変更したロガー内機能。"""

    RULE = "rule"
    HEARTBEAT = "heartbeat"
    TEST = "test"
    STARTUP = "startup"


InputState = Literal[0, 1]
DisplayState = Literal["ON", "OFF", "UNKNOWN"]
LogRecordType = Literal["input", "output"]
LogKind = Literal["edge", "snapshot", "output"]


@dataclass(frozen=True, slots=True)
# 監視の開始・終了を区切り、未監視区間の判定に使う。
class Session:
    """連続して入力監視できた区間を表す。"""

    id: int
    boot_id: str
    started_at_ms: int
    ended_at_ms: int | None
    app_version: str
    clock_synced: bool


@dataclass(frozen=True, slots=True)
# カウンタ値を集計し直す境界を表す。
class CounterReset:
    """ウィジェットのリセット基準とretention繰越値を表す。"""

    id: int
    widget_id: str
    reset_at_ms: int
    after_event_id: int
    carried_count: int

    def __post_init__(self) -> None:
        """counter resetの正値制約を検証する。"""
        if self.id < 1:
            raise ValueError("counter reset id must be positive")
        if not self.widget_id:
            raise ValueError("widget_id must not be empty")
        if self.after_event_id < 0 or self.carried_count < 0:
            raise ValueError("counter reset values must not be negative")


@dataclass(frozen=True, slots=True)
# 入力ごとの有効状態とデバウンス設定を保持する。
class ChannelConfig:
    """ユーザーが設定できる入力チャンネル情報を表す。"""

    id: int
    enabled: bool
    name: str
    debounce_ms: int
    on_label: str
    off_label: str
    updated_at_ms: int

    def __post_init__(self) -> None:
        """チャンネル設定のDB境界を検証する。"""
        if not 1 <= self.id <= 6:
            raise ValueError("channel id must be between 1 and 6")
        if not 0 <= self.debounce_ms <= 5000:
            raise ValueError("debounce_ms must be between 0 and 5000")


@dataclass(frozen=True, slots=True)
# OUT状態変化と操作元の情報を一緒に保持する。
class OutputEvent:
    """ロガー自身が変更したOUT状態履歴を表す。"""

    id: int
    session_id: int | None
    ts_utc_ms: int
    mono_ns: int
    output_channel: int
    value: InputState
    source: OutputSource
    clock_synced: bool

    def __post_init__(self) -> None:
        """出力履歴のDB境界を検証する。"""
        if self.id < 1:
            raise ValueError("output event id must be positive")
        if self.session_id is not None and self.session_id < 1:
            raise ValueError("session id must be positive when present")
        if not 1 <= self.output_channel <= 6:
            raise ValueError("output_channel must be between 1 and 6")
        if self.value not in (0, 1):
            raise ValueError("output event value must be 0 or 1")


@dataclass(frozen=True, slots=True)
# IN/OUTの履歴を同じ表示形式へまとめる。
class LogRecord:
    """IN/OUTを同じ時間軸へ並べたログ表示用レコードを表す。"""

    id: int
    record_type: LogRecordType
    ts_utc_ms: int
    mono_ns: int
    channel_id: int
    value: InputState
    kind: LogKind
    source: OutputSource | None
    clock_synced: bool

    def __post_init__(self) -> None:
        """統合ログが入力・出力どちらの制約も満たすことを確認する。"""
        if self.id < 1:
            raise ValueError("log record id must be positive")
        if self.record_type not in ("input", "output"):
            raise ValueError("record_type must be input or output")
        if not 1 <= self.channel_id <= 6:
            raise ValueError("channel_id must be between 1 and 6")
        if self.value not in (0, 1):
            raise ValueError("log record value must be 0 or 1")
        if self.kind not in ("edge", "snapshot", "output"):
            raise ValueError("unsupported log kind")
        if self.record_type == "input" and self.kind == "output":
            raise ValueError("input log record cannot have output kind")
        if self.record_type == "output":
            if self.kind != "output" or self.source is None:
                raise ValueError("output log record requires output kind and source")
        elif self.source is not None:
            raise ValueError("input log record cannot have output source")


@dataclass(frozen=True, slots=True)
# 入力値・取得時刻・時計同期状態を保持する。
class InputEvent:
    """1件の論理入力状態変化または監視開始snapshotを表す。"""

    id: int
    session_id: int
    ts_utc_ms: int
    mono_ns: int
    channel_id: int
    value: InputState
    kind: EventKind
    clock_synced: bool

    def __post_init__(self) -> None:
        """DBから復元した値がDomainの制約を満たすことを確認する。"""
        if self.id < 1:
            raise ValueError("event id must be positive")
        if self.session_id < 1:
            raise ValueError("session id must be positive")
        if not 1 <= self.channel_id <= 6:
            raise ValueError("channel_id must be between 1 and 6")
        if self.value not in (0, 1):
            raise ValueError("event value must be 0 or 1")
        if self.ts_utc_ms < 0 or self.mono_ns < 0:
            raise ValueError("event timestamps must not be negative")


@dataclass(frozen=True, slots=True)
# ON・OFFに加え、記録のないUNKNOWN区間も表現する。
class StateInterval:
    """ある入力状態が確実に観測できた期間を表す。"""

    channel_id: int
    start_ms: int
    end_ms: int
    state: DisplayState
    session_id: int | None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        """空区間や不正なチャンネルを受け付けない。"""
        if not 1 <= self.channel_id <= 6:
            raise ValueError("channel_id must be between 1 and 6")
        if self.start_ms >= self.end_ms:
            raise ValueError("state interval must have positive duration")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
