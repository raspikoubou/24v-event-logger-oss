"""Web APIの共通JSON response envelope。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flask import Response, jsonify


def _ok(data: Mapping[str, Any]) -> Response:
    """成功payloadを共通envelopeで返す。

    Args:
        data: `data` fieldへ格納するJSON対応mapping。

    Returns:
        `{ok: true, data: ...}`を持つHTTP 200 response。
    """
    return jsonify({"ok": True, "data": data})


def _error(code: str, message: str, status_code: int) -> Response:
    """利用者向けの安全なエラーenvelopeを返す。

    Args:
        code: API契約で使う固定error code。
        message: 内部詳細を含めない表示用message。
        status_code: HTTP responseのstatus code。

    Returns:
        `{ok: false, error: {code, message}}`を持つresponse。
    """
    response = jsonify({"ok": False, "error": {"code": code, "message": message}})
    response.status_code = status_code
    return response
