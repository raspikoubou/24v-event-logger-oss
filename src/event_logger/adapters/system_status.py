"""Linux systemdの時刻同期状態を読む小さなadapter。"""

from __future__ import annotations

from pathlib import Path


def is_clock_synchronized(
    status_path: str | Path = "/run/systemd/timesync/synchronized",
) -> bool:
    """systemd-timesyncdが同期済みと示した場合だけTrueを返す。

    Args:
        status_path: 時計同期済みを示すsystemd marker file path。

    Returns:
        条件が成立している場合True、 未成立または未設定の場合False。
    """
    # systemd-timesyncdは同期成功時にこのパスをtouchするマーカーを作る。
    # 内容を読む方式では、空ファイルという正しい状態を未同期と誤判定する。
    try:
        return Path(status_path).is_file()
    except OSError:
        return False
