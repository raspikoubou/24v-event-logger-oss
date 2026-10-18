"""loggerdが使うGPIO port protocol。"""

from __future__ import annotations

from typing import Protocol


class OutputPort(Protocol):
    """論理OUTを操作する最小port。"""

    @property
    def is_active(self) -> bool:
        """現在の論理出力状態を返す。

        Args:
            なし。

        Returns:
            条件が成立している場合True、 未成立または未設定の場合False。
        """

    def on(self) -> None:
        """論理OUTをONにする。

        Args:
            なし。

        Returns:
            None。 実装portに論理ON状態の設定を要求する。
        """

    def off(self) -> None:
        """論理OUTをOFFにする。

        Args:
            なし。

        Returns:
            None。 実装portに論理OFF状態の設定を要求する。
        """

    def toggle(self) -> None:
        """論理OUTを反転する。

        Args:
            なし。

        Returns:
            None。 実装portに現在の論理状態の反転を要求する。
        """
