"""Linux systemdの時刻同期状態を読む小さなadapter。"""

from __future__ import annotations

from pathlib import Path


# OSの時刻同期状態を確認し、イベントに同期可否を記録できるようにする。
def is_clock_synchronized(
    status_path: str | Path = "/run/systemd/timesync/synchronized",
) -> bool:
    """systemd-timesyncdが同期済みと示した場合だけTrueを返す。"""
    # systemd-timesyncdは同期成功時にこのパスをtouchするマーカーを作る。
    # 内容を読む方式では、空ファイルという正しい状態を未同期と誤判定する。
    try:
        return Path(status_path).is_file()
    except OSError:
        return False
