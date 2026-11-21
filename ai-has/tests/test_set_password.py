from contextlib import contextmanager

import pytest

from app import audit, passwords, set_password
from app.config import settings

LOCAL = "postgresql+psycopg2://aihas:x@db:5432/aihas"
REMOTE = "postgresql+psycopg2://u:p@db.supabase.co:5432/postgres"
GOOD = "a long enough password"


class FakeConn:
    def __init__(self, db):
        self.db = db

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "update users set password_hash" in sql:
            user = self.db.users.get(params["e"])
            if user is not None:
                self.db.hashes[user] = params["h"]
            return type("R", (), {"scalar": lambda s: user})()
        if "insert into audit_logs" in sql:
            self.db.audit.append(params)
            return None
        raise AssertionError(sql)


class FakeDb:
    def __init__(self):
        self.users = {"agent@example.com": 2}
        self.hashes, self.audit = {}, []

    @contextmanager
    def begin(self):
        yield FakeConn(self)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(set_password, "engine", fake)
    monkeypatch.setattr(audit, "engine", fake)
    monkeypatch.setattr(settings, "database_url", LOCAL)
    return fake


def typed(*answers):
    """A stand-in for getpass that returns the given answers in order and records the prompts."""
    prompts, queue = [], list(answers)

    def ask(prompt):
        prompts.append(prompt)
        return queue.pop(0)

    ask.prompts = prompts
    return ask


def run(argv, ask, tty=True):
    return set_password.main(argv, ask=ask, is_terminal=tty)


def test_the_password_is_set_from_a_hidden_prompt(db, capsys):
    ask = typed(GOOD, GOOD)
    assert run(["agent@example.com"], ask) == 0
    assert passwords.verify_password(GOOD, db.hashes[2])
    assert len(ask.prompts) == 2
    out = capsys.readouterr().out
    assert GOOD not in out and "agent@example.com" not in out and "user id 2" in out


def test_the_email_is_matched_ignoring_case(db):
    assert run(["  AGENT@Example.com "], typed(GOOD, GOOD)) == 0


def test_a_password_cannot_be_given_as_an_argument(db):
    with pytest.raises(SystemExit):
        run(["agent@example.com", "--password", GOOD], typed(GOOD, GOOD))
    with pytest.raises(SystemExit):
        run(["agent@example.com", GOOD], typed(GOOD, GOOD))  # an extra positional is not a password either
    assert db.hashes == {}


def test_it_refuses_to_run_without_a_terminal(db, capsys):
    ask = typed()
    assert run(["agent@example.com"], ask, tty=False) == 2
    assert ask.prompts == [] and db.hashes == {}
    assert "interactive terminal" in capsys.readouterr().out


def test_it_refuses_a_remote_database_without_the_flag(db, monkeypatch):
    monkeypatch.setattr(settings, "database_url", REMOTE)
    ask = typed(GOOD, GOOD)
    with pytest.raises(SystemExit) as stop:
        run(["agent@example.com"], ask)
    assert "Refusing to write" in str(stop.value)
    assert "p@" not in str(stop.value)  # the message never shows the URL or its password
    assert ask.prompts == [] and db.hashes == {}


def test_a_remote_database_needs_the_explicit_flag(db, monkeypatch):
    monkeypatch.setattr(settings, "database_url", REMOTE)
    assert run(["agent@example.com", "--allow-remote"], typed(GOOD, GOOD)) == 0
    assert 2 in db.hashes


def test_a_missing_host_is_refused_too(db, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@/x")
    with pytest.raises(SystemExit):
        run(["agent@example.com"], typed(GOOD, GOOD))


def test_a_weak_or_mismatched_password_changes_nothing(db, capsys):
    assert run(["agent@example.com"], typed("short")) == 1
    assert run(["agent@example.com"], typed(GOOD, GOOD + "x")) == 1
    assert db.hashes == {}
    out = capsys.readouterr().out
    assert "at least 8" in out and "do not match" in out


def test_an_unknown_user_is_not_created(db, capsys):
    assert run(["nobody@example.com"], typed(GOOD, GOOD)) == 1
    assert db.hashes == {} and "No user with that email" in capsys.readouterr().out


def test_the_change_is_audited_without_the_email_or_password(db):
    run(["agent@example.com"], typed(GOOD, GOOD))
    (row,) = db.audit
    assert row["a"] == "cli" and row["n"] == "password_set"
    assert row["p"] == '{"user_id": 2}'
