"""Limits for failed logins, in memory: per email and per client IP.

Only failures count, and a successful login clears the counter for that email. An email is
kept only as a hash. The counters live in this process and reset on restart. Because the
limit is per email, someone can lock a known account for a few minutes by failing on purpose.
That is the usual price of this protection, and the per-IP limit stops one client from
trying many emails.
"""
import hashlib
import math
import threading
import time
from collections import deque

from .config import settings

MAX_TRACKED = 10_000


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _email_key(email: str) -> str:
    return "email:" + hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


class LoginLimiter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()

    def _window(self) -> float:
        return settings.login_window_minutes * 60

    def _trim(self, key: str, now: float) -> deque:
        hits = self._fails.setdefault(key, deque())
        while hits and now - hits[0] >= self._window():
            hits.popleft()
        return hits

    def _wait(self, key: str, limit: int, now: float) -> int | None:
        hits = self._trim(key, now)
        if limit > 0 and len(hits) >= limit:
            return max(1, math.ceil(self._window() - (now - hits[0])))
        return None

    def blocked_for(self, email: str, ip: str) -> int | None:
        """Seconds to wait if this email or this IP has too many recent failures, else None."""
        now = self._clock()
        with self._lock:
            waits = [
                self._wait(_email_key(email), settings.login_max_failures_per_email, now),
                self._wait(f"ip:{ip}", settings.login_max_failures_per_ip, now),
            ]
        waits = [w for w in waits if w is not None]
        return max(waits) if waits else None

    def record_failure(self, email: str, ip: str) -> None:
        now = self._clock()
        with self._lock:
            if len(self._fails) >= MAX_TRACKED:
                self._sweep(now)
            for key in (_email_key(email), f"ip:{ip}"):
                self._trim(key, now).append(now)

    def clear_email(self, email: str) -> None:
        with self._lock:
            self._fails.pop(_email_key(email), None)

    def _sweep(self, now: float) -> None:
        for key in [k for k, h in self._fails.items() if not h or now - h[-1] >= self._window()]:
            del self._fails[key]


login_limiter = LoginLimiter()
