"""Simple in-memory rate limit per client IP.

The counters live in this process only: they reset on restart and are not shared
between several workers. The IP is the direct client address. Behind a reverse
proxy every user would share the proxy's IP, so a proxy needs its own handling.
"""
import math
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

from .config import settings

WINDOW_SECONDS = 60
MAX_TRACKED_CLIENTS = 10_000
TOO_MANY_MESSAGE = "Too many requests. Please wait a moment and try again."


class SlidingWindowLimiter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def check(self, name: str, client: str, limit: int) -> int | None:
        """Records a hit. Returns None if allowed, or the seconds to wait if over the limit."""
        now = self._clock()
        key = (name, client)
        with self._lock:
            if len(self._hits) >= MAX_TRACKED_CLIENTS and key not in self._hits:
                self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= WINDOW_SECONDS:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, math.ceil(WINDOW_SECONDS - (now - hits[0])))
            hits.append(now)
            return None

    def _sweep(self, now: float) -> None:
        """Drops clients with no hits in the current window, so memory stays bounded."""
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= WINDOW_SECONDS]:
            del self._hits[key]


limiter = SlidingWindowLimiter()


def rate_limit(name: str, setting_name: str):
    """FastAPI dependency. The limit is read from settings on every request.

    RATE_LIMIT_ENABLED=false turns all limits off. A limit of 0 or less turns off that route.
    """

    def dependency(request: Request) -> None:
        if not settings.rate_limit_enabled:
            return
        limit = getattr(settings, setting_name)
        if limit <= 0:
            return
        client = request.client.host if request.client else "unknown"
        wait = limiter.check(name, client, limit)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail=TOO_MANY_MESSAGE,
                headers={"Retry-After": str(wait)},
            )

    return dependency


limit_chat = rate_limit("chat", "rate_limit_chat_per_min")
limit_tickets = rate_limit("tickets", "rate_limit_tickets_per_min")
