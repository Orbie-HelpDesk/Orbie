import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import audit, ingest, kb, main, rag
from app.config import settings
from fake_kb_db import FakeKbDb

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
REMOTE = "postgresql+psycopg2://u:p@db.supabase.co:5432/postgres"


@pytest.fixture
def pages(monkeypatch, tmp_path, login_as):
    folder = tmp_path / "kb-page"
    folder.mkdir()
    (folder / "kb.html").write_text("<h1>knowledge base page</h1>", encoding="utf-8")
    monkeypatch.setattr(main, "KB_PAGE_DIR", folder)
    return TestClient(main.app, follow_redirects=False), login_as


# ---------- the page ----------
def test_a_logged_out_visitor_is_sent_to_the_login_page_and_back(pages):
    client, _ = pages
    main.app.dependency_overrides.clear()
    res = client.get("/admin/kb")
    assert res.status_code == 302 and res.headers["location"] == "/login?next=/admin/kb"
    assert "knowledge base page" not in res.text


def session_client(pages_client, monkeypatch, role):
    """A client with a real signed session cookie for that role (the page route reads the cookie directly)."""
    from app import auth
    from app.loginlimit import login_limiter
    from fake_auth_db import PASSWORD, FakeAuthDb

    monkeypatch.setattr(settings, "session_secret", "unit-test-secret-" + "x" * 32)
    login_limiter.reset()
    db = FakeAuthDb()
    monkeypatch.setattr(auth, "engine", db)
    monkeypatch.setattr(audit, "engine", db)
    emails = {"employee": "employee@example.com", "agent": "agent@example.com", "admin": "admin@example.com"}
    pages_client.post("/auth/login", json={"email": emails[role], "password": PASSWORD})
    return pages_client


def test_an_admin_gets_the_page(pages, monkeypatch):
    client = session_client(pages[0], monkeypatch, "admin")
    res = client.get("/admin/kb")
    assert res.status_code == 200 and "knowledge base page" in res.text and "no-cache" in res.headers["cache-control"]


@pytest.mark.parametrize("role", ["agent", "employee"])
def test_an_agent_and_an_employee_get_the_403_page(pages, monkeypatch, role):
    client = session_client(pages[0], monkeypatch, role)
    res = client.get("/admin/kb")
    assert res.status_code == 403
    assert "No access" in res.text and "admins only" in res.text and "<h1>knowledge base page</h1>" not in res.text


def test_the_dashboard_stays_open_to_agents_and_the_knowledge_base_page_does_not(pages, monkeypatch, tmp_path):
    admin_dir = tmp_path / "admin-dir"
    admin_dir.mkdir()
    (admin_dir / "admin.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
    monkeypatch.setattr(main, "ADMIN_DIR", admin_dir)
    client = session_client(pages[0], monkeypatch, "agent")
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/kb").status_code == 403


def test_the_security_headers_are_on_the_page_answers(pages, monkeypatch):
    client = session_client(pages[0], monkeypatch, "agent")
    assert client.get("/admin/kb").headers["x-content-type-options"] == "nosniff"


# ---------- the link on the dashboard ----------
def test_the_dashboard_link_is_hidden_until_the_session_script_shows_it_to_an_admin():
    html = (FRONTEND / "admin" / "admin.html").read_text(encoding="utf-8")
    assert re.search(r'<a[^>]*id="kbLink"[^>]*href="/admin/kb"[^>]*\bhidden\b', html) or re.search(
        r'<a[^>]*href="/admin/kb"[^>]*id="kbLink"[^>]*\bhidden\b', html
    )
    script = (FRONTEND / "chat" / "session.js").read_text(encoding="utf-8")
    assert re.search(r'kbLink[^\n]*\n[^\n]*user\.role === "admin"', script) or 'user.role === "admin"' in script
    # agents are shown the dashboard link only: the knowledge base link needs the admin role by itself
    assert 'if (kbLink && user.role === "admin") kbLink.hidden = false;' in script


def test_the_page_files_exist_and_use_the_shared_session_script():
    html = (FRONTEND / "kb" / "kb.html").read_text(encoding="utf-8")
    assert 'src="/static/session.js" data-then="/kb-assets/kb.js"' in html
    for name in ("kb.css", "kb.js"):
        assert (FRONTEND / "kb" / name).is_file()
    assert "dataset.theme" in html and "prefers-color-scheme" in html  # dark and light mode, like the other pages


def test_the_page_script_never_builds_html_from_article_text():
    script = (FRONTEND / "kb" / "kb.js").read_text(encoding="utf-8")
    assert "innerHTML = " not in script.replace('innerHTML = ""', "")  # text goes in with textContent or value


# ---------- /kb/info ----------
@pytest.fixture
def info_client(monkeypatch, login_as):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    return TestClient(main.app), login_as


def test_info_is_for_admins_only(info_client):
    client, login_as = info_client
    for role in ("employee", "agent"):
        login_as(role)
        assert client.get("/kb/info").status_code == 403
    main.app.dependency_overrides.clear()
    assert client.get("/kb/info").status_code == 401


def test_info_reports_the_limits_and_that_a_local_database_can_be_edited(info_client, monkeypatch):
    client, login_as = info_client
    login_as("admin")
    monkeypatch.setattr(settings, "kb_max_bytes", 1234)
    monkeypatch.setattr(settings, "kb_max_articles", 7)
    body = client.get("/kb/info").json()
    assert body["max_bytes"] == 1234 and body["max_articles"] == 7 and body["max_distance"] == rag.MAX_DISTANCE
    assert body["editable"] is True and body["name_pattern"] == kb.NAME_RE.pattern


def test_info_says_changes_are_off_on_a_shared_database_until_the_switch_is_on(info_client, monkeypatch):
    client, login_as = info_client
    login_as("admin")
    monkeypatch.setattr(settings, "database_url", REMOTE)
    monkeypatch.setattr(settings, "kb_edit_allow_remote", False)
    body = client.get("/kb/info").json()
    assert body["editable"] is False and "supabase" not in str(body)
    monkeypatch.setattr(settings, "kb_edit_allow_remote", True)
    assert client.get("/kb/info").json()["editable"] is True


def test_info_is_not_mistaken_for_an_article_name(info_client, monkeypatch, tmp_path):
    client, login_as = info_client
    login_as("admin")
    monkeypatch.setattr(settings, "kb_dir", str(tmp_path))
    monkeypatch.setattr(kb, "engine", FakeKbDb())
    monkeypatch.setattr(ingest, "engine", FakeKbDb())
    assert "max_bytes" in client.get("/kb/info").json()
    assert client.get("/kb/info.md").status_code == 404  # a real article name still goes to the article route
