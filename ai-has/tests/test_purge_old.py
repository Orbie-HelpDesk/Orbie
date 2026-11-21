import json
from contextlib import contextmanager

import pytest

from app import audit, purge_old
from app.config import settings

LOCAL = "postgresql+psycopg2://aihas:x@db:5432/aihas"
REMOTE = "postgresql+psycopg2://u:p@db.supabase.co:5432/postgres"


class Result:
    def __init__(self, n):
        self.rowcount, self._n = n, n

    def scalar(self):
        return self._n


class FakeEngine:
    """Records the statements. Returns 7 messages and 3 titles."""

    def __init__(self):
        self.calls, self.audit, self.transactions = [], [], 0

    @contextmanager
    def begin(self):
        self.transactions += 1
        yield self

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "insert into audit_logs" in sql:
            self.audit.append({"actor": params["a"], "action": params["n"], "payload": json.loads(params["p"])})
            return Result(1)
        self.calls.append((" ".join(sql.split()), params))
        flat = " ".join(sql.split())
        return Result(7 if flat.startswith(("update messages", "select count(*) from messages")) else 3)


@pytest.fixture
def db(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(purge_old, "engine", fake)
    monkeypatch.setattr(audit, "engine", fake)
    monkeypatch.setattr(settings, "database_url", LOCAL)
    return fake


def test_purge_replaces_the_text_and_the_titles_and_audits_in_one_transaction(db, capsys):
    assert purge_old.main(["--days", "90"]) == 0
    sqls = [c[0] for c in db.calls]
    assert sqls[0].startswith("update conversations set title = :p")  # titles first, found through the old messages
    assert sqls[1].startswith("update messages set content = :p")
    assert all(p["d"] == 90 and p["p"] == "[purged]" for _, p in db.calls)
    assert db.transactions == 1
    assert db.audit == [{"actor": "cli", "action": "messages_purged", "payload": {"days": 90, "messages": 7, "conversations": 3}}]
    assert "Purged 7 message(s) and 3 title(s) older than 90 days." in capsys.readouterr().out


def test_it_only_touches_text_and_titles_never_rows_or_numbers(db):
    purge_old.main(["--days", "30"])
    for sql, _ in db.calls:
        assert sql.startswith("update ") and " delete " not in sql
        assert "set content = :p" in sql or "set title = :p" in sql
        for kept in ("citations", "confidence", "latency_ms", "feedback", "escalated", "intent"):
            assert kept not in sql.split(" where ")[0]


def test_already_purged_and_blocked_placeholders_are_skipped(db):
    purge_old.main(["--days", "30"])
    messages_sql, params = [c for c in db.calls if "update messages" in c[0]][0]
    assert "content not in (:p, :b)" in messages_sql and params["b"] == "[blocked message]"
    titles_sql, tparams = [c for c in db.calls if "update conversations" in c[0]][0]
    assert "title is distinct from :p" in titles_sql and "title <> :k" in titles_sql
    assert tparams["k"] == "Blocked message"


def test_dry_run_counts_and_changes_nothing(db, capsys):
    assert purge_old.main(["--days", "30", "--dry-run"]) == 0
    assert all(sql.startswith("select count(*)") for sql, _ in db.calls)
    assert db.audit == []
    assert "Would purge" in capsys.readouterr().out


@pytest.mark.parametrize("days", ["0", "-5", "abc"])
def test_days_must_be_a_positive_number(db, days):
    with pytest.raises(SystemExit):
        purge_old.main(["--days", days])
    assert db.calls == []


def test_days_is_required(db):
    with pytest.raises(SystemExit):
        purge_old.main([])


def test_it_refuses_a_remote_database_without_the_flag(db, monkeypatch):
    monkeypatch.setattr(settings, "database_url", REMOTE)
    with pytest.raises(SystemExit) as stop:
        purge_old.main(["--days", "30"])
    assert "Refusing to write" in str(stop.value) and "p@" not in str(stop.value)
    assert db.calls == [] and db.audit == []


def test_a_remote_database_needs_the_explicit_flag(db, monkeypatch):
    monkeypatch.setattr(settings, "database_url", REMOTE)
    assert purge_old.main(["--days", "30", "--allow-remote"]) == 0
    assert db.calls


def test_a_missing_host_is_refused_too(db, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@/x")
    with pytest.raises(SystemExit):
        purge_old.main(["--days", "30"])
    assert db.calls == []
