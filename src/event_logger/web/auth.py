"""共有パスワードとCSRF tokenの標準ライブラリ実装。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from threading import RLock


class SharedPasswordAuth:
    """ユーザー管理を持たない共有パスワード認証。"""

    def __init__(self, *, enabled: bool, password_hash: str | None) -> None:
        """認証有効状態とscrypt hashを受け取る。"""
        self._lock = RLock()
        self._enabled = enabled
        self._password_hash = password_hash
        if enabled and not password_hash:
            raise ValueError("password_hash is required when auth is enabled")

    @property
    def enabled(self) -> bool:
        """現在の認証有効状態をthread-safeに返す。"""
        with self._lock:
            return self._enabled

    def verify(self, password: str) -> bool:
        """入力パスワードを保存済みhashと比較する。"""
        with self._lock:
            enabled = self._enabled
            password_hash = self._password_hash
        if not enabled or password_hash is None:
            return True
        return verify_password(password, password_hash)

    def configure(self, *, enabled: bool, password_hash: str | None) -> None:
        """設定画面から共有認証の有効状態を更新する。"""
        if enabled and not password_hash:
            raise ValueError("password_hash is required when auth is enabled")
        with self._lock:
            self._enabled = enabled
            self._password_hash = password_hash


def hash_password(password: str) -> str:
    """scryptで共有パスワードをhash化し、設定保存用文字列を返す。"""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
    )
    return "scrypt$16384$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    """scrypt形式を検証し、不正形式はFalseとして扱う。"""
    try:
        algorithm, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
        if algorithm != "scrypt":
            return False
        n_value = int(n_text)
        r_value = int(r_text)
        p_value = int(p_text)
        if (n_value, r_value, p_value) != (2**14, 8, 1):
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        if len(salt) != 16 or len(expected) != 64:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n_value,
            r=r_value,
            p=p_value,
        )
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)
