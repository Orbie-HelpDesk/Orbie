import json

import pytest
from fastapi.testclient import TestClient

from app import audit, auth, main, sessions
from app.config import settings
from app.loginlimit import login_limiter
from fake_auth_db import PASSWORD, FakeAuthDb


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    login_limiter.reset()
    monkeypatch.setattr(settings, "session_secret", "unit-test-secret-" + "x" * 32)
    monkeypatch.setattr(settings, "session_hours", 4.0)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "login_max_failures_per_email", 5)
    monkeypatch.setattr(settings, "login_max_failures_per_ip", 20)
    monkeypatch.setattr(settings, "login_window_minutes", 10)
    yield
    login_limiter.reset()


@pytest.fixture
def db(monkeypatch):
    fake = FakeAuthDb()
    monkeypatch.setattr(auth, "engine", fake)
    monkeypatch.setattr(audit, "engine", fake)
    return fake


@pytest.fixture
def client(db):
    return TestClient(main.app)


def login(client, email="employee@example.com", password=PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


# ---------- tokens ----------
def test_token_round_trip_and_expiry():
    token = sessions.create_token(7, now=1000)
    assert sessions.read_token(token, now=1000 + 60) == 7
    assert sessions.read_token(token, now=1000 + 4 * 3600) is None  # expired at exactly the lifetime


def test_a_changed_or_forged_token_is_rejected():
    token = sessions.create_token(7)
    payload, signature = token.split(".")
    other = sessions.create_token(8).split(".")[0]
    for bad in (f"{other}.{signature}", f"{payload}.{signature[:-2]}AA", payload, "", "a.b.c", "!!.??", None):
        assert sessions.read_token(bad) is None


def test_a_token_signed_with_another_secret_is_rejected(monkeypatch):
    token = sessions.create_token(7)
    monkeypatch.setattr(settings, "session_secret", "a-different-secret-" + "y" * 32)
    assert sessions.read_token(token) is None


def test_without_a_strong_secret_nothing_is_trusted(monkeypatch):
    token = sessions.create_token(7)
    monkeypatch.setattr(settings, "session_secret", "too-short")
    assert sessions.read_token(token) is None
    with pytest.raises(sessions.SessionConfigError):
        sessions.create_token(7)


# ---------- login ----------
def test_login_success_returns_the_user_and_sets_the_cookie(client, db):
    res = login(client)
    assert res.status_code == 200
    assert res.json() == {"id": 1, "email": "employee@example.com", "name": "Demo Employee", "role": "employee"}
    assert "password" not in res.text
    assert res.headers["cache-control"] == "no-store"
    assert sessions.read_token(client.cookies.get(sessions.COOKIE_NAME)) == 1


def test_the_cookie_is_httponly_samesite_lax_and_expires(client):
    cookie = login(client).headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "max-age=14400" in cookie
    assert "secure" not in cookie.replace("samesite", "")


def test_the_cookie_is_secure_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "cookie_secure", True)
    cookie = login(client).headers["set-cookie"].lower()
    assert "; secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie


def test_email_is_matched_ignoring_case_and_spaces(client):
    assert login(client, email="  Employee@Example.COM ").status_code == 200


@pytest.mark.parametrize(
    "email, password",
    [
        ("employee@example.com", "wrong password"),
        ("nobody@example.com", PASSWORD),
        ("nopass@example.com", PASSWORD),  # a user with no password set
        ("nopass@example.com", ""),
        ("odd@example.com", PASSWORD),  # an unknown role is never let in
    ],
)
def test_login_failure_is_a_plain_401_with_no_cookie(client, email, password):
    res = login(client, email, password) if password else client.post("/auth/login", json={"email": email, "password": "x"})
    assert res.status_code == 401
    assert res.json() == {"detail": auth.BAD_LOGIN_MESSAGE}  # same text for every reason
    assert "set-cookie" not in res.headers


def test_login_is_refused_when_the_secret_is_not_set(client, monkeypatch):
    monkeypatch.setattr(settings, "session_secret", "")
    res = login(client)
    assert res.status_code == 503 and "SESSION_SECRET" in res.json()["detail"]


def test_login_input_is_validated(client):
    assert client.post("/auth/login", json={"email": "a@b.co"}).status_code == 422
    assert client.post("/auth/login", json={"email": "a@b.co", "password": "x" * 201}).status_code == 422


# ---------- audit ----------
def test_login_logout_and_failures_are_audited_without_personal_data(client, db):
    login(client, password="wrong")
    login(client, email="nobody@example.com")
    login(client)
    client.post("/auth/logout", json={})
    assert [(r["actor"], r["action"]) for r in db.audit] == [
        ("user:1", "login_failed"),
        ("anonymous", "login_failed"),
        ("user:1", "login"),
        ("user:1", "logout"),
    ]
    text = json.dumps(db.audit)
    assert "@" not in text and "example.com" not in text and "wrong" not in text and PASSWORD not in text


