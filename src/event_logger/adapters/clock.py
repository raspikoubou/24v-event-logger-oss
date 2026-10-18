"""時刻取得をDomainから分離するadapter。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from event_logger.adapters.system_status import is_clock_synchronized


class Clock(Protocol):
    """イベント時刻に必要なwall clockとmonotonic clockを提供する。"""

    def now_utc_ms(self) -> int:
        """現在のUTC時刻をUnix epochミリ秒で返す。

        Args:
            なし。

        Returns:
            経過時間または継続時間（ms）。 UNKNOWNは確定時間に含めない。
        """

    def now_monotonic_ns(self) -> int:
        """起動中の経過時間をナノ秒で返す。

        Args:
            なし。

        Returns:
            条件に従って算出した整数値。
        """

    def is_synchronized(self) -> bool:
        """取得時点でシステム時計が同期済みかを返す。

        Args:
            なし。

        Returns:
            条件が成立している場合True、 未成立または未設定の場合False。
        """


class SystemClock:
    """Python標準時計を使う本番用Clock。"""

    def __init__(self, sync_checker: Callable[[], bool] | None = None) -> None:
        """Clockを作成する。

        Args:
            sync_checker: NTP等の同期状態を返す関数。
                未指定時はsystemdの同期状態を読む。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._sync_checker = sync_checker or is_clock_synchronized

    def now_utc_ms(self) -> int:
        """現在のUTC時刻をUnix epochミリ秒で返す。

        Args:
            なし。

        Returns:
            経過時間または継続時間（ms）。 UNKNOWNは確定時間に含めない。
        """
        return time.time_ns() // 1_000_000

    def now_monotonic_ns(self) -> int:
        """起動中の経過時間をナノ秒で返す。

        Args:
            なし。

        Returns:
            条件に従って算出した整数値。
        """
        return time.monotonic_ns()

    def is_synchronized(self) -> bool:
        """NTP等の同期状態を返す。

        Args:
            なし。

        Returns:
            条件が成立している場合True、 未成立または未設定の場合False。
        """
        return self._sync_checker()


@dataclass
class FakeClock:
    """sleepを使わず時刻を進められるテスト用Clock。"""

    utc_ms: int
    monotonic_ns: int
    synchronized: bool = True

    def now_utc_ms(self) -> int:
        """固定されたUTC時刻を返す。

        Args:
            なし。

        Returns:
            経過時間または継続時間（ms）。 UNKNOWNは確定時間に含めない。
        """
        return self.utc_ms

    def now_monotonic_ns(self) -> int:
        """固定されたmonotonic時刻を返す。

        Args:
            なし。

        Returns:
            条件に従って算出した整数値。
        """
        return self.monotonic_ns

    def is_synchronized(self) -> bool:
        """固定された同期状態を返す。

        Args:
            なし。

        Returns:
            条件が成立している場合True、 未成立または未設定の場合False。
        """
        return self.synchronized

    def advance(self, milliseconds: int) -> None:
        """wall clockとmonotonic clockを同じ時間だけ進める。

        Args:
            milliseconds: FakeClockのUTC/monotonic両clockを進める量（ms）。

        Returns:
            None。 対象device、 時計、 registry、 または状態機械の内部状態を更新する。

        Raises:
            ValueError: millisecondsが負の場合。
        """
        if milliseconds < 0:
            raise ValueError("milliseconds must not be negative")
        self.utc_ms += milliseconds
        self.monotonic_ns += milliseconds * 1_000_000

    def set_utc_ms(self, utc_ms: int) -> None:
        """NTP補正を表現するためwall clockだけを変更する。

        Args:
            utc_ms: FakeClockへ設定するUTC Unix epoch timestamp（ms）。

        Returns:
            None。 対象device、 時計、 registry、 または状態機械の内部状態を更新する。

        Raises:
            ValueError: utc_msが負の場合。
        """
        if utc_ms < 0:
            raise ValueError("utc_ms must not be negative")
        self.utc_ms = utc_ms
