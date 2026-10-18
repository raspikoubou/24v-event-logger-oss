"""GPIO Zero + lgpioで販売中HATを操作するadapter。"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from event_logger.hardware.hat import (
    INPUT_ACTIVE_HIGH,
    INPUT_PINS,
    INPUT_PULL_DOWN,
    OUTPUT_ACTIVE_HIGH,
    OUTPUT_PINS,
    validate_definition,
)


class GPIOInitializationError(RuntimeError):
    """GPIO Zeroまたはlgpioを初期化できない。"""


class _GPIOCallbackTrace:
    """lgpio edgeからGPIO Zero callbackまでの遅れを一時記録する。"""

    _MAX_RECORDS = 2_000
    _HEADER = b"callback_mono_ns,channel_id,value,callback_delay_ns\n"

    def __init__(self, path: Path | None) -> None:
        """診断が有効な場合だけ、tmpfs上の追記ファイルを開く。

        Args:
            path: 処理対象または診断出力先のfilesystem path。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._fd: int | None = None
        self._record_count = 0
        if path is None:
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW
        self._fd = os.open(path, flags, 0o640)
        if os.fstat(self._fd).st_size == 0:
            os.write(self._fd, self._HEADER)

    def record(self, channel_id: int, value: bool, device: Any) -> None:
        """edge timestampとcallback受信時刻の差を、上限付きで記録する。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
            value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。
            device: GPIO Zero input device。
                edgeとcallback時刻の差を診断するため参照する。

        Returns:
            None。 最大2000件まで診断CSVへ追記し、 write失敗時はtrace fileを閉じる。
        """
        if self._fd is None or self._record_count >= self._MAX_RECORDS:
            return
        self._record_count += 1
        callback_mono_ns = time.monotonic_ns()
        edge_age_s = device.active_time if value else device.inactive_time
        callback_delay_ns = (
            -1 if edge_age_s is None else round(edge_age_s * 1_000_000_000)
        )
        line = (
            f"{callback_mono_ns},{channel_id},{int(value)},{callback_delay_ns}\n"
        ).encode("ascii")
        try:
            os.write(self._fd, line)
        except OSError:
            # 診断ファイルの異常を本来のGPIO callbackへ伝播させない。
            self.close()

    def close(self) -> None:
        """診断ファイルを閉じ、以降の記録を無効化する。

        Args:
            なし。

        Returns:
            None。 解放または論理OFFを適用し、 失敗は例外として呼出元へ伝える。
        """
        if self._fd is None:
            return
        os.close(self._fd)
        self._fd = None


