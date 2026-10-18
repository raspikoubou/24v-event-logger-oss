"""仕様で定義した2種類の簡易出力条件を評価する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class RuleType(StrEnum):
    """初期版で許可する条件種別。"""

    STATE_DURATION = "state_duration"
    EVENT_ABSENCE = "event_absence"


class EdgeDirection(StrEnum):
    """event_absenceが監視するedge方向。"""

    RISING = "rising"
    FALLING = "falling"


@dataclass(frozen=True, slots=True)
class ConditionRule:
    """1つのIN条件と1つのOUT通知の対応を表す。"""

    id: int
    input_channel: int
    output_channel: int
    rule_type: RuleType
    duration_ms: int
    enabled: bool = True
    expected_value: bool = False
    edge: EdgeDirection = EdgeDirection.RISING

    def __post_init__(self) -> None:
        """ルールビルダーから来る値を検証する。

        Args:
            なし。

        Returns:
            None。 制約を検証するだけで、 永続状態や入力値は変更しない。

        Raises:
            ValueError: rule idが0以下の場合。 「unsupported rule
                type」に該当すると判定した場合。 「unsupported edge
                direction」に該当すると判定した場合。 input
                channelが1〜6の範囲外の場合。 output channelが1〜6の範囲外の場合。 rule
                durationが0以下の場合。 boolean設定値がbool型でない場合。
        """
        if self.id < 1:
            raise ValueError("rule id must be positive")
        if not isinstance(self.rule_type, RuleType):
            raise ValueError("unsupported rule type")
        if not isinstance(self.edge, EdgeDirection):
            raise ValueError("unsupported edge direction")
        if not 1 <= self.input_channel <= 6:
            raise ValueError("input channel must be between 1 and 6")
        if not 1 <= self.output_channel <= 6:
            raise ValueError("output channel must be between 1 and 6")
        if self.duration_ms < 1:
            raise ValueError("rule duration must be positive")
        if self.rule_type is RuleType.STATE_DURATION and not isinstance(
            self.expected_value, bool
        ):
            raise ValueError("state_duration expected_value must be boolean")


@dataclass(frozen=True, slots=True)
class OutputRule:
    """表示名を含むDB保存済み出力ルール。"""

    id: int
    name: str
    condition: ConditionRule
    updated_at_ms: int

    def __post_init__(self) -> None:
        """ルール名と更新時刻を検証する。

        Args:
            なし。

        Returns:
            None。 制約を検証するだけで、 永続状態や入力値は変更しない。

        Raises:
            ValueError: rule idが0以下の場合。 rule nameが空の場合。
                updated_at_msが負の場合。
        """
        if self.id < 1:
            raise ValueError("rule id must be positive")
        if not self.name.strip():
            raise ValueError("rule name must not be empty")
        if self.updated_at_ms < 0:
            raise ValueError("updated_at_ms must not be negative")


class RuleEvaluator:
    """sessionごとの入力観測だけを使って条件成立を判定する。"""

    def __init__(self) -> None:
        """未開始のevaluatorを作る。

        Args:
            なし。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._states: dict[int, bool] = {}
        self._state_since_ns: dict[int, int] = {}
        self._last_observed_ns: dict[int, int] = {}
        self._last_matching_event_ns: dict[int, dict[EdgeDirection, int]] = {}

    def start_session(self, input_values: Mapping[int, bool], now_ns: int) -> None:
        """新sessionのsnapshotを基準に全タイマーを初期化する。

        Args:
            input_values: IN番号から現在の論理状態へのsnapshot mapping。
            now_ns: 判定時monotonic timestamp（ns）。 経過時間計算に使う。

        Returns:
            None。 旧sessionの状態/タイマーを破棄し、
                snapshot値とnow_nsを新しい全タイマーの起点にする。
        """
        _validate_now(now_ns)
        self._states = {}
        self._state_since_ns = {}
        self._last_observed_ns = {}
        self._last_matching_event_ns = {}
        for channel_id, value in input_values.items():
            _validate_channel(channel_id)
            self._states[channel_id] = bool(value)
            self._state_since_ns[channel_id] = now_ns
            self._last_observed_ns[channel_id] = now_ns
            # 対象edgeがまだ一度も発生していない場合も、監視開始時点から
            # absence timerを開始する。反対方向のedgeでは起点を更新しない。
            self._last_matching_event_ns[channel_id] = {
                EdgeDirection.RISING: now_ns,
                EdgeDirection.FALLING: now_ns,
            }

    def observe(
        self,
        channel_id: int,
        value: bool,
        now_ns: int,
        *,
        is_edge: bool = True,
    ) -> None:
        """入力観測を記録し、状態継続・対象edgeの起点を更新する。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
            value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。
            now_ns: 判定時monotonic timestamp（ns）。 経過時間計算に使う。
            is_edge: 観測が確定edge eventかどうか。 snapshot観測ではFalse。

        Returns:
            None。 対象device、 時計、 registry、 または状態機械の内部状態を更新する。

        Raises:
            ValueError: 対象channelがactive sessionのsnapshotに含まれていない場合。
                観測時刻が以前のmonotonic timestampより小さい場合。
        """
        _validate_channel(channel_id)
        _validate_now(now_ns)
        if channel_id not in self._states:
            raise ValueError("channel is not part of the active session")
        if now_ns < self._last_observed_ns[channel_id]:
            raise ValueError("monotonic time moved backwards")
        old_value = self._states[channel_id]
        new_value = bool(value)
        if old_value != new_value:
            self._state_since_ns[channel_id] = now_ns
        self._last_observed_ns[channel_id] = now_ns
        self._states[channel_id] = new_value
        if is_edge and old_value != new_value:
            direction = EdgeDirection.RISING if new_value else EdgeDirection.FALLING
            self._last_matching_event_ns[channel_id][direction] = now_ns

    def is_active(
        self,
        rule: ConditionRule,
        now_ns: int,
        *,
        not_before_ns: int | None = None,
    ) -> bool:
        """指定ruleが現在成立しているか返す。

        Args:
            rule: 評価対象の単一条件rule。 enabledとrule_typeに従って判定する。
            now_ns: 判定時monotonic timestamp（ns）。 経過時間計算に使う。
            not_before_ns: 状態継続時間へ算入してよい最早monotonic時刻（ns）。

        Returns:
            条件が成立している場合True、 未成立または未設定の場合False。
        """
        _validate_now(now_ns)
        if not_before_ns is not None:
            _validate_now(not_before_ns)
        if not rule.enabled or rule.input_channel not in self._states:
            return False
        duration_ns = rule.duration_ms * 1_000_000
        if rule.rule_type is RuleType.STATE_DURATION:
            state = self._states[rule.input_channel]
            if state != rule.expected_value:
                return False
            state_since_ns = max(
                self._state_since_ns[rule.input_channel], not_before_ns or 0
            )
            return now_ns - state_since_ns >= duration_ns
        last_event_ns = self._last_matching_event_ns[rule.input_channel].get(
            rule.edge,
            self._state_since_ns[rule.input_channel],
        )
        if not_before_ns is not None:
            last_event_ns = max(last_event_ns, not_before_ns)
        return now_ns - last_event_ns >= duration_ns

    def evaluate_all(
        self,
        rules: list[ConditionRule],
        now_ns: int,
    ) -> dict[int, bool]:
        """有効・無効を含むruleの成立状態をID別に返す。

        Args:
            rules: 保存または評価する条件rule列。 IDと条件値を使って識別する。
            now_ns: 判定時monotonic timestamp（ns）。 経過時間計算に使う。

        Returns:
            channelまたはrule IDから成立状態へのmapping。 対象がなければ空dict。
        """
        return {rule.id: self.is_active(rule, now_ns) for rule in rules}


def _validate_channel(channel_id: int) -> None:
    """IN/OUT共通のchannel範囲を検証する。

    Args:
        channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。

    Returns:
        None。 制約を検証するだけで、 永続状態や入力値は変更しない。

    Raises:
        ValueError: channelが1〜6の範囲外の場合。
    """
    if not 1 <= channel_id <= 6:
        raise ValueError("channel must be between 1 and 6")


def _validate_now(now_ns: int) -> None:
    """monotonic timestampが負でないことを検証する。

    Args:
        now_ns: 判定時monotonic timestamp（ns）。 経過時間計算に使う。

    Returns:
        None。 制約を検証するだけで、 永続状態や入力値は変更しない。

    Raises:
        ValueError: monotonic timestampが負の場合。
    """
    if now_ns < 0:
        raise ValueError("monotonic timestamp must not be negative")
