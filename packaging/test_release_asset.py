"""配布範囲と同梱ライブラリの説明を確認する。"""

from __future__ import annotations

from pathlib import Path

from create_release_asset import _archive_entries


def test_regression_tools_are_not_distributed() -> None:
    """開発用の追加試験をアプリarchiveへ入れない。"""
    names = {path.as_posix() for path, _, _ in _archive_entries()}
    assert "packaging/test_auth_sessions.py" not in names
    assert "packaging/test_auth_sessions.cjs" not in names
    assert "packaging/test_update_monitor.cjs" not in names
    assert "packaging/test_update_status.py" not in names
    assert "packaging/test_release_asset.py" not in names
    root = Path(__file__).resolve().parents[1]
    for path in (root / "packaging").glob("test_*"):
        assert path.relative_to(root).as_posix() not in names
    assert "packaging/install.sh" in names
    assert "packaging/update.sh" in names


def test_sqlite_notice_contains_source_and_compatibility_only() -> None:
    """同梱ライブラリの出典・互換情報と作業記録を区別する。"""
    root = Path(__file__).resolve().parents[1]
    body = (root / "packaging/private-runtime/sqlite/BUILD_INFO.md").read_text()
    assert "sqlite.org/2026/sqlite-autoconf-3530400.tar.gz" in body
    assert "AArch64" in body
    assert "SHA-256" in body
    assert "smoke evidence" not in body.lower()
    assert "isolated candidate" not in body.lower()
    assert "passed on a temporary" not in body.lower()