def test_a_broken_audit_table_does_not_stop_login(client, db):
    db.audit_broken = True
    assert login(client).status_code == 200


# ---------- lockout ----------
def test_five_failures_lock_the_email_even_for_the_right_password(client):
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401
    res = login(client)  # correct password, but locked
    assert res.status_code == 429
    assert int(res.headers["retry-after"]) > 0
    assert login(client, email="agent@example.com").status_code == 200  # other emails are not affected


def test_a_successful_login_clears_the_email_counter(client):
    for _ in range(4):
        login(client, password="wrong")
    assert login(client).status_code == 200
    for _ in range(4):
        assert login(client, password="wrong").status_code == 401


def test_the_lock_ends_after_the_window(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(login_limiter, "_clock", lambda: now[0])
    for _ in range(5):
        login(client, password="wrong")
    assert login(client).status_code == 429
    now[0] += 10 * 60 + 1
    assert login(client).status_code == 200


def test_one_ip_trying_many_emails_is_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures_per_ip", 6)
    for i in range(6):
        assert login(client, email=f"user{i}@example.com", password="wrong").status_code == 401
    assert login(client, email="fresh@example.com", password="wrong").status_code == 429
    assert login(client).status_code == 429  # even a valid login, from the same IP


def test_unknown_emails_are_locked_like_real_ones(client):
    for _ in range(5):
        login(client, email="ghost@example.com", password="x")
    assert login(client, email="ghost@example.com", password="x").status_code == 429


# ---------- current user, logout ----------
def test_me_needs_a_valid_session(client, db):
    assert client.get("/auth/me").status_code == 401
    login(client, email="agent@example.com")
    res = client.get("/auth/me")
    assert res.status_code == 200 and res.json()["role"] == "agent"
    assert res.headers["cache-control"] == "no-store"


def test_me_rejects_a_forged_expired_or_orphaned_token(client, db):
    for token in (
        "garbage",
        sessions.create_token(1, now=1),  # long expired
        sessions.create_token(999),  # no such user
        sessions.create_token(5),  # a role the app does not know
    ):
        client.cookies.set(sessions.COOKIE_NAME, token)
        assert client.get("/auth/me").status_code == 401


def test_the_role_is_read_from_the_database_on_every_request(client, db):
    login(client, email="agent@example.com")
    assert client.get("/auth/me").json()["role"] == "agent"
    db.users[2]["role"] = "employee"
    assert client.get("/auth/me").json()["role"] == "employee"
    del db.users[2]
    assert client.get("/auth/me").status_code == 401


def test_logout_clears_the_cookie_and_is_audited(client, db):
    login(client)
    res = client.post("/auth/logout", json={})
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    cleared = res.headers["set-cookie"].lower()
    assert "max-age=0" in cleared and "httponly" in cleared
    assert client.get("/auth/me").status_code == 401
    assert db.audit[-1] == {"actor": "user:1", "action": "logout", "payload": {}}


def test_logout_without_a_session_is_harmless_and_not_audited(client, db):
    assert client.post("/auth/logout", json={}).status_code == 200
    assert db.audit == []


def test_logout_only_clears_the_cookie_the_token_itself_stays_valid(client):
    login(client)
    token = client.cookies.get(sessions.COOKIE_NAME)
    client.post("/auth/logout", json={})
    client.cookies.set(sessions.COOKIE_NAME, token)  # someone kept a copy
    assert client.get("/auth/me").status_code == 200  # known limit: it lasts until it expires


# ---------- cross-site protection ----------
def test_state_changing_requests_must_be_json(client):
    form = client.post("/auth/login", data={"email": "employee@example.com", "password": PASSWORD})
    assert form.status_code == 415
    text_plain = client.post(
        "/auth/login", content=b'{"email":"a","password":"b"}', headers={"Content-Type": "text/plain"}
    )
    assert text_plain.status_code == 415
    assert client.post("/auth/logout").status_code == 415  # no body, no JSON header


def test_a_request_from_another_site_is_rejected(client):
    res = client.post(
        "/auth/login",
        json={"email": "employee@example.com", "password": PASSWORD},
        headers={"Origin": "https://evil.example.com"},
    )
    assert res.status_code == 403 and "set-cookie" not in res.headers


def test_a_request_from_this_site_is_accepted(client):
    res = client.post(
        "/auth/login",
        json={"email": "employee@example.com", "password": PASSWORD},
        headers={"Origin": "http://testserver"},
    )
    assert res.status_code == 200


def test_rejected_cross_site_requests_still_get_security_headers(client):
    res = client.post("/auth/login", data={"a": "b"})
    assert res.headers["x-content-type-options"] == "nosniff"
