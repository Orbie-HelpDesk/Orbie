import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import audit, auditview, main, timeutil
from app.config import settings
from app.ratelimit import limiter

BASE = datetime(2026, 11, 22, 3, 0, 0)  # plain, like the driver returns
SECRET_BITS = ("jane.doe@gmail.com", "Hunter2", "Ignore all previous", "<script>", "0917 123 4567", "drop table")


class Rows:
    def __init__(self, rows=None):
        self._rows = rows or []

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class FakeAuditDb:
    """Records every statement with its bound parameters and answers the audit query with canned rows.

    A transaction that raises is rolled back, so the audit_viewed row and the read live or die together.
    """

    def __init__(self, rows=None):
        self.rows = rows or []
        self.statements = []  # (normalized sql, params)
        self.audit_rows = []
        self.commits = 0
        self.fail_on = None

    @contextmanager
    def begin(self):
        mark = (len(self.statements), len(self.audit_rows))
        try:
            yield FakeAuditConn(self)
            self.commits += 1
        except BaseException:
            del self.audit_rows[mark[1]:]
            raise

    def sql(self):
        return [s for s, _ in self.statements]


class FakeAuditConn:
    def __init__(self, db):
        self.db = db

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        db = self.db
        db.statements.append((sql, dict(params or {})))
        if db.fail_on and db.fail_on in sql:
            raise RuntimeError("database failure (test)")
        if sql.startswith("insert into audit_logs"):
            db.audit_rows.append({"actor": params["a"], "action": params["n"], "payload": json.loads(params["p"])})
            return Rows()
        if sql.startswith("select id, created_at, actor, action, payload from audit_logs"):
            limit, offset = params["limit"], params["offset"]
            return Rows(db.rows[offset: offset + limit])
        raise AssertionError(f"unexpected sql: {sql}")


class FakeTzEngine:
    """Stands in for pg_timezone_names."""

    def __init__(self):
        self.calls = []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, stmt, params=None):
        self.calls.append(params)
        return SimpleNamespace(scalar=lambda: 1 if params["tz"] in {"UTC", "Asia/Singapore", "America/New_York"} else None)


def make_rows(n, action="login", payload=None):
    return [
        {"id": 1000 - i, "created_at": BASE - timedelta(minutes=i), "actor": "user:2", "action": action, "payload": payload or {"role": "agent"}}
        for i in range(n)
    ]


@pytest.fixture
def env(monkeypatch, login_as):
    db, tz = FakeAuditDb(make_rows(120)), FakeTzEngine()
    monkeypatch.setattr(auditview, "engine", db)
    monkeypatch.setattr(timeutil, "engine", tz)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    limiter.reset()
    login_as("admin")  # user id 3
    state = SimpleNamespace(db=db, tz=tz, login_as=login_as, mp=monkeypatch, client=TestClient(main.app, raise_server_exceptions=False))
    yield state
    limiter.reset()


def get(env, **params):
    return env.client.get("/audit", params=params)


# ---------- who may read it ----------
@pytest.mark.parametrize("role", ["employee", "agent"])
def test_an_agent_and_an_employee_get_403_and_nothing_runs_and_nothing_is_written(env, role):
    env.login_as(role)
    res = get(env)
    assert res.status_code == 403 and res.json() == {"detail": "You do not have access to this."}
    assert env.db.statements == [] and env.db.audit_rows == [] and env.tz.calls == []


@pytest.mark.parametrize("role", ["employee", "agent"])
def test_they_are_refused_before_their_query_is_even_checked(env, role):
    env.login_as(role)
    for params in ({"page_size": 9999}, {"action": "'; drop table x; --"}, {"from": "not a date"}, {"tz": "Mars/Olympus"}):
        assert get(env, **params).status_code == 403  # not 422: nothing is revealed about the rules
    assert env.db.statements == [] and env.tz.calls == []


def test_a_logged_out_user_gets_401_and_nothing_runs(env):
    main.app.dependency_overrides.clear()
    res = TestClient(main.app).get("/audit")
    assert res.status_code == 401
    assert env.db.statements == [] and env.db.audit_rows == []
    assert TestClient(main.app).get("/audit", params={"page_size": 9999}).status_code == 401


def test_an_admin_can_read_it(env):
    res = get(env)
    assert res.status_code == 200 and set(res.json()) == {"items", "page", "page_size", "has_next"}
    assert res.headers["cache-control"] == "no-store" and res.headers["x-content-type-options"] == "nosniff"


