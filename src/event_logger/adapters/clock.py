"""時刻取得をDomainから分離するadapter。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from event_logger.adapters.system_status import is_clock_synchronized


# 履歴用のUTC時刻と継続時間測定用のmonotonic時刻を明確に分離する。
class Clock(Protocol):
    """イベント時刻に必要なwall clockとmonotonic clockを提供する。"""

    def now_utc_ms(self) -> int:
        """現在のUTC時刻をUnix epochミリ秒で返す。"""

    def now_monotonic_ns(self) -> int:
        """起動中の経過時間をナノ秒で返す。"""

    def is_synchronized(self) -> bool:
        """取得時点でシステム時計が同期済みかを返す。"""


# 実機の時計を使用し、同期状態も外部へ公開する。
class SystemClock:
    """Python標準時計を使う本番用Clock。"""

    def __init__(self, sync_checker: Callable[[], bool] | None = None) -> None:
        """Clockを作成する。

        Args:
            sync_checker: NTP等の同期状態を返す関数。未指定時はsystemdの同期状態を読む。
        """
        self._sync_checker = sync_checker or is_clock_synchronized

    def now_utc_ms(self) -> int:
        """現在のUTC時刻をUnix epochミリ秒で返す。"""
        return time.time_ns() // 1_000_000

    def now_monotonic_ns(self) -> int:
        """起動中の経過時間をナノ秒で返す。"""
        return time.monotonic_ns()

    def is_synchronized(self) -> bool:
        """NTP等の同期状態を返す。"""
        return self._sync_checker()


@dataclass
# テストで時刻の経過とUTC補正を個別に模擬する。
class FakeClock:
    """sleepを使わず時刻を進められるテスト用Clock。"""

    utc_ms: int
    monotonic_ns: int
    synchronized: bool = True

    def now_utc_ms(self) -> int:
        """固定されたUTC時刻を返す。"""
        return self.utc_ms

    def now_monotonic_ns(self) -> int:
        """固定されたmonotonic時刻を返す。"""
        return self.monotonic_ns

    def is_synchronized(self) -> bool:
        """固定された同期状態を返す。"""
        return self.synchronized

    # 経過時間をUTCとmonotonicの双方へ反映する。
    def advance(self, milliseconds: int) -> None:
        """wall clockとmonotonic clockを同じ時間だけ進める。"""
        if milliseconds < 0:
            raise ValueError("milliseconds must not be negative")
        self.utc_ms += milliseconds
        self.monotonic_ns += milliseconds * 1_000_000

    # NTP補正を模擬するため、monotonic時刻を動かさずUTCだけ変更する。
    def set_utc_ms(self, utc_ms: int) -> None:
        """NTP補正を表現するためwall clockだけを変更する。"""
        if utc_ms < 0:
            raise ValueError("utc_ms must not be negative")
        self.utc_ms = utc_ms
