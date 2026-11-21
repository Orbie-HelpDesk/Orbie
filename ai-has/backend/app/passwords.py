"""Password hashing with the standard library (PBKDF2-HMAC-SHA256). No extra dependency.

Stored form: pbkdf2_sha256$<iterations>$<salt, base64>$<hash, base64>
"""
import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 200  # stops someone from sending a huge value to be hashed


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    return f"{ALGORITHM}${iterations}${_b64(salt)}${_b64(_derive(password, salt, iterations))}"


def verify_password(password: str, stored: str | None) -> bool:
    """True only if the password matches. A missing or malformed hash is simply False."""
    try:
        algorithm, iterations, salt, expected = (stored or "").split("$")
        if algorithm != ALGORITHM:
            return False
        actual = _derive(password, base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(actual, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


# Checked when the email is unknown, so a wrong email costs the same time as a wrong password.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_password_rules(password: str) -> str | None:
    """A reason the password is not acceptable, or None if it is fine."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    return None
