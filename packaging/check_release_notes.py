"""ユーザー向けRelease本文への内部検証情報混入を検査する。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

INTERNAL_PATTERNS = (
    r"(?:テスト|検証|試験).*(?:\d+\s*(?:件|項目)|成功|失敗|pass|passed)",
    r"(?:pytest|ruff|mypy|CI/CD|CI結果|コミットID|commit\s+[0-9a-f]{7,40})",
    r"[A-Za-z0-9_/.-]+\.(?:py|jsfrag|js|cjs)(?![A-Za-z0-9_])",
    r"(?:関数|モジュール|リファクタリング|内部管理|作業報告)",
)


def violations(body: str) -> list[int]:
    """内部情報候補の行番号を返す。意味と事実は別途レビューする。

        Args:
            body: 検査・送信するHTTPまたはRelease本文。

        Returns:
            公開本文内の内部情報候補を示す1-based行番号list。本文の意味確認は行わない。
    """
    return [
        number
        for number, line in enumerate(body.splitlines(), 1)
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in INTERNAL_PATTERNS)
    ]


def main() -> int:
    """指定本文を読み取り検査し、混入候補があれば失敗する。

        Args:
            なし。

        Returns:
            CLI終了code。違反なしは0、候補検出または入力errorは非0。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("body_file", type=Path)
    args = parser.parse_args()
    body = args.body_file.read_text(encoding="utf-8")
    lines = violations(body)
    if not body.strip() or lines:
        print(f"Release本文の確認が必要です: 行 {lines}")
        return 1
    print("Release本文の内部情報検査: OK（内容のレビューも必要です）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
