from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main, tickets

CREATED = datetime(2026, 11, 21, 3, 13, 40)


def row(ticket_id, external_ref):
    return {
        "id": ticket_id, "summary": f"Ticket {ticket_id}", "priority": "low", "status": "open",
        "source": "ai", "created_at": CREATED, "resolved_at": None, "external_ref": external_ref,
        "email": "a@example.com", "name": "A",
    }


class Engine:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, stmt, params=None):
        self.sql.append(str(stmt))
        rows = self.rows
        return type("R", (), {"mappings": lambda s: type("M", (), {"all": lambda m: rows})()})()


@pytest.fixture
def engine(monkeypatch):
    fake = Engine([row(7, "HELP-4821"), row(6, None)])
    monkeypatch.setattr(tickets, "engine", fake)
    return fake


def test_the_list_query_reads_external_ref(engine):
    tickets.list_tickets()
    assert "t.external_ref" in engine.sql[0]


def test_a_synced_ticket_shows_its_key_and_an_unsynced_one_is_null(engine):
    synced, pending = tickets.list_tickets()
    assert synced["external_ref"] == "HELP-4821"
    assert pending["external_ref"] is None
    assert synced["ref"] == "TCK-0007"  # the local reference is unchanged


def test_the_tickets_route_includes_external_ref(engine, login_as):
    login_as("agent")
    body = TestClient(main.app).get("/tickets").json()
    assert [t["external_ref"] for t in body] == ["HELP-4821", None]
    assert {"id", "ref", "summary", "priority", "status", "source", "created_at", "resolved_at",
            "requester", "requester_email", "external_ref"} <= set(body[0])


def test_employees_still_cannot_list_tickets(engine, login_as):
    login_as("employee")
    assert TestClient(main.app).get("/tickets").status_code == 403
