"""OUTの単一所有ルールと診断テストの状態機械。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OutputOwnerKind(StrEnum):
    """OUTを占有する初期版機能。"""

    UNUSED = "unused"
    HEARTBEAT = "heartbeat"
    RULE = "rule"


class OutputOwnershipError(ValueError):
    """同一OUTへの複数用途割当を拒否した。"""


@dataclass(frozen=True, slots=True)
class OutputOwner:
    """1点のOUTに割り当てられた用途。"""

    channel_id: int
    kind: OutputOwnerKind
    rule_id: int | None = None

    def __post_init__(self) -> None:
        """ownerの整合性を検証する。"""
        if not 1 <= self.channel_id <= 6:
            raise ValueError("output channel must be between 1 and 6")
        if self.kind is OutputOwnerKind.RULE and self.rule_id is None:
            raise ValueError("rule owner requires rule_id")
        if self.kind is not OutputOwnerKind.RULE and self.rule_id is not None:
            raise ValueError("only rule owner may have rule_id")


# 一つのOUTをHeartbeatと条件ルールで重複制御しないため、占有者を一元管理する。
class OutputOwnerRegistry:
    """OUTごとに未使用、Heartbeat、1条件ルールのいずれかを保持する。"""

    def __init__(self) -> None:
        """全OUTを未使用で初期化する。"""
        self._owners = {
            channel_id: OutputOwner(channel_id, OutputOwnerKind.UNUSED)
            for channel_id in range(1, 7)
        }

    def owner(self, channel_id: int) -> OutputOwner:
        """指定OUTのownerを返す。"""
        _validate_channel(channel_id)
        return self._owners[channel_id]

    def assign_heartbeat(self, channel_id: int) -> None:
        """指定OUTをHeartbeatへ割り当てる。"""
        self._assign(OutputOwner(channel_id, OutputOwnerKind.HEARTBEAT))

    def assign_rule(self, channel_id: int, rule_id: int) -> None:
        """指定OUTを1つの条件ルールへ割り当てる。"""
        if rule_id < 1:
            raise ValueError("rule_id must be positive")
        self._assign(OutputOwner(channel_id, OutputOwnerKind.RULE, rule_id))

    def release(self, channel_id: int) -> None:
        """指定OUTを未使用へ戻す。"""
        _validate_channel(channel_id)
        self._owners[channel_id] = OutputOwner(channel_id, OutputOwnerKind.UNUSED)

    # 別の機能が既に使用中のOUTは暗黙に上書きせず競合として拒否する。
    def _assign(self, owner: OutputOwner) -> None:
        """既存ownerがあれば用途の上書きを拒否する。"""
        current = self._owners[owner.channel_id]
        if current.kind is not OutputOwnerKind.UNUSED:
            raise OutputOwnershipError(
                f"OUT{owner.channel_id} is already owned by {current.kind}"
            )
        self._owners[owner.channel_id] = owner


@dataclass
# テストは通常運転とは別の一時的な所有形態を持ち、期限はmonotonic時刻で判定する。
class OutputTest:
    """メンテナンス中の最大5秒出力テスト。"""

    channel_id: int
    started_ns: int
    duration_ms: int

    def __post_init__(self) -> None:
        """診断テストの制約を検証する。"""
        _validate_channel(self.channel_id)
        if self.started_ns < 0:
            raise ValueError("started_ns must not be negative")
        if not 1 <= self.duration_ms <= 5_000:
            raise ValueError("output test duration must be between 1 and 5000ms")

    def is_expired(self, now_ns: int) -> bool:
        """指定時刻でテストが終了しているか返す。"""
        if now_ns < self.started_ns:
            raise ValueError("now_ns must not precede started_ns")
        return now_ns - self.started_ns >= self.duration_ms * 1_000_000


def _validate_channel(channel_id: int) -> None:
    """OUTチャンネル番号を検証する。"""
    if not 1 <= channel_id <= 6:
        raise ValueError("output channel must be between 1 and 6")
