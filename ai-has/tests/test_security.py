import logging
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app import main, ratelimit
from app.config import settings

HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}
SECRET_URL = "postgresql://aihas:secret-pw@db.example.com:5432/postgres"
SECRET_KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE123456"


class FakeEngine:
    @contextmanager
    def connect(self):
        yield type("Conn", (), {"execute": lambda self, *a, **k: None})()


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(main, "engine", FakeEngine())
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    ratelimit.limiter.reset()
    yield
    ratelimit.limiter.reset()


def client():
    return TestClient(main.app, raise_server_exceptions=False)


def assert_headers(res):
    for name, value in HEADERS.items():
        assert res.headers[name] == value


def test_headers_on_success_and_not_found():
    c = client()
    ok = c.get("/health")
    assert ok.status_code == 200
    assert_headers(ok)
    missing = c.get("/no-such-page")
    assert missing.status_code == 404
    assert_headers(missing)


def test_headers_on_static_style_responses(tmp_path, monkeypatch):
    chat = tmp_path / "chat"
    chat.mkdir()
    (chat / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    monkeypatch.setattr(main, "CHAT_DIR", chat)
    res = client().get("/")
    assert res.status_code == 200
    assert_headers(res)


def test_headers_on_rate_limited_response(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_chat_per_min", 1)
    monkeypatch.setattr(main, "answer", lambda q: {"answer": "ok", "citations": [], "escalate": False, "distances": []})
    c = client()
    c.post("/chat", json={"message": "printer"})
    res = c.post("/chat", json={"message": "printer"})
    assert res.status_code == 429
    assert_headers(res)


def test_unhandled_error_becomes_generic_500_without_leaking(monkeypatch, caplog):
    def boom(question):
        raise RuntimeError(f"could not connect to {SECRET_URL} using key {SECRET_KEY}")

    monkeypatch.setattr(main, "answer", boom)
    with caplog.at_level(logging.ERROR):
        res = client().post("/chat", json={"message": "My printer is not working"})

    assert res.status_code == 500
    assert res.json() == {"detail": main.GENERIC_ERROR}
    assert_headers(res)
    for secret in (SECRET_URL, SECRET_KEY, "secret-pw", "RuntimeError", "Traceback"):
        assert secret not in res.text
    # the details are kept in the server log for debugging
    assert any(r.exc_info for r in caplog.records)


def test_http_exceptions_pass_through_unchanged(monkeypatch):
    def no_user(*args, **kwargs):
        raise ValueError("No employee user found to own the ticket")

    monkeypatch.setattr(main, "create_ticket", no_user)
    monkeypatch.setattr(main, "update_status", lambda ticket_id, status: None)
    c = client()

    bad_request = c.post("/tickets", json={"summary": "Printer is jammed"})
    assert bad_request.status_code == 400
    assert bad_request.json() == {"detail": "No employee user found to own the ticket"}

    not_found = c.patch("/tickets/999", json={"status": "resolved"})
    assert not_found.status_code == 404
    assert not_found.json() == {"detail": "Ticket not found"}
    assert_headers(not_found)


def test_validation_error_keeps_fastapi_422_format():
    res = client().post("/chat", json={})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["type"] == "missing"
    assert detail[0]["loc"] == ["body", "message"]
    assert_headers(res)
