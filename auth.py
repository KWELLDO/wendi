"""问底 · 认证：密码哈希 + 会话 token（标准库实现）。"""

from __future__ import annotations

import hashlib
import secrets

ITER = 200_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。"""
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITER).hex()
    return salt, h


def verify_password(password: str, salt: str, expected: str) -> bool:
    _, h = hash_password(password, salt)
    return secrets.compare_digest(h, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)
