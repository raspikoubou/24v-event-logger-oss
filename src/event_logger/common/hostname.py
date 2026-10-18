"""OS hostnameの検証と初期値生成。"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path


def validate_hostname(hostname: str) -> None:
    """systemdへ渡す単一ラベルhostnameを検証する。

    Args:
        hostname: OS hostnameとして設定する候補値。

    Returns:
        None。 制約を検証するだけで、 永続状態や入力値は変更しない。

    Raises:
        ValueError: 「hostname must be 1..63 characters」に該当すると判定した場合。
            「hostname must contain lowercase letters, numbers, and
            hyphens」に該当すると判定した場合。
    """
    if not 1 <= len(hostname) <= 63:
        raise ValueError("hostname must be 1..63 characters")
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname) is None:
        raise ValueError(
            "hostname must contain lowercase letters, numbers, and hyphens"
        )


def default_hostname(
    *, machine_id_path: str | Path = "/etc/machine-id", mac_address: int | None = None
) -> str:
    """端末固有値から衝突しにくい初期hostnameを作る。

    Args:
        machine_id_path: 端末識別用machine-id file path。
            読めない場合にMACへfallbackする。
        mac_address: 端末識別のfallbackに使う数値MAC値。 Noneならuuid.getnode()を使う。

    Returns:
        検査または正規化を通過した文字列値。
    """
    machine_id = ""
    try:
        machine_id = Path(machine_id_path).read_text(encoding="utf-8").strip()
    except OSError:
        # macOSのdemoや最小環境にはmachine-idがないためMACへフォールバックする。
        pass
    numeric_mac = mac_address if mac_address is not None else uuid.getnode()
    identity = machine_id or f"{numeric_mac:012x}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:4]
    return f"event-logger-{suffix}"


__all__ = ["default_hostname", "validate_hostname"]