class GpioZeroHat:
    """GPIO Zero deviceを論理IN/OUTへ変換する。

    このクラスはloggerd起動時にだけ生成する。module import時にGPIOを初期化
    しないことで、unit testとdemo modeをRaspberry Piなしで実行できる。
    """

    def __init__(
        self,
        inputs: Mapping[int, Any],
        outputs: Mapping[int, Any],
        callback_trace: _GPIOCallbackTrace | None = None,
    ) -> None:
        """初期化済みGPIO Zero deviceを保持する。

        Args:
            inputs: IN番号からGPIO Zero input deviceへのmapping。
            outputs: OUT番号からGPIO Zero output deviceへのmapping。
            callback_trace: 任意のGPIO callback診断記録先。
                Noneの場合は診断記録を省略する。

        Returns:
            None。 依存値を保持して利用可能なinstanceを初期化する。
        """
        self._inputs = dict(inputs)
        self._outputs = dict(outputs)
        self._callback_trace = callback_trace

    @classmethod
    def create(cls) -> GpioZeroHat:
        """lgpio backendを明示してHATの全deviceを作る。

        Args:
            なし。

        Returns:
            GpioZeroHat型の結果。 戻り値の型に応じ、
                該当なしはNoneまたは空collectionとなる。

        Raises:
            GPIOInitializationError: 「gpiozero and lgpio are required on Raspberry
                Pi」に該当すると判定した場合。 「HAT input pull configuration conflicts
                with active polarity」に該当すると判定した場合。 「failed to initialize
                HAT GPIO devices」に該当すると判定した場合。
        """
        # 実機GPIOを掴む前に、固定定義のチャンネル重複や欠落を拒否する。
        # 誤ったpin mapで一部だけ初期化されると、入力監視と出力安全停止の
        # 前提が同時に崩れるため、起動時に全体を検証する。
        validate_definition()
        trace_path = (
            Path("/run/event-logger/gpio-callback-trace.csv")
            if os.environ.get("EVENT_LOGGER_GPIO_TRACE") == "1"
            else None
        )
        callback_trace = _GPIOCallbackTrace(trace_path)
        try:
            # Raspberry Pi依存を生成時まで遅らせる。
            # host testやdemo importをGPIOなしで保つため。
            from gpiozero import (
                Device,
                DigitalInputDevice,
                DigitalOutputDevice,
            )
            from gpiozero.pins.lgpio import (
                LGPIOFactory,
            )
        except ImportError as error:
            callback_trace.close()
            raise GPIOInitializationError(
                "gpiozero and lgpio are required on Raspberry Pi"
            ) from error

        # 固定pin mapでinput/output deviceを作る。
        # 途中失敗では作成済みdeviceを閉じてから例外を外へ返す。
        try:
            Device.pin_factory = LGPIOFactory(chip=0)
            pull_up = not INPUT_PULL_DOWN
            # GPIO Zeroはpull_upがTrue/Falseのときactive_stateを自動決定し、
            # active_stateを同時指定するとPinInvalidStateを送出する。HATの
            # 固定極性はpull-up/downの選択で表現し、矛盾は起動時に拒否する。
            expected_active_high = not pull_up
            if INPUT_ACTIVE_HIGH != expected_active_high:
                raise GPIOInitializationError(
                    "HAT input pull configuration conflicts with active polarity"
                )
            inputs: dict[int, Any] = {}
            outputs: dict[int, Any] = {}
            try:
                for channel_id, pin in INPUT_PINS.items():
                    inputs[channel_id] = DigitalInputDevice(
                        pin,
                        pull_up=pull_up,
                        bounce_time=None,
                    )
                for channel_id, pin in OUTPUT_PINS.items():
                    outputs[channel_id] = DigitalOutputDevice(
                        pin,
                        active_high=OUTPUT_ACTIVE_HIGH,
                        initial_value=False,
                    )
            except Exception:
                for device in (*inputs.values(), *outputs.values()):
                    device.close()
                raise
        except Exception as error:
            callback_trace.close()
            raise GPIOInitializationError(
                "failed to initialize HAT GPIO devices"
            ) from error
        return cls(inputs, outputs, callback_trace)

    def input_values(self) -> dict[int, bool]:
        """全入力を論理ON/OFFへ正規化して返す。

        Args:
            なし。

        Returns:
            channelまたはrule IDから成立状態へのmapping。 対象がなければ空dict。
        """
        return {
            channel_id: bool(device.is_active)
            for channel_id, device in self._inputs.items()
        }

    def attach_input_callbacks(
        self,
        callback: Callable[[int, bool], None],
    ) -> None:
        """各入力のactive/inactive callbackをloggerdへ接続する。

        Args:
            callback: 入力変化時に呼ぶ関数。 channel番号と新しい論理値を受け取る。

        Returns:
            None。 対象device、 時計、 registry、 または状態機械の内部状態を更新する。
        """
        for channel_id, device in self._inputs.items():
            device.when_activated = _make_callback(
                callback, channel_id, True, device, self._callback_trace
            )
            device.when_deactivated = _make_callback(
                callback, channel_id, False, device, self._callback_trace
            )

    def set_output(self, channel_id: int, value: bool) -> None:
        """指定OUTを論理ON/OFFへ設定する。

        Args:
            channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
            value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。

        Returns:
            None。 対象device、 時計、 registry、 または状態機械の内部状態を更新する。
        """
        _validate_channel(channel_id)
        self._outputs[channel_id].value = value

    def output_values(self) -> dict[int, bool]:
        """全OUTの論理状態を返す。

        Args:
            なし。

        Returns:
            channelまたはrule IDから成立状態へのmapping。 対象がなければ空dict。
        """
        return {
            channel_id: bool(device.is_active)
            for channel_id, device in self._outputs.items()
        }

    @property
    def output_ports(self) -> Mapping[int, Any]:
        """OutputManagerへ渡す論理OUT port mappingを返す。

        Args:
            なし。

        Returns:
            OUT番号からOutputPortへのmapping。 deviceを通じて論理出力を操作する。
        """
        return self._outputs

    def all_outputs_off(self) -> None:
        """全OUTをOFFにする。

        Args:
            なし。

        Returns:
            None。 解放または論理OFFを適用し、 失敗は例外として呼出元へ伝える。

        Raises:
            first_error: deviceまたはSQLite操作で失敗が返された場合。
        """
        first_error: Exception | None = None
        for device in self._outputs.values():
            try:
                device.off()
            except Exception as error:
                # 一つのport障害を他のOUTの停止中断へ波及させない。
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def close(self) -> None:
        """GPIO Zero deviceを明示的に解放する。

        Args:
            なし。

        Returns:
            None。 解放または論理OFFを適用し、 失敗は例外として呼出元へ伝える。

        Raises:
            first_error: deviceまたはSQLite操作で失敗が返された場合。
        """
        first_error: Exception | None = None
        try:
            self.all_outputs_off()
        except Exception as error:
            first_error = error
        # OFFに失敗しても全deviceの解放を試す。close時の別例外で最初の
        # 故障を隠さず、診断traceのfdも最後に必ず解放する。
        for device in (*self._inputs.values(), *self._outputs.values()):
            try:
                device.close()
            except Exception as error:
                if first_error is None:
                    first_error = error
        if self._callback_trace is not None:
            try:
                self._callback_trace.close()
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


