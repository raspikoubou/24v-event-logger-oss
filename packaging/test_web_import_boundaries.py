"""Web route moduleのimport方向を検証する。"""

from __future__ import annotations

import ast
from pathlib import Path


def test_route_modules_do_not_import_the_app_factory() -> None:
    """routeがfactoryを逆向きimportして循環依存を作らない。"""
    root = Path(__file__).resolve().parents[1]
    web_root = root / "src" / "event_logger" / "web"
    route_files = sorted(web_root.glob("routes_*.py"))
    assert route_files
    for path in route_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"app", "event_logger.web.app"}, path.name
            elif isinstance(node, ast.Import):
                assert all(alias.name != "event_logger.web.app" for alias in node.names)
