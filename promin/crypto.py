"""Comparing and hashing secrets."""
from __future__ import annotations

import hashlib
import hmac

PBKDF2_ROUNDS = 200_000


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


def hash_secret(secret: str) -> str:
    """What is stored instead of a cookie secret."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()
