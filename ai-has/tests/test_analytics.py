from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import analytics, main

TODAY = date(2026, 11, 21)
TOTALS = {
    "today": TODAY, "answered": 6, "escalated": 2, "actions": 1, "blocked": 1,
    "median_ms": 850.4, "p95_ms": 2210.6, "up": 3, "down": 1,
}


def build(days=3, daily=(), totals=None, articles=(), escalated=()):
    return analytics.build(days, TODAY, list(daily), totals or TOTALS, list(articles), list(escalated))


def test_every_day_of_the_range_is_listed_oldest_first_with_zeros_filled_in():
    result = build(3, daily=[{"day": date(2026, 11, 19), "n": 4}, {"day": date(2026, 11, 21), "n": 2}])
    assert result["questions_per_day"] == [
        {"day": "2026-11-19", "count": 4},
        {"day": "2026-11-20", "count": 0},
        {"day": "2026-11-21", "count": 2},
    ]
    assert result["totals"]["questions"] == 6 and result["days"] == 3


def test_days_outside_the_range_are_ignored():
    result = build(2, daily=[{"day": date(2026, 10, 1), "n": 50}])
    assert result["totals"]["questions"] == 0


def test_deflection_is_answered_over_answered_plus_escalated():
    result = build()
    assert result["deflection_rate"] == 0.75  # 6 / (6 + 2): blocked and actions are not counted
    assert result["totals"] == {"questions": 0, "answered": 6, "escalated": 2, "actions_proposed": 1, "blocked": 1}


def test_thumbs_share_is_up_over_rated():
    assert build()["feedback"] == {"up": 3, "down": 1, "up_share": 0.75}


def test_latency_is_rounded_to_whole_milliseconds():
    assert build()["latency_ms"] == {"median": 850, "p95": 2211}


def test_no_data_gives_nulls_not_errors():
    empty = {**TOTALS, "answered": 0, "escalated": 0, "actions": 0, "blocked": 0, "median_ms": None, "p95_ms": None, "up": 0, "down": 0}
    result = build(totals=empty)
    assert result["deflection_rate"] is None
    assert result["latency_ms"] == {"median": None, "p95": None}
    assert result["feedback"]["up_share"] is None
    assert result["top_articles"] == [] and result["top_escalated_questions"] == []


def test_top_lists_keep_the_order_and_counts_of_the_rows():
    result = build(
        articles=[{"source": "vpn.md", "n": 9}, {"source": "wifi.md", "n": 4}],
        escalated=[{"question": "how do i get a parking pass", "n": 3}],
    )
    assert result["top_articles"] == [{"source": "vpn.md", "count": 9}, {"source": "wifi.md", "count": 4}]
    assert result["top_escalated_questions"] == [{"question": "how do i get a parking pass", "count": 3}]


# ---------- compute() runs the four queries ----------
class Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def one(self):
        return self.rows[0]


class FakeEngine:
    def __init__(self):
        self.calls = []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, stmt, params):
        sql = str(stmt)
        self.calls.append((sql, params))
        if "as today" in sql:
            return Rows([TOTALS])
        if "json_array_elements_text" in sql:
            return Rows([{"source": "vpn.md", "n": 2}])
        if "join lateral" in sql:
            return Rows([{"question": "parking pass", "n": 1}])
        return Rows([{"day": TODAY, "n": 5}])


def test_compute_sends_the_range_and_hides_purged_text(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(analytics, "engine", fake)
    result = analytics.compute(7)
    assert len(fake.calls) == 4 and all(p["d"] == 7 for _, p in fake.calls)
    escalated_params = [p for sql, p in fake.calls if "join lateral" in sql][0]
    assert escalated_params["purged"] == "[purged]"
    assert result["questions_per_day"][-1] == {"day": "2026-11-21", "count": 5}
    assert result["top_articles"][0]["source"] == "vpn.md"


def test_the_queries_never_ask_for_a_user_or_a_name():
    for query in (analytics.DAILY_SQL, analytics.TOTALS_SQL, analytics.ARTICLES_SQL, analytics.ESCALATED_SQL):
        sql = str(query).lower()
        assert "email" not in sql and "user_id" not in sql and "users" not in sql


# ---------- the route ----------
@pytest.fixture
def client(monkeypatch):
    seen = []
    monkeypatch.setattr(analytics, "compute", lambda days, tz="UTC": seen.append((days, tz)) or {"days": days, "tz": tz})
    c = TestClient(main.app)
    c.seen = seen
    return c


@pytest.mark.parametrize("role, status", [("employee", 403), ("agent", 200), ("admin", 200)])
def test_only_staff_can_read_the_analytics(client, login_as, role, status):
    login_as(role)
    assert client.get("/stats/analytics").status_code == status
    assert bool(client.seen) == (status == 200)


def test_logged_out_gets_401(client):
    assert client.get("/stats/analytics").status_code == 401


def test_the_period_defaults_to_30_days_and_is_validated(client, login_as):
    login_as("admin")
    assert client.get("/stats/analytics").json() == {"days": 30, "tz": "UTC"}
    assert client.get("/stats/analytics?days=90").json() == {"days": 90, "tz": "UTC"}
    for bad in ("0", "366", "-5", "abc"):
        assert client.get(f"/stats/analytics?days={bad}").status_code == 422
