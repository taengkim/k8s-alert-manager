"""JWT, symmetric encryption, and token-hashing helpers.

All key material is derived from `settings.secret_key` so a single
setting configures both JWT signing and Fernet encryption.
"""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import jwt
from cryptography.fernet import Fernet

from app.config import get_settings

JWT_ALGORITHM = "HS256"


@lru_cache
def _fernet() -> Fernet:
    """Derive a urlsafe Fernet key from settings.secret_key (sha256 -> base64)."""
    settings = get_settings()
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def create_jwt(user_id: int, is_admin: bool) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "is_admin": is_admin,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_ttl_hours),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])


def encrypt_str(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_str(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