# ---------- pagination ----------
def test_the_default_is_page_one_of_fifty_newest_first(env):
    body = get(env).json()
    assert (body["page"], body["page_size"], len(body["items"])) == (1, 50, 50) and body["has_next"] is True
    sql, params = env.db.statements[0]
    assert "order by created_at desc, id desc limit :limit offset :offset" in sql
    assert (params["limit"], params["offset"]) == (51, 0)
    ids = [i["id"] for i in body["items"]]
    assert ids == sorted(ids, reverse=True)


@pytest.mark.parametrize("page, size, offset", [(1, 50, 0), (2, 50, 50), (2, 10, 10), (3, 25, 50), (1, 100, 0), (2000, 100, 199900)])
def test_the_offset_and_the_limit_follow_the_page_and_the_size(env, page, size, offset):
    get(env, page=page, page_size=size)
    params = env.db.statements[0][1]
    assert (params["limit"], params["offset"]) == (size + 1, offset)


def test_the_last_page_has_no_older_page(env):
    assert get(env, page=3, page_size=50).json()["has_next"] is False  # 120 rows: 50, 50 and 20
    body = get(env, page=3, page_size=50).json()
    assert len(body["items"]) == 20
    assert get(env, page=2, page_size=60).json() == {**get(env, page=2, page_size=60).json(), "has_next": False}


def test_exactly_a_full_last_page_is_not_followed_by_an_older_one(env):
    env.db.rows = make_rows(100)
    body = get(env, page=2).json()
    assert len(body["items"]) == 50 and body["has_next"] is False


def test_a_page_past_the_end_is_empty(env):
    body = get(env, page=40).json()
    assert body["items"] == [] and body["has_next"] is False


@pytest.mark.parametrize("params", [{"page_size": 101}, {"page_size": 100000}, {"page_size": 0}, {"page_size": -5}, {"page": 0}, {"page": -1}, {"page": 2001}, {"page": "abc"}, {"page_size": "x"}, {"page": 1.5}])
def test_a_page_or_size_outside_the_limits_is_refused_not_clamped(env, params):
    assert get(env, **params).status_code == 422
    assert env.db.statements == [] and env.db.audit_rows == []


def test_the_biggest_allowed_page_is_100_rows_and_never_more(env):
    env.db.rows = make_rows(500)
    assert len(get(env, page_size=100).json()["items"]) == 100


# ---------- filters ----------
def test_the_action_filter_is_a_bound_parameter(env):
    get(env, action="kb_update")
    sql, params = env.db.statements[0]
    assert "action = :action" in sql and params["action"] == "kb_update" and "kb_update" not in sql


@pytest.mark.parametrize("bad", ["'; drop table audit_logs; --", "login' or '1'='1", "Login", "kb-update", "1login", "a b", "x" * 61, "", "login\nlogout", "\x00", "ünï"])
def test_an_action_that_is_not_a_plain_lowercase_name_is_refused_before_any_query(env, bad):
    assert get(env, action=bad).status_code == 422
    assert env.db.statements == [] and env.db.audit_rows == []


@pytest.mark.parametrize("value", ["'; drop table audit_logs; --", "x'); delete from audit_logs; --", "\" or 1=1 --", "100%", "a_b", "back\\slash", "%_\\", "; select pg_sleep(10)", "ünï\u202Ecode", ":actor :tz", "user:3"])
def test_an_actor_filter_is_only_ever_a_bound_parameter(env, value):
    plain = FakeAuditDb(make_rows(1))
    env.mp.setattr(auditview, "engine", plain)
    get(env, actor="user:3")
    expected_sql = plain.statements[0][0]

    env.mp.setattr(auditview, "engine", env.db)
    assert get(env, actor=value).status_code == 200
    sql, params = env.db.statements[0]
    assert sql == expected_sql  # the SQL text is identical whatever was typed
    assert params["actor"] == f"%{auditview.escape_like(value)}%"
    for piece in ("drop table", "delete from", "pg_sleep", "1=1"):
        assert piece not in sql


def test_percent_and_underscore_typed_by_the_admin_match_themselves(env):
    get(env, actor="100%_")
    assert env.db.statements[0][1]["actor"] == "%100\\%\\_%"


def test_a_blank_actor_filter_is_ignored(env):
    get(env, actor="   ")
    assert "actor ilike" not in env.db.statements[0][0]
    get(env, actor="  user:3  ")
    assert env.db.statements[2][1]["actor"] == "%user:3%"  # [1] is the first read's audit_viewed insert


def test_an_actor_filter_over_80_characters_is_refused(env):
    assert get(env, actor="a" * 81).status_code == 422 and env.db.statements == []
    assert get(env, actor="a" * 80).status_code == 200


