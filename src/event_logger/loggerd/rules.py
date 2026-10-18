"""DBルール、Domain evaluator、OUT ownerをつなぐloggerd側runner。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence

from event_logger.adapters.clock import Clock
from event_logger.domain.rules import OutputRule, RuleEvaluator
from event_logger.loggerd.outputs import OutputManager

_LOGGER = logging.getLogger(__name__)


class OutputRuleConfigurationError(RuntimeError):
    """rule ownerの再構成と旧状態への補償が完了しなかった。"""

    def __init__(
        self,
        primary_error: Exception,
        rollback_errors: Sequence[Exception],
    ) -> None:
        """元の設定失敗と補償失敗を保持する。

        Args:
            primary_error: 設定処理で最初に起きた例外。
            rollback_errors: 旧owner復旧中に発生した例外列。

        Returns:
            None。
        """
        self.primary_error = primary_error
        self.rollback_errors = tuple(rollback_errors)
        details = "; ".join(str(error) for error in self.rollback_errors)
        super().__init__(f"owner rollback incomplete: {details}")


class OutputRuleRunner:
    """監視可能な入力時間だけで条件ルールを評価する。"""

    def __init__(
        self,
        *,
        clock: Clock,
        output_manager: OutputManager,
        evaluator: RuleEvaluator | None = None,
    ) -> None:
        """Clock、出力副作用、純粋evaluatorを注入する。

        Args:
            clock: UTC/monotonic時刻と同期状態を提供するClock。
            output_manager: OUT owner、物理状態、出力履歴を管理する依存。
            evaluator: conditionを評価する純粋なRu
                leEvaluator。未指定なら既定実装を作る。

        Returns:
            None。
        """
        self._clock = clock
        self._output_manager = output_manager
        self._evaluator = evaluator or RuleEvaluator()
        self._rules: list[OutputRule] = []
        self._rule_reset_ns: dict[int, int] = {}
        self._owners_active = False
        self._paused_for_maintenance = False
        self._lock = threading.RLock()

    @property
    def rules(self) -> tuple[OutputRule, ...]:
        """現在ロード済みのルールを返す。

        Args:
            なし。

        Returns:
            内部設定を変更できない現在のOutputRule tuple。
        """
        with self._lock:
            return tuple(self._rules)

    @staticmethod
    def validate_configuration(
        rules: Sequence[OutputRule], heartbeat_channel: int | None = None
    ) -> None:
        """Heartbeatを含むOUT owner重複を副作用なしで検証する。

        Args:
            rules: 検証または適用する条件ruleの列。
            heartbeat_channel: Heartbeatが占有する論理OUT番号。未割当はNone。

        Returns:
            None。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        # 無効ルールも設定上のOUT割当を保持する。無効化中だけ別用途へ
        # 再利用できると、再有効化時に物理OUTの所有境界が壊れるためである。
        output_channels = [rule.condition.output_channel for rule in rules]
        if len(output_channels) != len(set(output_channels)):
            raise ValueError("multiple rules cannot share one output")
        if heartbeat_channel is not None and heartbeat_channel in output_channels:
            raise ValueError("Heartbeat and a condition rule cannot share one output")

    def configure(self, rules: Sequence[OutputRule]) -> None:
        """OUT重複を検証してrule ownerを再構成する。

        Args:
            rules: 検証または適用する条件ruleの列。

        Returns:
            None。旧ownerを解放し、新rule
                ownerを再構成する。失敗時は旧状態への補償を試み
                る。
            ownerを再構成する。失敗時は旧状態への補償を試み
            る。
            ownerを再構成する。失敗時は旧状態への補償を試みる。
            ownerを再構成する。失敗時は旧状態への補償を試みる。

        Raises:
            OutputRuleConfigurationError:
                新しいownerの適用に失敗し、旧ownerへの補償
                も完了できない場合。
        """
        with self._lock:
            self.validate_configuration(rules)
            new_rules = list(rules)
            if self._paused_for_maintenance:
                self._rules = new_rules
                self._update_reset_points(new_rules)
                return
            previous_rules = list(self._rules)
            previous_resets = dict(self._rule_reset_ns)
            previous_rules_by_id = {rule.id: rule for rule in previous_rules}
            new_owner_rules = {rule.id: rule for rule in new_rules}
            released_previous: list[OutputRule] = []
            assigned_new: list[OutputRule] = []
            try:
                for rule in previous_rules_by_id.values():
                    replacement = new_owner_rules.get(rule.id)
                    if replacement is not None and (
                        replacement.condition.output_channel
                        == rule.condition.output_channel
                    ):
                        continue
                    self._output_manager.release_rule(
                        rule.condition.output_channel,
                        rule.id,
                    )
                    released_previous.append(rule)
                for rule in new_owner_rules.values():
                    previous = previous_rules_by_id.get(rule.id)
                    if previous is not None and (
                        previous.condition.output_channel
                        == rule.condition.output_channel
                    ):
                        continue
                    self._output_manager.configure_rule(
                        rule.id,
                        rule.condition.output_channel,
                    )
                    assigned_new.append(rule)
                now_ns = self._clock.now_monotonic_ns()
                for rule in new_rules:
                    previous = next(
                        (item for item in previous_rules if item.id == rule.id),
                        None,
                    )
                    if previous is None or (
                        _condition_signature(previous) != _condition_signature(rule)
                    ):
                        self._rule_reset_ns[rule.id] = now_ns
                        if (
                            previous is not None
                            and previous.condition.enabled
                            and previous.condition.output_channel
                            == rule.condition.output_channel
                        ):
                            self._output_manager.set_rule_state(
                                rule.condition.output_channel,
                                False,
                            )
                self._rule_reset_ns = {
                    rule.id: self._rule_reset_ns[rule.id]
                    for rule in new_rules
                    if rule.id in self._rule_reset_ns
                }
            except Exception as primary_error:
                # 新設定が一部だけownerを取得した状態を残さない。DB reload ACKが
                # 失敗しても、次のtickで旧設定を継続できるよう所有権を戻す。
                _LOGGER.exception(
                    "rule owner reconfiguration failed; restoring old owners"
                )
                rollback_errors: list[Exception] = []
                for rule in assigned_new:
                    try:
                        self._output_manager.release_rule(
                            rule.condition.output_channel,
                            rule.id,
                        )
                    except Exception as rollback_error:
                        rollback_errors.append(rollback_error)
                        _LOGGER.exception(
                            "failed to release new rule owner: rule_id=%s output=%s",
                            rule.id,
                            rule.condition.output_channel,
                        )
                for rule in released_previous:
                    try:
                        self._output_manager.configure_rule(
                            rule.id,
                            rule.condition.output_channel,
                        )
                    except Exception as rollback_error:
                        rollback_errors.append(rollback_error)
                        _LOGGER.exception(
                            "failed to restore previous rule owner: "
                            "rule_id=%s output=%s",
                            rule.id,
                            rule.condition.output_channel,
                        )
                self._rule_reset_ns = previous_resets
                if rollback_errors:
                    raise OutputRuleConfigurationError(
                        primary_error,
                        rollback_errors,
                    ) from primary_error
                raise
            self._rules = new_rules
            self._owners_active = bool(new_owner_rules)

    def pause(self) -> None:
        """メンテナンス移行時にrule ownerを解放する。

        Args:
            なし。

        Returns:
            None。全rule ownerの解放を試み、最初の失敗を送出する。
        """
        with self._lock:
            self._paused_for_maintenance = True
            if not self._owners_active:
                return
            first_error: Exception | None = None
            for rule in self._rules:
                try:
                    self._output_manager.release_rule(
                        rule.condition.output_channel,
                        rule.id,
                    )
                except Exception as error:
                    # 1 OUTの異常で、他の条件通知のOFFまで中断しない。
                    if first_error is None:
                        first_error = error
            if first_error is not None:
                raise first_error
            self._owners_active = False

    def _resume_owners(self) -> None:
        """再開時に保存済みrule ownerを再割り当てする。

        Args:
            なし。

        Returns:
            None。
        """
        if self._owners_active:
            return
        for rule in self._rules:
            self._output_manager.configure_rule(
                rule.id,
                rule.condition.output_channel,
            )
        self._owners_active = True
        self._paused_for_maintenance = False

    def start_session(self, input_values: Mapping[int, bool]) -> None:
        """新sessionのsnapshotをtimer起点としてevaluatorへ渡す。

        Args:
            input_values: channel IDを論理状態へ対応づけた入力snapshot。

        Returns:
            None。snapshotをevaluatorへ渡し、条件timerの起点を現在時刻へ揃える。
        """
        with self._lock:
            self._resume_owners()
            self._evaluator.start_session(
                input_values,
                self._clock.now_monotonic_ns(),
            )
            now_ns = self._clock.now_monotonic_ns()
            self._rule_reset_ns = {rule.id: now_ns for rule in self._rules}

    def observe(self, channel_id: int, value: bool, mono_ns: int) -> None:
        """debounce確定後の入力だけをevaluatorへ渡す。

        Args:
            channel_id: 論理IN/OUT番号（1〜6）。GPIO pin番号ではない。
            value: 論理状態。ONはTrue/1、OFFはFalse/0。
            mono_ns: 確定時のmonotonic時計値（ns）。

        Returns:
            None。
        """
        with self._lock:
            self._evaluator.observe(channel_id, value, mono_ns)

    def tick(self) -> None:
        """全ruleを評価し、成立状態をOutputManagerへ反映する。

        Args:
            なし。

        Returns:
            None。成立中のrule状態を評価してOutputManagerへ反映する。
        """
        with self._lock:
            if self._paused_for_maintenance:
                return
            now_ns = self._clock.now_monotonic_ns()
            actions: list[tuple[int, bool]] = []
            for rule in self._rules:
                if not rule.condition.enabled:
                    continue
                actions.append(
                    (
                        rule.condition.output_channel,
                        self._evaluator.is_active(
                            rule.condition,
                            now_ns,
                            not_before_ns=self._rule_reset_ns.get(rule.id),
                        ),
                    )
                )
        # output_eventsへのSQLite書込みは外部I/Oであり、ここでrunner lockを
        # 保持すると、履歴DBの遅延中にdebounce確定入力まで止めてしまう。
        # loggerdの設定reloadとtickは同じ制御loopで直列化されるため、評価結果
        # を確定してから副作用だけをlock外で実行する。
        for channel_id, value in actions:
            self._output_manager.set_rule_state(channel_id, value)

    def _update_reset_points(self, rules: Sequence[OutputRule]) -> None:
        """停止中のreloadでも、次session用の条件変更情報を保つ。

        Args:
            rules: 検証または適用する条件ruleの列。

        Returns:
            None。
        """
        now_ns = self._clock.now_monotonic_ns()
        previous = {rule.id: rule for rule in self._rules}
        for rule in rules:
            old = previous.get(rule.id)
            if old is None or _condition_signature(old) != _condition_signature(rule):
                self._rule_reset_ns[rule.id] = now_ns
        self._rule_reset_ns = {
            rule.id: self._rule_reset_ns[rule.id]
            for rule in rules
            if rule.id in self._rule_reset_ns
        }


def _condition_signature(rule: OutputRule) -> tuple[object, ...]:
    """表示名を除いた、タイマーを再起点すべき設定値を返す。

    Args:
        rule: 比較する単一の条件rule。

    Returns:
        表示名を除く、条件timerを再起点する設定値tuple。
    """
    condition = rule.condition
    return (
        condition.input_channel,
        condition.output_channel,
        condition.rule_type,
        condition.duration_ms,
        condition.enabled,
        condition.expected_value,
        condition.edge,
    )