def _make_callback(
    callback: Callable[[int, bool], None],
    channel_id: int,
    value: bool,
    device: Any | None = None,
    callback_trace: _GPIOCallbackTrace | None = None,
) -> Callable[[], None]:
    """GPIO Zeroの引数なしcallbackをloggerd callbackへ変換する。

    Args:
        callback: 入力変化時に呼ぶ関数。 channel番号と新しい論理値を受け取る。
        channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。
        value: 論理入力/出力値。 ONはTrueまたは1、 OFFはFalseまたは0。
        device: GPIO Zero input device。 edgeとcallback時刻の差を診断するため参照する。
        callback_trace: 任意のGPIO callback診断記録先。 Noneの場合は診断記録を省略する。

    Returns:
        GPIO Zero device callbackから呼べる引数なしcallable。
            edgeの論理channel/valueを閉じ込める。
    """

    def dispatch() -> None:
        """dispatchの動作を実装契約に従って処理する。

        Args:
            なし。

        Returns:
            None。 状態/DBへの変更は実装内で完了し、 値は返さない。
        """
        if callback_trace is not None and device is not None:
            callback_trace.record(channel_id, value, device)
        callback(channel_id, value)

    return dispatch


def _validate_channel(channel_id: int) -> None:
    """OUTチャンネル番号を検証する。

    Args:
        channel_id: 対象のIN/OUT番号。 製品固定の1〜6範囲で、 BCM pin番号ではない。

    Returns:
        None。 制約を検証するだけで、 永続状態や入力値は変更しない。

    Raises:
        ValueError: channel_idが1〜6の範囲外の場合。
    """
    if not 1 <= channel_id <= 6:
        raise ValueError("channel_id must be between 1 and 6")
