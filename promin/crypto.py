"""Comparing and hashing secrets."""
from __future__ import annotations

import hashlib
import hmac

# What new passwords are hashed with: the OWASP recommendation for PBKDF2-HMAC-SHA256.
PBKDF2_ROUNDS = 600_000
# What passwords were hashed with before the rounds were recorded next to the hash.
LEGACY_PBKDF2_ROUNDS = 200_000


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_password(password: str, salt: bytes, rounds: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds).hex()


def hash_secret(secret: str) -> str:
    """What is stored instead of a cookie secret."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()
