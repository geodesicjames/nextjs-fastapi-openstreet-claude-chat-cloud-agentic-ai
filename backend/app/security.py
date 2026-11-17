"""Helpers for random tokens.

`secrets.token_urlsafe(32)` gives 256 bits of randomness, which is far beyond
guessing range. We store only `hash_token(token)`, so the raw value exists
only in the user's email or cookie.
"""

import hashlib
import secrets


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
