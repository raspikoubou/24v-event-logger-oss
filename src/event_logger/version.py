"""配布package metadataをアプリ表示へ渡す。"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

# source checkoutをpip installしていない開発・demo環境でも表示を壊さない。
_FALLBACK_VERSION = "1.0.2"


def get_version() -> str:
    """インストール済みevent-loggerのversionを返す。

        Args:
            なし。

        Returns:
            インストールmetadataまたはpackage defaultから得たversion文字列。
    """
    try:
        return distribution_version("event-logger")
    except PackageNotFoundError:
        return _FALLBACK_VERSION


VERSION = get_version()
"""現在のevent-logger version。"""
