"""実機なしでGPIO callbackとOUT状態を再現するFakeGPIO。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

Callback = Callable[[], None]


@dataclass
class FakeInput:
    """論理入力値を手動変更できるテスト用入力。"""

    value: bool = False
    when_activated: Callback | None = None
    when_deactivated: Callback | None = None

    def set_value(self, value: bool) -> None:
        """値が変わった場合だけ対応callbackを呼び出す。

        Args:
            value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。

        Returns:
            None。 値が変わったときだけ新値を保存し、 対応するactivated/deactivated
                callbackを同期的に呼ぶ。
        """
        if value == self.value:
            return
        self.value = value
        callback = self.when_activated if value else self.when_deactivated
        if callback is not None:
            callback()


@dataclass
class FakeOutput:
    """論理出力状態を保持するテスト用出力。"""

    is_active: bool = False

    def on(self) -> None:
        """論理出力をONにする。

        Args:
            なし。

        Returns:
            None。 Fake出力のis_activeをTrueへ設定する。 物理GPIOには触れない。
        """
        self.is_active = True

    def off(self) -> None:
        """論理出力をOFFにする。

        Args:
            なし。

        Returns:
            None。 Fake出力のis_activeをFalseへ設定する。 物理GPIOには触れない。
        """
        self.is_active = False

    def toggle(self) -> None:
        """論理出力を反転する。

        Args:
            なし。

        Returns:
            None。 Fake出力のis_activeを現在値の反対へ設定する。
        """
        self.is_active = not self.is_active


class FakeGPIO:
    """IN1〜IN6とOUT1〜OUT6を保持するFakeGPIO集合。"""

    def __init__(self) -> None:
        """全チャンネルをOFF / Falseで初期化する。

        Args:
            なし。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self.inputs = {channel_id: FakeInput() for channel_id in range(1, 7)}
        self.outputs = {channel_id: FakeOutput() for channel_id in range(1, 7)}

    def set_input(self, channel_id: int, value: bool) -> None:
        """指定入力へ論理値を注入する。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
            value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。

        Returns:
            None。 対象device、 時計、 registry、 または状態機械の内部状態を更新する。
        """
        self._validate_channel(channel_id)
        self.inputs[channel_id].set_value(value)

    def all_inputs(self) -> dict[int, bool]:
        """現在の全入力状態をsnapshot用に返す。

        Args:
            なし。

        Returns:
            channelまたはrule IDから成立状態へのmapping。 対象がなければ空dict。
        """
        return {channel_id: device.value for channel_id, device in self.inputs.items()}

    def input_values(self) -> dict[int, bool]:
        """GPIO Zero adapterと同じ名前で論理入力snapshotを返す。

        Args:
            なし。

        Returns:
            channelまたはrule IDから成立状態へのmapping。 対象がなければ空dict。
        """
        return self.all_inputs()

    @property
    def output_ports(self) -> dict[int, FakeOutput]:
        """OutputManagerへ渡せる論理OUT port mappingを返す。

        Args:
            なし。

        Returns:
            結果値を識別keyへ対応させたdict。 入力mappingを変更しない。
        """
        return self.outputs

    def attach_input_callbacks(self, callback: Callable[[int, bool], None]) -> None:
        """GPIO Zero adapterと同じ形で入力callbackを接続する。

        Args:
            callback: 入力変化時に呼ぶ関数。 channel番号と新しい論理値を受け取る。

        Returns:
            None。 対象device、 時計、 registry、 または状態機械の内部状態を更新する。
        """
        for channel_id, device in self.inputs.items():
            device.when_activated = lambda channel_id=channel_id: callback(
                channel_id, True
            )
            device.when_deactivated = lambda channel_id=channel_id: callback(
                channel_id, False
            )

    def all_outputs_off(self) -> None:
        """全OUTをOFFに戻す。

        Args:
            なし。

        Returns:
            None。 解放または論理OFFを適用し、 失敗は例外として呼出元へ伝える。
        """
        for output in self.outputs.values():
            output.off()

    def close(self) -> None:
        """FakeGPIOのcloseを実機adapterと同じAPIで提供する。

        Args:
            なし。

        Returns:
            None。 解放または論理OFFを適用し、 失敗は例外として呼出元へ伝える。
        """
        self.all_outputs_off()

    @staticmethod
    def _validate_channel(channel_id: int) -> None:
        """IN/OUT共通のチャンネル番号を検証する。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。

        Returns:
            None。 制約を検証するだけで、 永続状態や入力値は変更しない。

        Raises:
            ValueError: channel_idが1〜6の範囲外の場合。
        """
        if not 1 <= channel_id <= 6:
            raise ValueError("channel_id must be between 1 and 6")
