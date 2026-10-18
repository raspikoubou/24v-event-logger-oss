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
        """ownerの整合性を検証する。

        Args:
            なし。

        Returns:
            None。 制約を検証するだけで、 永続状態や入力値は変更しない。

        Raises:
            ValueError: output channelが1〜6の範囲外の場合。 「rule owner requires
                rule_id」に該当すると判定した場合。 「only rule owner may have
                rule_id」に該当すると判定した場合。
        """
        if not 1 <= self.channel_id <= 6:
            raise ValueError("output channel must be between 1 and 6")
        if self.kind is OutputOwnerKind.RULE and self.rule_id is None:
            raise ValueError("rule owner requires rule_id")
        if self.kind is not OutputOwnerKind.RULE and self.rule_id is not None:
            raise ValueError("only rule owner may have rule_id")


class OutputOwnerRegistry:
    """OUTごとに未使用、Heartbeat、1条件ルールのいずれかを保持する。"""

    def __init__(self) -> None:
        """全OUTを未使用で初期化する。

        Args:
            なし。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._owners = {
            channel_id: OutputOwner(channel_id, OutputOwnerKind.UNUSED)
            for channel_id in range(1, 7)
        }

    def owner(self, channel_id: int) -> OutputOwner:
        """指定OUTのownerを返す。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。

        Returns:
            OutputOwner型の結果。 戻り値の型に応じ、
                該当なしはNoneまたは空collectionとなる。
        """
        _validate_channel(channel_id)
        return self._owners[channel_id]

    def assign_heartbeat(self, channel_id: int) -> None:
        """指定OUTをHeartbeatへ割り当てる。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。

        Returns:
            None。 対象device、 時計、 registry、 または状態機械の内部状態を更新する。
        """
        self._assign(OutputOwner(channel_id, OutputOwnerKind.HEARTBEAT))

    def assign_rule(self, channel_id: int, rule_id: int) -> None:
        """指定OUTを1つの条件ルールへ割り当てる。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
            rule_id: 条件ruleの永続ID。 1以上の整数を指定する。

        Returns:
            None。 対象device、 時計、 registry、 または状態機械の内部状態を更新する。

        Raises:
            ValueError: rule_idが0以下の場合。
        """
        if rule_id < 1:
            raise ValueError("rule_id must be positive")
        self._assign(OutputOwner(channel_id, OutputOwnerKind.RULE, rule_id))

    def release(self, channel_id: int) -> None:
        """指定OUTを未使用へ戻す。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。

        Returns:
            None。 対象device、 時計、 registry、 または状態機械の内部状態を更新する。
        """
        _validate_channel(channel_id)
        self._owners[channel_id] = OutputOwner(channel_id, OutputOwnerKind.UNUSED)

    def _assign(self, owner: OutputOwner) -> None:
        """既存ownerがあれば用途の上書きを拒否する。

        Args:
            owner: OUTへ割り当てる単一のOutputOwner値。

        Returns:
            None。 未使用OUTのownerだけを設定し、 既存割当は上書きしない。

        Raises:
            OutputOwnershipError: deviceまたはSQLite操作で失敗が返された場合。
        """
        current = self._owners[owner.channel_id]
        if current.kind is not OutputOwnerKind.UNUSED:
            raise OutputOwnershipError(
                f"OUT{owner.channel_id} is already owned by {current.kind}"
            )
        self._owners[owner.channel_id] = owner


@dataclass
class OutputTest:
    """メンテナンス中の最大5秒出力テスト。"""

    channel_id: int
    started_ns: int
    duration_ms: int

    def __post_init__(self) -> None:
        """診断テストの制約を検証する。

        Args:
            なし。

        Returns:
            None。 制約を検証するだけで、 永続状態や入力値は変更しない。

        Raises:
            ValueError: started_nsが負の場合。 output test
                durationが1〜5000msの範囲外の場合。
        """
        _validate_channel(self.channel_id)
        if self.started_ns < 0:
            raise ValueError("started_ns must not be negative")
        if not 1 <= self.duration_ms <= 5_000:
            raise ValueError("output test duration must be between 1 and 5000ms")

    def is_expired(self, now_ns: int) -> bool:
        """指定時刻でテストが終了しているか返す。

        Args:
            now_ns: 判定時monotonic timestamp（ns）。 経過時間計算に使う。

        Returns:
            条件が成立している場合True、 未成立または未設定の場合False。

        Raises:
            ValueError: 「now_ns must not precede started_ns」に該当すると判定した場合。
        """
        if now_ns < self.started_ns:
            raise ValueError("now_ns must not precede started_ns")
        return now_ns - self.started_ns >= self.duration_ms * 1_000_000


def _validate_channel(channel_id: int) -> None:
    """OUTチャンネル番号を検証する。

    Args:
        channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。

    Returns:
        None。 制約を検証するだけで、 永続状態や入力値は変更しない。

    Raises:
        ValueError: output channelが1〜6の範囲外の場合。
    """
    if not 1 <= channel_id <= 6:
        raise ValueError("output channel must be between 1 and 6")
