"""loggerdが使うGPIO port protocol。"""

from __future__ import annotations

from typing import Protocol


class OutputPort(Protocol):
    """論理OUTを操作する最小port。"""

    @property
    def is_active(self) -> bool:
        """現在の論理出力状態を返す。"""

    def on(self) -> None:
        """論理OUTをONにする。"""

    def off(self) -> None:
        """論理OUTをOFFにする。"""

    def toggle(self) -> None:
        """論理OUTを反転する。"""
