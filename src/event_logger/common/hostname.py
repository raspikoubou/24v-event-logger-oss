"""OS hostnameの検証と初期値生成。"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path


# OSへ渡すホスト名は許容文字・長さを制限する。
def validate_hostname(hostname: str) -> None:
    """systemdへ渡す単一ラベルhostnameを検証する。"""
    if not 1 <= len(hostname) <= 63:
        raise ValueError("hostname must be 1..63 characters")
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname) is None:
        raise ValueError(
            "hostname must contain lowercase letters, numbers, and hyphens"
        )


# デバイス固有の既定ホスト名を一つの手順で作る。
def default_hostname(
    *, machine_id_path: str | Path = "/etc/machine-id", mac_address: int | None = None
) -> str:
    """端末固有値から衝突しにくい初期hostnameを作る。"""
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
