from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app import audit, auth, main, tickets
from app.auth import safe_next
from app.config import settings
from app.loginlimit import login_limiter
from app.ratelimit import limiter
from fake_auth_db import PASSWORD, FakeAuthDb

ROLES = ["employee", "agent", "admin"]
EMAILS = {"employee": "employee@example.com", "agent": "agent@example.com", "admin": "admin@example.com"}


@pytest.fixture
def seen(monkeypatch, tmp_path):
    """Real login and cookies against a fake users table. The data layer behind each route is stubbed."""
    monkeypatch.setattr(settings, "session_secret", "unit-test-secret-" + "x" * 32)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    login_limiter.reset()
    limiter.reset()
    db = FakeAuthDb()
    monkeypatch.setattr(auth, "engine", db)
    monkeypatch.setattr(audit, "engine", db)

    calls = {"ticket": [], "status": [], "chat": [], "confirm": []}
    monkeypatch.setattr(main, "list_tickets", lambda status=None: [{"id": 1}])
    monkeypatch.setattr(main, "get_stats", lambda tz="UTC": {"total": 1})

    def fake_status(ticket_id, status, actor=None):
        calls["status"].append((ticket_id, status, actor))
        return {"id": ticket_id, "status": status}

    def fake_ticket(summary, priority, source="ai", user_email=None):
        calls["ticket"].append(user_email)
        return {"id": 1, "ref": "TCK-0001", "summary": summary}

    def fake_propose(user_email, name, args):
        calls["chat"].append(user_email)

    monkeypatch.setattr(main, "update_status", fake_status)
    monkeypatch.setattr(main, "create_ticket", fake_ticket)
    monkeypatch.setattr(main, "answer", lambda q: {"answer": "ok", "citations": [], "escalate": False, "distances": []})
    monkeypatch.setattr(main, "propose_action", lambda q: None)
    monkeypatch.setattr(main.proposals, "propose", fake_propose)
    monkeypatch.setattr(
        main.proposals, "decide", lambda aid, email, confirm: calls["confirm"].append(email) or {"status": "done"}
    )

    for name, text in (("chat", "index.html"), ("admin", "admin.html"), ("login", "login.html")):
        folder = tmp_path / name
        folder.mkdir()
        (folder / text).write_text(f"<h1>{name} page</h1>", encoding="utf-8")
        monkeypatch.setattr(main, f"{name.upper()}_DIR", folder)
    calls["db"] = db
    yield calls
    login_limiter.reset()
    limiter.reset()


def client_as(role):
    """A client with a real session cookie for that role, or no cookie for None."""
    client = TestClient(main.app, follow_redirects=False)
    if role:
        res = client.post("/auth/login", json={"email": EMAILS[role], "password": PASSWORD})
        assert res.status_code == 200
    return client


# (method, path, json body, roles allowed in, expected status when allowed)
STAFF_ONLY = [
    ("GET", "/tickets", None, ["agent", "admin"], 200),
    ("GET", "/stats", None, ["agent", "admin"], 200),
    ("PATCH", "/tickets/1", {"status": "resolved"}, ["agent", "admin"], 200),
]
ANY_USER = [
    ("POST", "/chat", {"message": "My printer is not working"}, ROLES, 200),
    ("POST", "/tickets", {"summary": "Printer is jammed"}, ROLES, 200),
    ("POST", "/actions/confirm", {"action_id": "a" * 20, "confirm": True}, ROLES, 200),
]


@pytest.mark.parametrize("method, path, body, allowed, ok", STAFF_ONLY + ANY_USER)
@pytest.mark.parametrize("role", [None, *ROLES])
def test_each_role_against_each_protected_endpoint(seen, role, method, path, body, allowed, ok):
    res = client_as(role).request(method, path, json=body)
    if role is None:
        assert res.status_code == 401  # logged out
    elif role in allowed:
        assert res.status_code == ok
    else:
        assert res.status_code == 403  # logged in, wrong role


def test_an_employee_gets_403_not_a_redirect_on_the_api(seen):
    res = client_as("employee").get("/tickets")
    assert res.status_code == 403 and res.json() == {"detail": "You do not have access to this."}
    assert seen["status"] == []


def test_open_endpoints_need_no_login(seen, monkeypatch):
    class Engine:
        @contextmanager
        def connect(self):
            yield type("C", (), {"execute": lambda self, *a, **k: None})()

    monkeypatch.setattr(main, "engine", Engine())
    c = client_as(None)
    assert c.get("/health").status_code == 200
    assert c.get("/docs").status_code == 200
    assert c.get("/login").status_code == 200