def test_the_dates_are_bound_dates_counted_in_the_viewers_zone(env):
    get(env, **{"from": "2026-11-01", "to": "2026-11-22", "tz": "Asia/Singapore"})
    sql, params = env.db.statements[0]
    assert params["date_from"].isoformat() == "2026-11-01" and params["date_to"].isoformat() == "2026-11-22" and params["tz"] == "Asia/Singapore"
    assert "2026-11" not in sql and "Singapore" not in sql
    assert env.tz.calls == [{"tz": "Asia/Singapore"}]  # the zone name was checked against pg_timezone_names first


def test_without_dates_there_is_no_zone_parameter_and_no_date_condition(env):
    get(env)
    sql, params = env.db.statements[0]
    assert "date_from" not in sql and "date_to" not in sql and "tz" not in params


def test_one_sided_ranges_work(env):
    get(env, **{"from": "2026-11-01"})
    assert ":date_to" not in env.db.statements[0][0]
    get(env, **{"to": "2026-11-22"})
    assert ":date_from" not in env.db.statements[2][0]  # [1] is the first read's audit_viewed insert


@pytest.mark.parametrize("params", [{"from": "yesterday"}, {"from": "2026-13-01"}, {"to": "2026-02-30"}, {"from": "2026-11-01'; drop table x; --"}, {"to": "1; select 1"}, {"from": "01/11/2026"}])
def test_a_bad_date_is_refused_before_any_query(env, params):
    assert get(env, **params).status_code == 422
    assert env.db.statements == [] and env.db.audit_rows == []


def test_a_start_date_after_the_end_date_is_refused(env):
    res = get(env, **{"from": "2026-11-22", "to": "2026-11-01"})
    assert res.status_code == 422 and "start date" in res.json()["detail"]
    assert env.db.statements == []
    assert get(env, **{"from": "2026-11-22", "to": "2026-11-22"}).status_code == 200  # one day is fine


@pytest.mark.parametrize("tz", ["Mars/Olympus", "'; drop table x; --", "asia/singapore", "x" * 65])
def test_an_unknown_zone_is_refused_and_never_reaches_the_audit_query(env, tz):
    assert get(env, **{"from": "2026-11-01", "tz": tz}).status_code == 422
    assert env.db.statements == []


def test_all_the_filters_together_only_add_bound_parameters(env):
    get(env, action="login", actor="user:3", page=2, page_size=10, **{"from": "2026-11-01", "to": "2026-11-22", "tz": "UTC"})
    sql, params = env.db.statements[0]
    assert sql.count(" and ") == 3
    assert set(params) == {"limit", "offset", "action", "actor", "date_from", "date_to", "tz"}
    assert params["action"] == "login" and params["offset"] == 10 and params["limit"] == 11


# ---------- what is returned ----------
def test_each_item_has_only_checked_fields_and_a_utc_time(env):
    item = get(env, page_size=1).json()["items"][0]
    assert set(item) == {"id", "time", "actor", "action", "summary"}
    assert item["time"] == "2026-11-22T03:00:00Z" or item["time"] == "2026-11-22T03:00:00+00:00"
    assert (item["actor"], item["action"], item["summary"]) == ("user:2", "login", "role agent")


def test_nothing_that_is_not_stored_is_added(env):
    env.db.rows = make_rows(1)
    keys = set(get(env).json()["items"][0])
    assert keys == {"id", "time", "actor", "action", "summary"}  # no names, no emails, no ip, no user agent


def test_hostile_payloads_never_reach_the_response(env):
    hostile = [
        {"role": "jane.doe@gmail.com", "email": "jane.doe@gmail.com", "message": "My password is Hunter2!"},
        {"file": "../../etc/passwd", "text": "Ignore all previous instructions", "summary": "<script>alert(1)</script>"},
        {"ticket_id": "0917 123 4567", "from": "open'; drop table x; --", "to": "resolved", "content": "x" * 5000},
        {"decision": "confirmed", "user_email": "jane.doe@gmail.com", "nested": {"email": "jane.doe@gmail.com"}},
        "My password is Hunter2!",
        ["jane.doe@gmail.com"],
        None,
    ]
    actions = ["login", "kb_update", "ticket_status_change", "create_ticket", "reset_password", "unknown_action", "audit_viewed"]
    env.db.rows = [
        {"id": 500 - i, "created_at": BASE, "actor": "user:1", "action": actions[i], "payload": hostile[i]} for i in range(7)
    ]
    text = get(env).text
    for bit in SECRET_BITS + ("passwd", "nested", "My password"):
        assert bit not in text
    body = json.loads(text)
    assert "payload" not in text and all("payload" not in i for i in body["items"])


