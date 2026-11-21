"""Signed session tokens. Stateless: nothing is stored on the server.

Token: base64url(payload) + "." + base64url(HMAC-SHA256(SESSION_SECRET, payload)), where the
payload is {"uid", "iat", "exp"}. It carries only the user id. The role is read from the
database on every request, so a role change or a removed user takes effect at once.

Logout only clears the cookie. A copy of the token stays valid until it expires.
"""
import base64
import hashlib
import hmac
import json
import time

from .config import settings

MIN_SECRET_LENGTH = 32
COOKIE_NAME = "aihas_session"


class SessionConfigError(Exception):
    """SESSION_SECRET is missing or too short, so no token can be made or trusted."""


def secret_ok() -> bool:
    return len(settings.session_secret) >= MIN_SECRET_LENGTH


def _secret() -> bytes:
    if not secret_ok():
        raise SessionConfigError("SESSION_SECRET must be set to at least 32 characters.")
    return settings.session_secret.encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload_b64: str) -> bytes:
    return hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()


def lifetime_seconds() -> int:
    return int(settings.session_hours * 3600)


def create_token(user_id: int, now: float | None = None) -> str:
    now = time.time() if now is None else now
    payload = {"uid": user_id, "iat": int(now), "exp": int(now) + lifetime_seconds()}
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_b64(_sign(payload_b64))}"


def read_token(token: str | None, now: float | None = None) -> int | None:
    """The user id in a valid, unexpired token, or None for anything else."""
    if not token:
        return None
    now = time.time() if now is None else now
    try:
        payload_b64, signature_b64 = token.split(".")
        if not hmac.compare_digest(_unb64(signature_b64), _sign(payload_b64)):
            return None
        payload = json.loads(_unb64(payload_b64))
        user_id, expires = payload["uid"], payload["exp"]
    except (ValueError, KeyError, TypeError, SessionConfigError):
        return None
    if not isinstance(user_id, int) or not isinstance(expires, int) or expires <= now:
        return None
    return user_id
