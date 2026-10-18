"""販売中HATのGPIO割当を一箇所へ集約する。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

INPUT_PINS: Final[Mapping[int, int]] = MappingProxyType(
    {
        1: 5,
        2: 6,
        3: 13,
        4: 19,
        5: 26,
        6: 16,
    }
)
"""IN1〜IN6のBCM GPIO番号。ユーザー設定には公開しない。"""

OUTPUT_PINS: Final[Mapping[int, int]] = MappingProxyType(
    {
        1: 17,
        2: 27,
        3: 22,
        4: 23,
        5: 24,
        6: 25,
    }
)
"""OUT1〜OUT6のBCM GPIO番号。ユーザー設定には公開しない。"""

INPUT_PULL_DOWN: Final[bool] = True
"""HAT入力回路はプルダウンを必要とする。"""

INPUT_ACTIVE_HIGH: Final[bool] = True
"""HAT側で論理入力ONへ正規化するGPIOレベル。実機で必ず照合する。"""

OUTPUT_ACTIVE_HIGH: Final[bool] = True
"""PNP出力を論理ONにするGPIOレベル。実機で必ず照合する。"""


def validate_definition() -> None:
    """固定定義のチャンネル数と重複を検証する。

    Args:
        なし。

    Returns:
        None。 制約を検証するだけで、 永続状態や入力値は変更しない。

    Raises:
        ValueError: 「HAT definition must contain channels 1 through
            6」に該当すると判定した場合。 「HAT input and output GPIO pins must be
            unique」に該当すると判定した場合。
    """
    expected_channels = set(range(1, 7))
    if set(INPUT_PINS) != expected_channels or set(OUTPUT_PINS) != expected_channels:
        raise ValueError("HAT definition must contain channels 1 through 6")
    all_pins = list(INPUT_PINS.values()) + list(OUTPUT_PINS.values())
    if len(all_pins) != len(set(all_pins)):
        raise ValueError("HAT input and output GPIO pins must be unique")
