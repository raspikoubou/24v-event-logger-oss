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
        """認証有効状態とscrypt hashを受け取る。

Args:
    enabled: 共有パスワード保護を有効にするかを表す真偽値。
    password_hash: scrypt形式の保存済みパスワードhash。認証設定の比較・検証に使う。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
        self._lock = RLock()
        self._enabled = enabled
        self._password_hash = password_hash
        # Cookieにはhashを出さず、現在の認証設定にだけ有効な乱数を持たせる。
        # Web再起動でも既存ログインを失効させ、再認証を要求する。
        self._session_generation = secrets.token_urlsafe(32)
        if enabled and not password_hash:
            raise ValueError("password_hash is required when auth is enabled")

    @property
    def enabled(self) -> bool:
        """現在の認証有効状態をthread-safeに返す。

Args:
    なし。

Returns:
    判定結果。条件を満たす場合はTrueを返し、
        満たさない場合はFalse（または注釈に示すNone）を返す。"""
        with self._lock:
            return self._enabled

    def verify(self, password: str) -> bool:
        """入力パスワードを保存済みhashと比較する。

Args:
    password: 照合または保存対象の共有パスワード。平文をログや応答へ含めない。

Returns:
    判定結果。条件を満たす場合はTrueを返し、
        満たさない場合はFalse（または注釈に示すNone）を返す。"""
        with self._lock:
            enabled = self._enabled
            password_hash = self._password_hash
        if not enabled or password_hash is None:
            return True
        return verify_password(password, password_hash)

    def authenticate(self, password: str) -> str | None:
        """照合した認証設定に対応するCookie用世代を返す。

            Args:
                password: 照合または保存対象の共有パスワード。
                    平文をログや応答へ含めない。

            Returns:
                現在設定のgeneration文字列。password不一致または認証無効ならNone。
        """
        # 照合後に別threadでpasswordが変わっても、新しい世代を旧passwordへ
        # 発行しないよう、照合と世代取得を同じlock内で完結させる。
        with self._lock:
            if (
                not self._enabled
                or self._password_hash is None
                or not verify_password(password, self._password_hash)
            ):
                return None
            return self._session_generation

    def accepts_session(self, generation: object) -> bool:
        """現在の共有認証に対応するログインだけを受け付ける。

Args:
    generation: sessionから読んだauth generation。
        現在有効なgeneration文字列との一致を確認する。

Returns:
    判定結果。条件を満たす場合はTrueを返し、
        満たさない場合はFalse（または注釈に示すNone）を返す。"""
        with self._lock:
            return not self._enabled or (
                isinstance(generation, str)
                and generation.isascii()
                and secrets.compare_digest(generation, self._session_generation)
            )

    def configure(self, *, enabled: bool, password_hash: str | None) -> None:
        """設定画面から共有認証の有効状態を更新する。

Args:
    enabled: 共有パスワード保護を有効にするかを表す真偽値。
    password_hash: scrypt形式の保存済みパスワードhash。認証設定の比較・検証に使う。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
        if enabled and not password_hash:
            raise ValueError("password_hash is required when auth is enabled")
        with self._lock:
            if enabled != self._enabled or password_hash != self._password_hash:
                # 無効化→同じpasswordで再有効化しても古いCookieを復活させない。
                self._session_generation = secrets.token_urlsafe(32)
            self._enabled = enabled
            self._password_hash = password_hash


def hash_password(password: str) -> str:
    """scryptで共有パスワードをhash化し、設定保存用文字列を返す。

        Args:
            password: 照合または保存対象の共有パスワード。平文をログや応答へ含めない。

        Returns:
            scrypt形式の保存用文字列。random salt、固定cost parameters、
                Base64化したsalt/digestを含む。
    """
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
    """scrypt形式を検証し、不正形式はFalseとして扱う。

Args:
    password: 照合または保存対象の共有パスワード。平文をログや応答へ含めない。
    encoded: 保存形式のscrypt password hash文字列。

Returns:
    判定結果。条件を満たす場合はTrueを返し、
        満たさない場合はFalse（または注釈に示すNone）を返す。"""
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
