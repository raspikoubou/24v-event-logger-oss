#!/usr/bin/env python3
"""複数Web workerで共有するFlask署名鍵を設定ファイルへ永続化する。"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import tempfile
from pathlib import Path

_KEY_NAME = "EVENT_LOGGER_SECRET_KEY"
_KEY_ASSIGNMENT = re.compile(r"^[ \t]*EVENT_LOGGER_SECRET_KEY[ \t]*=(.*)$")


def _configured_assignment(contents: str) -> str | None:
    """既存の非空キー設定を返す。空設定は初回生成の対象にする。

        Args:
            contents: 読み込んだ設定・本文のテキスト。

        Returns:
            非空の署名鍵設定行。空・未設定ならNone。
    """
    for line in contents.splitlines():
        match = _KEY_ASSIGNMENT.match(line)
        if match is None:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            return line
    return None


def ensure_web_secret(env_path: Path) -> str:
    """設定に署名鍵を一つだけ残し、原子的に保存する。

    Args:
        env_path: systemd EnvironmentFileとして読む設定ファイル。

    Returns:
        保存した署名鍵。呼び出し元は値をログへ出さないこと。

    Raises:
        OSError: 設定ファイルの読み書きに失敗した場合。
        ValueError: 設定パスがシンボリックリンクの場合。
    """
    if env_path.is_symlink():
        raise ValueError(f"設定ファイルがシンボリックリンクです: {env_path}")

    contents = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    existing_assignment = _configured_assignment(contents)
    secret_key = (
        existing_assignment.split("=", maxsplit=1)[1].strip()
        if existing_assignment is not None
        else secrets.token_urlsafe(48)
    )

    preserved_lines = [
        line for line in contents.splitlines() if _KEY_ASSIGNMENT.match(line) is None
    ]
    if existing_assignment is not None:
        preserved_lines.append(existing_assignment)
    else:
        preserved_lines.append(f"{_KEY_NAME}={secret_key}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{env_path.name}.", dir=env_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temporary_file:
            temporary_file.write("\n".join(preserved_lines) + "\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, 0o640)
        os.replace(temporary_path, env_path)
        directory_fd = os.open(env_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)
    return secret_key


def main() -> int:
    """Parse arguments and provision the shared web signing key.

        Args:
            なし。

        Returns:
            CLI終了code。secret維持/作成成功は0、file/設定errorは非0。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path)
    arguments = parser.parse_args()
    try:
        ensure_web_secret(arguments.env_file)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