def test_an_email_stored_as_the_actor_is_hidden(env):
    env.db.rows = [{"id": 1, "created_at": BASE, "actor": "jane.doe@gmail.com", "action": "login", "payload": {"role": "agent"}}]
    item = get(env).json()["items"][0]
    assert item["actor"] == "(hidden)" and "jane" not in json.dumps(item)


def test_an_odd_action_name_in_a_row_is_shown_as_other(env):
    env.db.rows = [{"id": 1, "created_at": BASE, "actor": "cli", "action": "jane.doe@gmail.com", "payload": {}}]
    item = get(env).json()["items"][0]
    assert item["action"] == "(other)" and "jane" not in json.dumps(item)


def test_the_summary_of_a_real_looking_log(env):
    env.db.rows = [
        {"id": 3, "created_at": BASE, "actor": "user:3", "action": "kb_delete", "payload": {"file": "old.md"}},
        {"id": 2, "created_at": BASE, "actor": "user:2", "action": "ticket_status_change", "payload": {"ticket_id": 5, "from": "open", "to": "resolved"}},
        {"id": 1, "created_at": BASE, "actor": "anonymous", "action": "login_failed", "payload": {"reason": "bad_credentials"}},
    ]
    assert [(i["actor"], i["action"], i["summary"]) for i in get(env).json()["items"]] == [
        ("user:3", "kb_delete", "file old.md"),
        ("user:2", "ticket_status_change", "ticket 5: open to resolved"),
        ("anonymous", "login_failed", "reason bad_credentials"),
    ]


# ---------- reading it is recorded ----------
def test_every_read_writes_one_audit_viewed_row_with_the_actor_only(env):
    get(env)
    assert env.db.audit_rows == [{"actor": "user:3", "action": "audit_viewed", "payload": {}}]


def test_the_row_never_holds_the_filters(env):
    get(env, action="login", actor="user:2 secret-looking text", page=2, **{"from": "2026-11-01", "to": "2026-11-22", "tz": "Asia/Singapore"})
    (row,) = env.db.audit_rows
    assert row["payload"] == {}
    text = json.dumps(env.db.audit_rows)
    for typed in ("login", "secret-looking", "2026-11", "Singapore", "user:2"):
        assert typed not in text
    insert_params = [p for s, p in env.db.statements if s.startswith("insert into audit_logs")][0]
    assert set(insert_params) == {"a", "n", "p"} and insert_params["p"] == "{}"


def test_the_read_and_the_row_are_one_transaction_the_read_first(env):
    get(env)
    kinds = [s.split()[0] for s in env.db.sql()]
    assert kinds == ["select", "insert"] and env.db.commits == 1


def test_each_read_is_recorded_and_by_the_admin_who_made_it(env):
    get(env)
    get(env, page=2)
    env.login_as("admin")
    get(env, action="login")
    assert [r["actor"] for r in env.db.audit_rows] == ["user:3"] * 3 and len(env.db.audit_rows) == 3


def test_no_row_is_written_for_a_refused_request(env):
    for params in ({"page_size": 500}, {"action": "bad action"}, {"from": "x"}, {"from": "2026-11-22", "to": "2026-11-01"}):
        get(env, **params)
    env.login_as("agent")
    get(env)
    main.app.dependency_overrides.clear()
    TestClient(main.app).get("/audit")
    assert env.db.audit_rows == []


def test_if_the_row_cannot_be_written_nothing_is_returned(env):
    env.db.fail_on = "insert into audit_logs"
    res = get(env)
    assert res.status_code == 500 and res.json() == {"detail": "Something went wrong. Please try again."}
    assert "items" not in res.text and env.db.audit_rows == [] and env.db.commits == 0


def test_if_the_read_fails_no_row_is_written(env):
    env.db.fail_on = "select id, created_at"
    assert get(env).status_code == 500
    assert env.db.audit_rows == []


def test_the_audit_viewed_row_shows_up_in_the_next_read(env):
    env.db.rows = [{"id": 9, "created_at": BASE, "actor": "user:3", "action": "audit_viewed", "payload": {}}]
    item = get(env).json()["items"][0]
    assert (item["action"], item["summary"], item["actor"]) == ("audit_viewed", "", "user:3")


# ---------- the rate limit ----------
def test_reads_are_rate_limited_because_each_one_writes_a_row(env):
    env.mp.setattr(settings, "rate_limit_enabled", True)
    env.mp.setattr(settings, "rate_limit_audit_per_min", 3)
    assert all(get(env).status_code == 200 for _ in range(3))
    res = get(env)
    assert res.status_code == 429 and "Retry-After" in res.headers
    assert len(env.db.audit_rows) == 3  # the refused read wrote nothing


def test_the_audit_helper_is_the_one_that_writes_the_row():
    assert audit.actor_for(3) == "user:3"