# ---------- who owns what ----------
def test_the_logged_in_user_owns_the_ticket(seen):
    client_as("agent").post("/tickets", json={"summary": "Printer is jammed"})
    client_as("employee").post("/tickets", json={"summary": "Printer is jammed"})
    assert seen["ticket"] == ["agent@example.com", "employee@example.com"]


def test_confirm_and_chat_use_the_logged_in_user(seen, monkeypatch):
    monkeypatch.setattr(main, "propose_action", lambda q: {"name": "request_password_reset", "arguments": {}})
    client_as("employee").post("/chat", json={"message": "reset my password please"})
    client_as("admin").post("/actions/confirm", json={"action_id": "a" * 20, "confirm": False})
    assert seen["chat"] == ["employee@example.com"]
    assert seen["confirm"] == ["admin@example.com"]


def test_a_status_change_passes_the_acting_user_for_the_audit(seen):
    client_as("agent").patch("/tickets/7", json={"status": "in_progress"})
    assert seen["status"] == [(7, "in_progress", "user:2")]


# ---------- pages ----------
def test_logged_out_pages_redirect_to_the_login_page(seen):
    c = client_as(None)
    home = c.get("/")
    assert home.status_code == 302 and home.headers["location"] == "/login"
    admin = c.get("/admin")
    assert admin.status_code == 302 and admin.headers["location"] == "/login?next=/admin"
    assert "no-cache" in admin.headers["cache-control"]


@pytest.mark.parametrize("role, status", [("employee", 403), ("agent", 200), ("admin", 200)])
def test_the_admin_page_is_for_staff_only(seen, role, status):
    res = client_as(role).get("/admin")
    assert res.status_code == status
    assert ("admin page" in res.text) == (status == 200)
    if status == 403:
        assert "No access" in res.text


@pytest.mark.parametrize("role", ROLES)
def test_the_chat_page_is_open_to_every_logged_in_role(seen, role):
    res = client_as(role).get("/")
    assert res.status_code == 200 and "chat page" in res.text


def test_a_logged_in_user_is_sent_away_from_the_login_page(seen):
    c = client_as("employee")
    assert c.get("/login").headers["location"] == "/"
    assert c.get("/login?next=/admin").headers["location"] == "/admin"
    assert c.get("/login?next=https://evil.example.com").headers["location"] == "/"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/admin", "/admin"),
        ("/", "/"),
        (None, "/"),
        ("", "/"),
        ("https://evil.example.com", "/"),
        ("//evil.example.com", "/"),
        ("/\\evil.example.com", "/"),
        ("javascript:alert(1)", "/"),
        ("/ok\r\nSet-Cookie: x=y", "/"),
    ],
)
def test_safe_next_only_allows_local_paths(value, expected):
    assert safe_next(value) == expected


# ---------- audit of ticket status changes ----------
class Rows:
    def __init__(self, status):
        self.status = status
        self.audit = []
        self.joined = False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "for update" in sql:
            return type("R", (), {"scalar": lambda s: self.status})()
        if "update tickets" in sql:
            row = {"id": params["id"], "status": params["s"], "resolved_at": None}
            return type("R", (), {"mappings": lambda s: type("M", (), {"first": lambda m: row})()})()
        if "insert into audit_logs" in sql:
            self.audit.append(params)
            return None
        raise AssertionError(sql)


class RowsEngine:
    def __init__(self, rows):
        self.rows, self.transactions = rows, 0

    @contextmanager
    def begin(self):
        self.transactions += 1
        yield self.rows


def test_a_status_change_is_audited_in_the_same_transaction(monkeypatch):
    rows = Rows("open")
    engine = RowsEngine(rows)
    monkeypatch.setattr(tickets, "engine", engine)
    result = tickets.update_status(5, "resolved", actor="user:2")
    assert result["status"] == "resolved"
    assert engine.transactions == 1  # the update and the audit row share one transaction
    (row,) = rows.audit
    assert row["a"] == "user:2" and row["n"] == "ticket_status_change"
    assert '"from": "open"' in row["p"] and '"to": "resolved"' in row["p"] and '"ticket_id": 5' in row["p"]
    assert "@" not in row["p"]


def test_an_unknown_ticket_is_not_audited(monkeypatch):
    rows = Rows(None)
    monkeypatch.setattr(tickets, "engine", RowsEngine(rows))
    assert tickets.update_status(999, "resolved", actor="user:2") is None
    assert rows.audit == []


def test_without_an_actor_no_audit_row_is_written(monkeypatch):
    rows = Rows("open")
    monkeypatch.setattr(tickets, "engine", RowsEngine(rows))
    tickets.update_status(5, "resolved")
    assert rows.audit == []
