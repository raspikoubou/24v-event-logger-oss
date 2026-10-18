"""入力状態を安定時間で確定するdebounce state machine。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Debouncer:
    """新状態が連続して安定した時だけ状態変化を確定する。"""

    debounce_ms: int
    stable_value: bool | None = None
    _candidate_value: bool | None = None
    _candidate_started_ns: int | None = None
    _last_observed_ns: int | None = None

    def __post_init__(self) -> None:
        """debounce設定を検証する。"""
        if not 0 <= self.debounce_ms <= 5000:
            raise ValueError("debounce_ms must be between 0 and 5000")

    def initialize(self, value: bool, observed_ns: int) -> None:
        """現在値をsnapshotの基準状態として初期化する。"""
        self.stable_value = value
        self._candidate_value = None
        self._candidate_started_ns = None
        self._record_time(observed_ns)

    # 入力が動いた時点ではまだエッジを確定しない。安定時間を満たした候補だけを採用する。
    def observe(self, value: bool, observed_ns: int) -> bool | None:
        """入力変化を観測し、確定した新状態だけを返す。

        Args:
            value: GPIO adapterが論理値へ正規化した現在値。
            observed_ns: 同じClockから得たmonotonic時刻。

        Returns:
            安定時間を満たして確定した場合の新状態。未確定ならNone。
        """
        self._record_time(observed_ns)
        if self.stable_value is None:
            self.initialize(value, observed_ns)
            return None
        if value == self.stable_value:
            if self._candidate_value is None:
                return None
            if self._candidate_started_ns is None:
                raise RuntimeError("candidate state has no start timestamp")
            stable_ns = self.debounce_ms * 1_000_000
            if observed_ns - self._candidate_started_ns < stable_ns:
                # 候補が安定時間に達する前に元の状態へ戻ったので、ノイズとして破棄する。
                self._candidate_value = None
                self._candidate_started_ns = None
                return None

            # callbackの間隔がdebounce時間より長い場合、元の状態へ戻る
            # callbackを受け取った時点で候補状態は既に確定している。
            # 現在の状態も次の候補として開始し、パルス後半を失わない。
            confirmed = self._confirm_candidate(self._candidate_value)
            self._candidate_value = value
            self._candidate_started_ns = observed_ns
            return confirmed
        if value != self._candidate_value:
            self._candidate_value = value
            self._candidate_started_ns = observed_ns
            if self.debounce_ms == 0:
                self.stable_value = value
                self._candidate_value = None
                self._candidate_started_ns = None
                return value
            return None
        if self._candidate_started_ns is None:
            raise RuntimeError("candidate state has no start timestamp")
        stable_ns = self.debounce_ms * 1_000_000
        if observed_ns - self._candidate_started_ns >= stable_ns:
            return self._confirm_candidate(value)
        return None

    # 最後のGPIO通知以降に変化がなくても、候補の安定時間を確認できるようにする。
    def poll(self, observed_ns: int) -> bool | None:
        """現在の入力候補が安定時間を満たしたか確認する。

        GPIOのedge callbackは状態が変わった瞬間に一度だけ呼ばれるため、
        callback受信時刻だけで判定すると「変化後も安定している」ことを
        観測できない。daemonの通常loopからこのメソッドを定期的に呼び、
        callbackを追加で要求せずに安定時間の経過を確定する。

        Args:
            observed_ns: 同じClockから得たmonotonic時刻。

        Returns:
            安定時間を満たした場合の新状態。未確定ならNone。
        """
        self._record_time(observed_ns)
        if self._candidate_value is None or self._candidate_started_ns is None:
            return None
        stable_ns = self.debounce_ms * 1_000_000
        if observed_ns - self._candidate_started_ns < stable_ns:
            return None
        return self._confirm_candidate(self._candidate_value)

    def _confirm_candidate(self, value: bool) -> bool:
        """候補状態を確定し、次の候補を受け付ける状態へ戻す。"""
        self.stable_value = value
        self._candidate_value = None
        self._candidate_started_ns = None
        return value

    # 監視時刻の逆行は長さ計算を壊すため、monotonic時刻の前提を確認する。
    def _record_time(self, observed_ns: int) -> None:
        """monotonic時刻の逆行を検出する。"""
        if observed_ns < 0:
            raise ValueError("observed_ns must not be negative")
        if self._last_observed_ns is not None and observed_ns < self._last_observed_ns:
            raise ValueError("observed_ns must be monotonic")
        self._last_observed_ns = observed_ns
