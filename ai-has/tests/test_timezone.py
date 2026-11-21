from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app import analytics, main, tickets, timeutil

KNOWN = {"UTC", "Etc/UTC", "Asia/Singapore", "America/New_York"}
INJECTION = "Asia/Singapore'; drop table users; --"


class TzEngine:
    """Stands in for pg_timezone_names. Records every statement and its parameters."""

    def __init__(self):
        self.calls = []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, stmt, params=None):
        self.calls.append((" ".join(str(stmt).split()), params))
        return type("R", (), {"scalar": lambda s: 1 if params["tz"] in KNOWN else None})()


@pytest.fixture
def pg(monkeypatch):
    engine = TzEngine()
    monkeypatch.setattr(timeutil, "engine", engine)
    return engine


# ---------- the check ----------
def test_no_zone_means_utc_without_asking_the_database(pg):
    assert timeutil.check_timezone(None) == "UTC"
    assert pg.calls == []


def test_a_known_zone_is_accepted_after_a_bound_lookup(pg):
    assert timeutil.check_timezone("Asia/Singapore") == "Asia/Singapore"
    ((sql, params),) = pg.calls
    assert sql == "select 1 from pg_timezone_names where name = :tz"
    assert params == {"tz": "Asia/Singapore"}


@pytest.mark.parametrize("bad", ["Mars/Olympus", "asia/singapore", "+08:00", INJECTION, "UTC' or '1'='1", "Asia/Singapore\n"])
def test_an_unknown_zone_is_refused(pg, bad):
    with pytest.raises(ValueError):
        timeutil.check_timezone(bad)
    # it was only ever looked up as a bound value, never added to the SQL text
    assert all(bad not in sql for sql, _ in pg.calls)


@pytest.mark.parametrize("bad", ["", "x" * 65])
def test_an_empty_or_over_long_name_is_refused_without_a_lookup(pg, bad):
    with pytest.raises(ValueError):
        timeutil.check_timezone(bad)
    assert pg.calls == []


# ---------- the queries only use :tz as a bound parameter ----------
def test_no_query_has_a_zone_name_or_an_f_string_value_in_its_text():
    queries = [
        analytics.DAILY_SQL, analytics.TOTALS_SQL, analytics.ARTICLES_SQL, analytics.ESCALATED_SQL,
    ]
    for query in queries:
        sql = str(query)
        assert ":tz" in sql
        assert "Singapore" not in sql and "{" not in sql and "}" not in sql
        assert "at time zone 'UTC'" in sql  # the only literal zone is UTC, for the stored values


def test_the_ticket_stats_query_binds_the_zone(monkeypatch):
    seen = []

    class Engine:
        @contextmanager
        def connect(self):
            yield self

        def execute(self, stmt, params=None):
            seen.append((str(stmt), params))
            row = {"total": 1, "open": 1, "in_progress": 0, "resolved": 0, "today": 1, "avg_hours": None}
            rows = [{"priority": "low", "n": 1}]
            return type("R", (), {
                "mappings": lambda s: type("M", (), {"one": lambda m: row, "all": lambda m: rows})()
            })()

    monkeypatch.setattr(tickets, "engine", Engine())
    assert tickets.get_stats("Asia/Singapore")["today"] == 1
    sql, params = seen[0]
    assert params == {"tz": "Asia/Singapore"} and ":tz" in sql and "Singapore" not in sql
    assert seen[1][1] is None or "tz" not in (seen[1][1] or {})


# ---------- the routes ----------
@pytest.fixture
def client(pg, monkeypatch, login_as):
    calls = {"stats": [], "analytics": []}
    monkeypatch.setattr(main, "get_stats", lambda tz="UTC": calls["stats"].append(tz) or {"total": 0})
    monkeypatch.setattr(analytics, "compute", lambda days, tz="UTC": calls["analytics"].append(tz) or {"days": days})
    login_as("agent")
    c = TestClient(main.app)
    c.calls = calls
    return c


@pytest.mark.parametrize("path, key", [("/stats", "stats"), ("/stats/analytics", "analytics")])
def test_the_routes_default_to_utc(client, path, key):
    assert client.get(path).status_code == 200
    assert client.calls[key] == ["UTC"]


@pytest.mark.parametrize("path, key", [("/stats", "stats"), ("/stats/analytics", "analytics")])
def test_the_routes_pass_a_valid_zone_through(client, path, key):
    assert client.get(path, params={"tz": "Asia/Singapore"}).status_code == 200
    assert client.calls[key] == ["Asia/Singapore"]


@pytest.mark.parametrize("path, key", [("/stats", "stats"), ("/stats/analytics", "analytics")])
@pytest.mark.parametrize("bad", ["Mars/Olympus", INJECTION, "x" * 65])
def test_the_routes_refuse_an_unknown_zone_with_422(client, pg, path, key, bad):
    res = client.get(path, params={"tz": bad})
    assert res.status_code == 422
    assert client.calls[key] == []  # nothing was computed
    assert all(bad not in sql for sql, _ in pg.calls)


def test_a_zone_does_not_widen_who_can_read_the_stats(pg, monkeypatch, login_as):
    monkeypatch.setattr(main, "get_stats", lambda tz="UTC": {"total": 0})
    login_as("employee")
    assert TestClient(main.app).get("/stats", params={"tz": "Asia/Singapore"}).status_code == 403
