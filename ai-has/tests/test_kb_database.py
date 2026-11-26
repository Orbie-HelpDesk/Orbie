"""KB_STORE=database, the guards that protect it, and the one-time import. No Gemini, no database."""
# ruff: noqa: F811  (env is the fixture imported from test_kb)
import pytest

from app import import_articles, ingest, kb
from app.config import Settings, settings
from test_kb import ARTICLE, REQUESTS, env, files, put  # noqa: F401  (env is a fixture)
from test_llm import unavailable

REMOTE = "postgresql+psycopg2://u:p@db.supabase.co:5432/postgres"
NEW = "# VPN troubleshooting\n\nReinstall the client.\n"


@pytest.fixture
def web(env):
    """The page keeps the text in the database. As on Vercel, there is no usable KB folder."""
    env.mp.setattr(settings, "kb_store", "database")
    env.mp.setattr(settings, "kb_dir", str(env.folder / "not-there"))
    env.mp.setattr(ingest, "BUNDLED_KB_DIR", env.folder / "not-there-either")
    return env


def text_of(env, source):
    return [d["content"] for d in env.db.docs.values() if d["source_path"] == source]


def test_the_default_is_files_and_an_unknown_value_is_refused():
    assert Settings.model_fields["kb_store"].default == "files"
    with pytest.raises(ValueError):
        Settings(kb_store="databse")


# ---------- create, read, update, delete ----------
def test_create_read_update_delete_without_any_file(web):
    created = put(web, "vpn.md")
    assert created.status_code == 201 and created.json()["created"] is True
    assert text_of(web, "vpn.md") == [ARTICLE]
    assert web.client.get("/kb/vpn.md").json()["content"] == ARTICLE
    (item,) = web.client.get("/kb").json()
    assert (item["source"], item["status"], item["size"]) == ("vpn.md", "indexed", len(ARTICLE))

    updated = put(web, "vpn.md", NEW)
    assert updated.status_code == 200 and updated.json()["created"] is False
    assert text_of(web, "vpn.md") == [NEW]  # one copy, with the new text
    assert [c["content"] for c in web.db.chunks_of("vpn.md")] == [NEW.strip()]

    assert web.client.request("DELETE", "/kb/vpn.md", json={}).status_code == 200
    assert web.db.docs == {} and web.db.chunks == [] and web.client.get("/kb/vpn.md").status_code == 404
    assert [a["action"] for a in web.db.audit] == ["kb_create", "kb_update", "kb_delete"]
    assert all(a["payload"] == {"file": a["payload"]["file"]} for a in web.db.audit)
    assert files(web) == []


def test_every_change_takes_the_lock(web):
    put(web, "vpn.md")
    web.client.request("DELETE", "/kb/vpn.md", json={})
    assert sum("pg_advisory_xact_lock" in s for s in web.db.statements) == 2


@pytest.mark.parametrize("role", ["employee", "agent"])
@pytest.mark.parametrize("method, path, body", REQUESTS)
def test_only_admins_in_database_mode_too(web, role, method, path, body):
    web.login_as(role)
    assert web.client.request(method, path, json=body if body is not None else {}).status_code == 403
    assert web.db.docs == {} and web.embedder.calls == []


@pytest.mark.parametrize("name", ["UPPER.md", "con.md", "x.txt", "under_score.md"])
def test_names_are_still_checked(web, name):
    assert put(web, name).status_code == 400
    assert web.db.docs == {} and web.embedder.calls == []


def test_content_is_still_cleaned_and_checked(web):
    assert put(web, "vpn.md", "no heading").status_code == 422
    assert put(web, "vpn.md", "\ufeff# T\r\n\r\nBody").status_code == 201
    assert text_of(web, "vpn.md") == ["# T\n\nBody\n"]


def test_the_cap_stops_new_articles_before_embedding_but_not_updates(web):
    web.mp.setattr(settings, "kb_max_articles", 1)
    assert put(web, "a.md").status_code == 201
    calls = len(web.embedder.calls)
    assert put(web, "b.md").status_code == 409 and len(web.embedder.calls) == calls
    assert put(web, "a.md", NEW).status_code == 200


def test_a_failing_embedder_leaves_the_article_as_it_was(web):
    put(web, "vpn.md")
    web.embedder.error = unavailable()
    assert put(web, "vpn.md", NEW).status_code == 503
    assert text_of(web, "vpn.md") == [ARTICLE]


@pytest.mark.parametrize("failing", ["insert into kb_chunks", "insert into audit_logs"])
def test_a_failing_database_rolls_the_whole_save_back(web, failing):
    put(web, "vpn.md")
    web.db.fail_on = failing
    assert put(web, "vpn.md", NEW).status_code == 500
    assert text_of(web, "vpn.md") == [ARTICLE] and len(web.db.audit) == 1


def test_a_failing_audit_write_keeps_the_article_on_delete(web):
    put(web, "vpn.md")
    web.db.fail_on = "insert into audit_logs"
    assert web.client.request("DELETE", "/kb/vpn.md", json={}).status_code == 500
    assert text_of(web, "vpn.md") == [ARTICLE]


