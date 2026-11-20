from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app import main, ratelimit
from app.config import settings


@pytest.fixture(autouse=True)
def stub_backends(monkeypatch):
    """No Gemini and no database: the routes behind the limiter are replaced."""
    monkeypatch.setattr(
        main,
        "answer",
        lambda q: {"answer": "ok", "citations": [], "escalate": False, "distances": []},
    )
    monkeypatch.setattr(
        main,
        "create_ticket",
        lambda summary, priority, source="ai": {"id": 1, "ref": "TCK-0001", "summary": summary},
    )
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_chat_per_min", 20)
    monkeypatch.setattr(settings, "rate_limit_tickets_per_min", 10)
    ratelimit.limiter.reset()
    yield
    ratelimit.limiter.reset()


def client(ip="10.0.0.1"):
    return TestClient(main.app, client=(ip, 50000))


def post_chat(c):
    return c.post("/chat", json={"message": "My printer is not working"})


def post_ticket(c):
    return c.post("/tickets", json={"summary": "Printer is jammed"})


def test_chat_is_limited_to_20_per_minute():
    c = client()
    assert all(post_chat(c).status_code == 200 for _ in range(20))
    res = post_chat(c)
    assert res.status_code == 429
    assert res.json() == {"detail": ratelimit.TOO_MANY_MESSAGE}
    assert 1 <= int(res.headers["retry-after"]) <= 60


def test_tickets_are_limited_to_10_per_minute():
    c = client()
    assert all(post_ticket(c).status_code == 200 for _ in range(10))
    assert post_ticket(c).status_code == 429


def test_limits_are_separate_per_route_and_per_ip():
    a, b = client("10.0.0.1"), client("10.0.0.2")
    for _ in range(10):
        post_ticket(a)
    assert post_ticket(a).status_code == 429
    assert post_chat(a).status_code == 200  # chat has its own counter
    assert post_ticket(b).status_code == 200  # another IP is unaffected


def test_limit_clears_after_the_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit.limiter, "_clock", lambda: now[0])
    c = client()
    for _ in range(10):
        post_ticket(c)
    assert post_ticket(c).status_code == 429
    now[0] += ratelimit.WINDOW_SECONDS + 1
    assert post_ticket(c).status_code == 200


def test_limits_come_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_chat_per_min", 2)
    c = client()
    assert post_chat(c).status_code == 200
    assert post_chat(c).status_code == 200
    assert post_chat(c).status_code == 429


def test_rate_limit_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    c = client()
    assert all(post_chat(c).status_code == 200 for _ in range(50))
    assert all(post_ticket(c).status_code == 200 for _ in range(30))


class FakeEngine:
    @contextmanager
    def connect(self):
        yield type("Conn", (), {"execute": lambda self, *a, **k: None})()


def test_other_routes_are_not_limited(monkeypatch):
    monkeypatch.setattr(main, "engine", FakeEngine())  # /health must not reach a real database
    c = client()
    assert all(c.get("/health").status_code == 200 for _ in range(60))


def test_memory_is_bounded(monkeypatch):
    now = [0.0]
    limiter = ratelimit.SlidingWindowLimiter(clock=lambda: now[0])
    monkeypatch.setattr(ratelimit, "MAX_TRACKED_CLIENTS", 5)
    for i in range(5):
        limiter.check("chat", f"ip{i}", 20)
    now[0] += ratelimit.WINDOW_SECONDS + 1
    limiter.check("chat", "new-ip", 20)  # sweeps the expired clients first
    assert len(limiter._hits) == 1
