import logging
from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main, tickets


class FakeResult:
    def __init__(self, value=None, row=None):
        self._value, self._row = value, row

    def scalar(self):
        return self._value

    def mappings(self):
        return self

    def one(self):
        return self._row


class FakeConn:
    """Answers the three queries create_ticket runs, and records the insert."""

    def __init__(self, users_by_email, first_employee_id):
        self.users_by_email = users_by_email
        self.first_employee_id = first_employee_id
        self.queries = []
        self.insert_params = None

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.queries.append(sql)
        if "where email" in sql:
            return FakeResult(value=self.users_by_email.get(params["e"]))
        if "where role = 'employee'" in sql:
            return FakeResult(value=self.first_employee_id)
        if "insert into tickets" in sql:
            self.insert_params = params
            row = {"id": 12, "status": "open", "priority": params["p"], "created_at": datetime(2026, 1, 1)}
            return FakeResult(row=row)
        raise AssertionError(f"unexpected sql: {sql}")


class FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def begin(self):
        yield self.conn


def use_engine(monkeypatch, users_by_email=None, first_employee_id=None):
    conn = FakeConn(users_by_email or {}, first_employee_id)
    monkeypatch.setattr(tickets, "engine", FakeEngine(conn))
    return conn


def test_configured_user_is_used_without_warning_or_fallback(monkeypatch, caplog):
    conn = use_engine(monkeypatch, {tickets.DEFAULT_USER: 5}, first_employee_id=99)
    with caplog.at_level(logging.WARNING):
        result = tickets.create_ticket("Printer is jammed")
    assert conn.insert_params["u"] == 5
    assert not any("where role" in q for q in conn.queries)
    assert caplog.records == []
    assert result["ref"] == "TCK-0012"


def test_missing_user_falls_back_to_first_employee_and_warns(monkeypatch, caplog):
    conn = use_engine(monkeypatch, {}, first_employee_id=1)
    with caplog.at_level(logging.WARNING):
        result = tickets.create_ticket("Printer is jammed")
    assert conn.insert_params["u"] == 1
    assert result["ref"] == "TCK-0012"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "fallback" in warnings[0].getMessage()


def test_no_user_at_all_raises_clear_error(monkeypatch):
    use_engine(monkeypatch, {}, first_employee_id=None)
    with pytest.raises(ValueError, match="No employee user"):
        tickets.create_ticket("Printer is jammed")


def test_post_tickets_creates_ticket_and_redacts_pii(monkeypatch, login_as):
    login_as("employee")
    conn = use_engine(monkeypatch, {}, first_employee_id=1)
    res = TestClient(main.app).post(
        "/tickets", json={"summary": "My VPN fails, email me at juan@company.com", "priority": "high"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ref"] == "TCK-0012"
    assert body["priority"] == "high"
    assert "juan@company.com" not in conn.insert_params["s"]
    assert conn.insert_params["src"] == "escalation"


def test_post_tickets_returns_400_when_no_user_exists(monkeypatch, login_as):
    login_as("employee")
    use_engine(monkeypatch, {}, first_employee_id=None)
    res = TestClient(main.app).post("/tickets", json={"summary": "Printer is jammed"})
    assert res.status_code == 400
    assert "No employee user" in res.json()["detail"]