def test_rows_without_text_are_shown_and_can_be_deleted(web):
    web.db.add_doc("jungkook.md")
    assert web.client.get("/kb").json()[0]["status"] == "text_missing"
    assert web.client.get("/kb/jungkook.md").status_code == 404
    assert web.client.request("DELETE", "/kb/jungkook.md", json={}).status_code == 200
    assert web.db.docs == {}
    assert web.client.request("DELETE", "/kb/jungkook.md", json={}).status_code == 404


def test_a_shared_database_still_needs_the_switch(web):
    web.mp.setattr(settings, "database_url", REMOTE)
    assert web.client.get("/kb/info").json()["editable"] is False
    assert put(web, "vpn.md").status_code == 403 and web.embedder.calls == []
    web.mp.setattr(settings, "kb_edit_allow_remote", True)
    info = web.client.get("/kb/info").json()
    assert info["editable"] is True and info["read_only_reason"] is None
    assert put(web, "vpn.md").status_code == 201


def test_a_missing_column_is_a_clear_503_and_costs_no_embedding(web):
    web.db.has_content_column = False
    for res in (web.client.get("/kb"), web.client.get("/kb/vpn.md"), put(web, "vpn.md")):
        assert res.status_code == 503 and res.json()["detail"] == kb.NO_COLUMN_MESSAGE
    assert web.embedder.calls == []


# ---------- file mode protects a database that stores the text ----------
def test_file_mode_works_on_a_database_without_the_column(env):
    env.db.has_content_column = False
    assert env.client.get("/kb/info").json()["editable"] is True
    assert put(env, "vpn.md").status_code == 201
    assert env.client.get("/kb").json()[0]["status"] == "indexed"
    assert env.client.request("DELETE", "/kb/vpn.md", json={}).status_code == 200


def test_file_mode_cannot_change_a_database_that_stores_the_text(env):
    env.db.add_doc("vpn.md", content=ARTICLE)
    assert env.client.get("/kb/info").json()["read_only_reason"] == kb.MANAGED_MESSAGE
    assert put(env, "vpn.md", NEW).status_code == 403
    assert env.client.request("DELETE", "/kb/vpn.md", json={}).status_code == 403
    assert text_of(env, "vpn.md") == [ARTICLE] and env.embedder.calls == []


def test_a_change_is_refused_when_that_check_fails(env):
    env.db.fail_on = "select exists"
    assert put(env, "vpn.md").status_code == 500
    assert env.db.docs == {} and env.embedder.calls == [] and files(env) == []


# ---------- the script ----------
def test_the_script_refuses_to_overwrite_web_edits(env):
    (env.folder / "vpn.md").write_text(ARTICLE, encoding="utf-8")
    env.db.add_doc("vpn.md", content=NEW)
    with pytest.raises(SystemExit, match="Refusing to index"):
        ingest.ingest()
    assert text_of(env, "vpn.md") == [NEW] and env.embedder.calls == []


def test_the_script_with_the_flag_replaces_and_keeps_the_database_managed(env):
    (env.folder / "vpn.md").write_text(ARTICLE, encoding="utf-8")
    env.db.add_doc("vpn.md", content=NEW)
    ingest.ingest(overwrite=True)
    assert text_of(env, "vpn.md") == [ARTICLE]


def test_the_script_saves_no_text_on_an_unmanaged_database(env):
    env.db.has_content_column = False
    (env.folder / "vpn.md").write_text(ARTICLE, encoding="utf-8")
    ingest.ingest()
    assert text_of(env, "vpn.md") == [None]


# ---------- the one-time import ----------
def indexed(env, name, content=ARTICLE):
    (env.folder / name).write_text(content, encoding="utf-8")
    with env.db.begin() as conn:
        ingest.store_article(conn, ingest.prepare_article(name, content))


@pytest.fixture
def importer(env):
    env.mp.setattr(import_articles, "engine", env.db)
    return env


def test_the_import_saves_the_text_without_any_ai_call_and_can_run_again(importer, capsys):
    indexed(importer, "vpn.md")
    importer.db.add_doc("jungkook.md")
    calls = len(importer.embedder.calls)
    import_articles.import_articles()
    assert text_of(importer, "vpn.md") == [ARTICLE] and text_of(importer, "jungkook.md") == [None]
    assert len(importer.embedder.calls) == calls
    assert "No file for jungkook.md" in capsys.readouterr().out
    import_articles.import_articles()
    assert "0 saved, 1 already had text" in capsys.readouterr().out


def test_the_import_never_overwrites_an_edit(importer):
    (importer.folder / "vpn.md").write_text(ARTICLE, encoding="utf-8")
    importer.db.add_doc("vpn.md", content=NEW)
    import_articles.import_articles()
    assert text_of(importer, "vpn.md") == [NEW]


@pytest.mark.parametrize("problem, message", [
    ("changed", "differs from the indexed text"), ("unindexed", "not indexed"), ("twice", "more than once"),
])
def test_the_import_changes_nothing_when_one_article_has_a_problem(importer, problem, message):
    indexed(importer, "a.md")
    if problem == "changed":
        indexed(importer, "b.md")
        (importer.folder / "b.md").write_text(NEW, encoding="utf-8")
    else:
        (importer.folder / "b.md").write_text(ARTICLE, encoding="utf-8")
        if problem == "twice":
            importer.db.add_doc("b.md")
            importer.db.add_doc("b.md")
    with pytest.raises(SystemExit, match=message):
        import_articles.import_articles()
    assert text_of(importer, "a.md") == [None]
