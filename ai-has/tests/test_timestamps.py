import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import audit, chatlog, main, tickets
from app.ratelimit import limiter
from app.timeutil import as_utc
from fake_chat_db import FakeChatDb

# JSON from FastAPI writes UTC as "Z" or "+00:00". Both carry the zone. A bare "2026-11-21T03:13:40" does not.
AWARE_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)$")
PLAIN = datetime(2026, 11, 21, 3, 13, 40)  # what the database driver returns


def is_aware_utc(value):
    return isinstance(value, str) and AWARE_UTC.match(value) is not None


# ---------- the helper ----------
def test_a_plain_datetime_is_taken_to_be_utc():
    result = as_utc(PLAIN)
    assert result.tzinfo is UTC and result.isoformat() == "2026-11-21T03:13:40+00:00"


def test_an_aware_datetime_is_converted_to_utc():
    singapore = datetime(2026, 11, 21, 11, 13, 40, tzinfo=timezone(timedelta(hours=8)))
    assert as_utc(singapore).isoformat() == "2026-11-21T03:13:40+00:00"


def test_none_stays_none_and_other_types_are_refused():
    assert as_utc(None) is None
    with pytest.raises(TypeError):
        as_utc("2026-11-21T03:13:40")


# ---------- tickets ----------
class Rows:
    def __init__(self, rows=None, value=None):
        self._rows, self._value = rows or [], value

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def one(self):
        return self._rows[0]

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._value


class TicketsEngine:
    """Answers the ticket queries with plain datetimes, like the driver does."""

    def __init__(self):
        self.created = PLAIN
        self.resolved = PLAIN + timedelta(hours=2)

    @contextmanager
    def begin(self):
        yield self

    @contextmanager
    def connect(self):
        yield self

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "where email" in sql:
            return Rows(value=1)
        if "insert into tickets" in sql:
            return Rows([{"id": 5, "status": "open", "priority": "low", "created_at": self.created}])
        if "from tickets t join users" in sql:
            return Rows([{
                "id": 5, "summary": "VPN", "priority": "low", "status": "resolved", "source": "ai",
                "created_at": self.created, "resolved_at": self.resolved,
                "email": "a@example.com", "name": "A",
            }, {
                "id": 6, "summary": "Wi-Fi", "priority": "low", "status": "open", "source": "ai",
                "created_at": self.created, "resolved_at": None, "email": "a@example.com", "name": "A",
            }])
        if "for update" in sql:
            return Rows(value="open")
        if "update tickets set status" in sql:
            return Rows([{"id": 5, "status": "resolved", "resolved_at": self.resolved}])
        raise AssertionError(sql)


@pytest.fixture
def ticket_engine(monkeypatch):
    engine = TicketsEngine()
    monkeypatch.setattr(tickets, "engine", engine)
    return engine


def test_a_created_ticket_carries_its_zone(ticket_engine):
    assert tickets.create_ticket("VPN fails")["created_at"].isoformat() == "2026-11-21T03:13:40+00:00"


def test_listed_tickets_carry_their_zone_and_a_missing_resolved_time_stays_null(ticket_engine):
    first, second = tickets.list_tickets()
    assert first["created_at"].isoformat() == "2026-11-21T03:13:40+00:00"
    assert first["resolved_at"].isoformat() == "2026-11-21T05:13:40+00:00"
    assert second["resolved_at"] is None


def test_a_status_change_returns_an_aware_resolved_time(ticket_engine):
    assert tickets.update_status(5, "resolved")["resolved_at"].isoformat() == "2026-11-21T05:13:40+00:00"


def test_the_tickets_route_sends_zoned_timestamps_in_json(ticket_engine, login_as):
    login_as("agent")
    body = TestClient(main.app).get("/tickets").json()
    assert is_aware_utc(body[0]["created_at"]) and is_aware_utc(body[0]["resolved_at"])
    assert body[1]["resolved_at"] is None
    assert body[0]["created_at"].startswith("2026-11-21T03:13:40")  # the instant is unchanged


# ---------- conversations ----------
@pytest.fixture
def chats(monkeypatch, login_as):
    monkeypatch.setattr(main.settings, "rate_limit_enabled", False)
    limiter.reset()
    db = FakeChatDb()
    monkeypatch.setattr(chatlog, "engine", db)
    monkeypatch.setattr(audit, "engine", db)
    login_as("employee")  # id 1
    saved = chatlog.save_exchange(1, None, "My VPN keeps dropping", "1. Restart.", intent="answered")
    yield TestClient(main.app), saved
    limiter.reset()


def test_the_conversation_list_has_zoned_times(chats):
    client, _ = chats
    (row,) = client.get("/conversations").json()
    assert is_aware_utc(row["created_at"]) and is_aware_utc(row["last_at"])


def test_a_conversation_and_each_message_have_zoned_times(chats):
    client, saved = chats
    body = client.get(f"/conversations/{saved['conversation_id']}").json()
    assert is_aware_utc(body["created_at"])
    assert [is_aware_utc(m["created_at"]) for m in body["messages"]] == [True, True]
