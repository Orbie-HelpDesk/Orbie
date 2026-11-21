import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auditview, auth, main
from app.config import settings
from app.loginlimit import login_limiter
from fake_auth_db import PASSWORD, FakeAuthDb

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
EMAILS = {"employee": "employee@example.com", "agent": "agent@example.com", "admin": "admin@example.com"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    folder = tmp_path / "audit-page"
    folder.mkdir()
    (folder / "audit.html").write_text("<h1>audit log page</h1>", encoding="utf-8")
    monkeypatch.setattr(main, "AUDIT_PAGE_DIR", folder)
    monkeypatch.setattr(settings, "session_secret", "unit-test-secret-" + "x" * 32)
    login_limiter.reset()
    monkeypatch.setattr(auth, "engine", FakeAuthDb())
    return TestClient(main.app, follow_redirects=False)


def logged_in(client, role):
    assert client.post("/auth/login", json={"email": EMAILS[role], "password": PASSWORD}).status_code == 200
    return client


# ---------- the page ----------
def test_a_logged_out_visitor_is_sent_to_the_login_page_and_back(client):
    res = client.get("/admin/audit")
    assert res.status_code == 302 and res.headers["location"] == "/login?next=/admin/audit"
    assert "audit log page" not in res.text


def test_an_admin_gets_the_page(client):
    res = logged_in(client, "admin").get("/admin/audit")
    assert res.status_code == 200 and "<h1>audit log page</h1>" in res.text and "no-cache" in res.headers["cache-control"]


@pytest.mark.parametrize("role", ["agent", "employee"])
def test_an_agent_and_an_employee_get_the_403_page(client, role):
    res = logged_in(client, role).get("/admin/audit")
    assert res.status_code == 403
    assert "No access" in res.text and "audit log is for admins only" in res.text and "<h1>audit log page</h1>" not in res.text


def test_the_dashboard_stays_open_to_agents_but_the_audit_page_does_not(client, monkeypatch, tmp_path):
    admin_dir = tmp_path / "admin-dir"
    admin_dir.mkdir()
    (admin_dir / "admin.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
    monkeypatch.setattr(main, "ADMIN_DIR", admin_dir)
    logged_in(client, "agent")
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/audit").status_code == 403


def test_the_page_answer_has_the_security_headers(client):
    assert logged_in(client, "agent").get("/admin/audit").headers["x-content-type-options"] == "nosniff"


# ---------- the link ----------
def test_the_dashboard_link_is_hidden_and_only_the_session_script_shows_it_to_an_admin():
    html = (FRONTEND / "admin" / "admin.html").read_text(encoding="utf-8")
    assert re.search(r'<a[^>]*id="auditLink"[^>]*href="/admin/audit"[^>]*\bhidden\b', html)
    script = (FRONTEND / "chat" / "session.js").read_text(encoding="utf-8")
    assert 'if (auditLink && user.role === "admin") auditLink.hidden = false;' in script
    assert "auditLink" not in (FRONTEND / "chat" / "index.html").read_text(encoding="utf-8")  # not in the chat header


# ---------- the page files ----------
def test_the_page_uses_the_shared_session_script_and_both_themes():
    html = (FRONTEND / "audit" / "audit.html").read_text(encoding="utf-8")
    assert 'src="/static/session.js" data-then="/audit-assets/audit.js"' in html
    assert "dataset.theme" in html and "prefers-color-scheme" in html
    for name in ("audit.css", "audit.js"):
        assert (FRONTEND / "audit" / name).is_file()
    css = (FRONTEND / "audit" / "audit.css").read_text(encoding="utf-8")
    assert "@media (max-width" in css and "table-wrap" in html  # works on a phone


def test_the_filters_are_the_ones_the_api_accepts():
    html = (FRONTEND / "audit" / "audit.html").read_text(encoding="utf-8")
    for field in ('id="action"', 'id="actor"', 'id="dateFrom"', 'id="dateTo"'):
        assert field in html
    assert 'maxlength="80"' in html  # the same limit as the API's actor filter
    assert auditview.MAX_ACTOR_FILTER == 80


def test_the_action_list_matches_the_actions_the_server_knows():
    script = (FRONTEND / "audit" / "audit.js").read_text(encoding="utf-8")
    listed = set(re.findall(r'"([a-z_]+)"', script.split("const ACTIONS = [")[1].split("];")[0]))
    assert listed == set(auditview.KNOWN_ACTIONS)


def test_the_script_asks_for_no_more_than_the_default_page_and_never_for_a_bigger_one():
    script = (FRONTEND / "audit" / "audit.js").read_text(encoding="utf-8")
    assert "page_size" not in script  # always the default 50, so the hard maximum is never in play
    assert '"50 per page"' in script or "50 per page" in script


def test_the_zone_is_sent_only_together_with_a_date():
    script = (FRONTEND / "audit" / "audit.js").read_text(encoding="utf-8")
    assert "if ((from || to) && browserZone()) filters.tz = browserZone();" in script


def test_times_are_shown_in_the_viewers_local_time_with_the_exact_utc_time_on_hover():
    script = (FRONTEND / "audit" / "audit.js").read_text(encoding="utf-8")
    assert "toLocaleString" in script and "when.title = item.time" in script
    assert "text + \"Z\"" in script  # a time with no zone is read as UTC, never as local


def test_the_script_puts_data_on_the_page_with_text_not_html():
    script = (FRONTEND / "audit" / "audit.js").read_text(encoding="utf-8")
    assert "innerHTML = " not in script.replace('innerHTML = ""', "")
    assert script.count("textContent") >= 5


def test_the_page_says_that_reading_the_log_is_recorded():
    html = (FRONTEND / "audit" / "audit.html").read_text(encoding="utf-8")
    assert "audit_viewed" in html and "never emails, passwords or message text" in html
