"""ユーザー向けRelease本文への内部検証情報混入を検査する。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# 公開文の書き方は.github/RELEASE_POLICY.mdを正本とする。
# 件数がない未確認報告も検出し、公開前に内部記録と切り分ける。
INTERNAL_PATTERNS = (
    r"(?:未確認|未実施|未検証|未試験|未テスト|確認していません|確認していない)",
    r"(?:開発側|内部実装|全テスト|FakeGPIO|スキップ)",
    r"(?:Ruff|mypy|pytest|型チェック|静的解析|CI(?:結果|合格|チェック|確認))",
    r"(?:Python|JavaScript).*(?:\d+\s*(?:件|suite)|成功|失敗|passed)",
    r"(?:実GPIO|実機|実Pi|Raspberry Pi|24時間運転).*(?:未確認|未実施|確認済み|確認しました|確認しています)",
    r"(?:テスト|検証|試験).*(?:\d+\s*(?:件|項目)|成功|失敗|pass|passed)",
    r"(?:pytest|ruff|mypy|CI/CD|CI結果|コミットID|commit\s+[0-9a-f]{7,40})",
    r"[A-Za-z0-9_/.-]+\.(?:py|jsfrag|js|cjs)(?![A-Za-z0-9_])",
    r"(?:関数|モジュール|リファクタリング|内部管理|作業報告)",
)


def violations(body: str) -> list[int]:
    """内部情報候補の行番号を返す。意味と事実は別途レビューする。"""
    return [
        number
        for number, line in enumerate(body.splitlines(), 1)
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in INTERNAL_PATTERNS)
    ]


def main() -> int:
    """指定本文を読み取り検査し、混入候補があれば失敗する。"""
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
