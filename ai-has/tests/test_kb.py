import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import audit, ingest, kb, llm, main, rag
from app.config import settings
from app.ratelimit import limiter
from fake_kb_db import FakeEmbedder, FakeKbDb
from test_llm import unavailable

ARTICLE = "# VPN troubleshooting\n\nRestart the VPN client.\n\nCheck your network.\n"


@pytest.fixture
def env(monkeypatch, tmp_path, login_as):
    """The knowledge base API on a temporary folder, a fake index and a fake embedder. No Gemini, no database."""
    folder = tmp_path / "kb"
    folder.mkdir()
    db, embedder = FakeKbDb(), FakeEmbedder()
    for module in (kb, ingest, audit):
        monkeypatch.setattr(module, "engine", db)
    monkeypatch.setattr(ingest, "client", embedder)
    monkeypatch.setattr(settings, "kb_dir", str(folder))
    monkeypatch.setattr(settings, "embed_dimensions", 4)
    monkeypatch.setattr(settings, "kb_max_bytes", 60_000)
    monkeypatch.setattr(settings, "kb_max_articles", 200)
    monkeypatch.setattr(settings, "kb_edit_allow_remote", False)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(rag, "answer", lambda *a, **k: pytest.fail("the test box must not call the answer step"))
    limiter.reset()
    login_as("admin")  # user id 3
    state = SimpleNamespace(
        folder=folder, db=db, embedder=embedder, login_as=login_as, mp=monkeypatch,
        client=TestClient(main.app, raise_server_exceptions=False),
    )
    yield state
    limiter.reset()


def put(env, name, content=ARTICLE):
    return env.client.put(f"/kb/{name}", json={"content": content})


def files(env):
    return sorted(p.name for p in env.folder.iterdir())


# ---------- who may use it ----------
REQUESTS = [
    ("GET", "/kb", None),
    ("GET", "/kb/vpn.md", None),
    ("PUT", "/kb/vpn.md", {"content": ARTICLE}),
    ("DELETE", "/kb/vpn.md", {}),
    ("POST", "/kb/test", {"question": "my vpn is down"}),
]


@pytest.mark.parametrize("method, path, body", REQUESTS)
def test_a_logged_out_user_gets_401(env, method, path, body):
    main.app.dependency_overrides.clear()
    res = TestClient(main.app).request(method, path, json=body if body is not None else {})
    assert res.status_code == 401
    assert files(env) == [] and env.db.docs == {} and env.embedder.calls == []


@pytest.mark.parametrize("role", ["employee", "agent"])
@pytest.mark.parametrize("method, path, body", REQUESTS)
def test_an_employee_and_an_agent_get_403_and_nothing_happens(env, role, method, path, body):
    env.login_as(role)
    res = env.client.request(method, path, json=body if body is not None else {})
    assert res.status_code == 403
    assert files(env) == [] and env.db.docs == {} and env.db.audit == [] and env.embedder.calls == []


def test_an_agent_cannot_change_an_existing_article(env):
    put(env, "vpn.md")
    before = (env.folder / "vpn.md").read_text(encoding="utf-8")
    env.login_as("agent")
    assert put(env, "vpn.md", "# Changed\n\nNope.\n").status_code == 403
    assert env.client.delete("/kb/vpn.md").status_code in (403, 415)
    assert env.client.request("DELETE", "/kb/vpn.md", json={}).status_code == 403
    assert (env.folder / "vpn.md").read_text(encoding="utf-8") == before and env.db.sources() == ["vpn.md"]


@pytest.mark.parametrize("method, path", [("GET", "/kb"), ("PUT", "/kb/vpn.md"), ("DELETE", "/kb/vpn.md")])
def test_the_403_is_the_roles_message_not_a_detail_about_the_knowledge_base(env, method, path):
    env.login_as("agent")
    res = env.client.request(method, path, json={"content": ARTICLE})
    assert res.status_code == 403 and res.json() == {"detail": "You do not have access to this."}


# ---------- file names and paths ----------
BAD_NAMES = [
    "UPPER.md", "Mixed-Case.md", "has space.md", "under_score.md", "dot.in.name.md", "x.txt", "x.markdown", "x",
    ".md", "-leading-dash.md", "ünï.md", "con.md", "nul.md", "aux.md", "prn.md", "com1.md", "lpt9.md",
    "x" * 82 + ".md", "a\\b.md", "..\\evil.md", "a%00.md", "x.md.md", "x.md ",
]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_a_bad_file_name_is_refused_and_nothing_is_written(env, name):
    res = env.client.put(f"/kb/{name}", json={"content": ARTICLE})
    assert res.status_code in (400, 404, 405, 422)
    assert files(env) == [] and env.db.docs == {} and env.embedder.calls == [] and env.db.audit == []
    assert env.client.get(f"/kb/{name}").status_code in (400, 404, 405, 422)
    assert env.client.request("DELETE", f"/kb/{name}", json={}).status_code in (400, 404, 405, 422)


@pytest.mark.parametrize("name", ["..%2Fevil.md", "..%2F..%2Fevil.md", "%2e%2e%2fevil.md", "sub%2Fevil.md", "%2Fetc%2Fpasswd", "....%2F%2Fevil.md"])
def test_path_traversal_through_the_url_never_writes_outside_the_folder(env, name):
    res = env.client.put(f"/kb/{name}", json={"content": ARTICLE})
    assert res.status_code in (400, 404, 405)
    assert files(env) == []
    assert not (env.folder.parent / "evil.md").exists()
    assert sorted(p.name for p in env.folder.parent.iterdir()) == ["kb"]


@pytest.mark.parametrize("name", ["../evil.md", "../../evil.md", "/etc/passwd", "sub/evil.md", "..", "vpn.md/../../x.md"])
def test_the_functions_themselves_refuse_traversal_too(env, name):
    for call in (kb.path_for, kb.check_name):
        with pytest.raises(kb.KbError) as err:
            call(name)
        assert err.value.status_code == 400
    with pytest.raises(kb.KbError):
        kb.save_article(name, ARTICLE, "user:3")
    assert files(env) == [] and not (env.folder.parent / "evil.md").exists()


@pytest.mark.parametrize("name", ["vpn.md", "a.md", "0.md", "wi-fi-troubleshooting.md", "x" * 81 + ".md"[:0] + ".md"])
def test_good_names_are_accepted(env, name):
    assert kb.check_name(name) == name


def test_a_symlink_in_the_folder_is_never_followed(env, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("# Secret\n\noutside", encoding="utf-8")
    try:
        os.symlink(outside, env.folder / "link.md")
    except (OSError, NotImplementedError):
        pytest.skip("this system does not allow creating symlinks")
    for res in (put(env, "link.md"), env.client.get("/kb/link.md"), env.client.request("DELETE", "/kb/link.md", json={})):
        assert res.status_code == 400
    assert outside.read_text(encoding="utf-8") == "# Secret\n\noutside" and env.db.docs == {}
    assert [i["source"] for i in env.client.get("/kb").json()] == []  # not even listed


def test_a_missing_knowledge_base_folder_is_a_503(env):
    env.mp.setattr(settings, "kb_dir", str(env.folder / "does-not-exist"))
    assert env.client.get("/kb").status_code == 503
    assert put(env, "vpn.md").status_code == 503


# ---------- size and content ----------
def test_an_article_of_exactly_the_limit_is_accepted_and_one_byte_more_is_refused(env):
    env.mp.setattr(settings, "kb_max_bytes", 200)
    body = "# T\n\n" + "a" * 194 + "\n"  # 5 + 194 + 1 = 200 bytes
    assert len(body.encode("utf-8")) == 200
    assert put(env, "fits.md", body).status_code == 201
    res = put(env, "toolong.md", body.replace("a" * 194, "a" * 195))
    assert res.status_code == 413 and "200 bytes" in res.json()["detail"]
    assert files(env) == ["fits.md"] and env.db.sources() == ["fits.md"]


def test_the_limit_counts_bytes_not_characters(env):
    env.mp.setattr(settings, "kb_max_bytes", 200)
    assert put(env, "ok.md", "# T\n\n" + "é" * 97).status_code == 201  # 5 + 194 + 1 = 200 bytes
    assert put(env, "big.md", "# T\n\n" + "é" * 98).status_code == 413  # 202 bytes, only 104 characters


def test_a_huge_body_is_refused_before_it_is_read_or_embedded(env):
    res = env.client.put("/kb/big.md", json={"content": "# T\n\n" + "a" * (settings.kb_max_bytes * 7)})
    assert res.status_code == 413
    assert env.embedder.calls == [] and files(env) == []


@pytest.mark.parametrize(
    "content, status",
    [
        ("", 422), ("   \n\n  ", 422), ("No heading here\n\nJust text.", 422), ("#NoSpace\n\ntext", 422),
        ("##  \n\ntext", 422), ("text\n# Heading later", 422), ("# Title\x00\n\nNUL", 422),
    ],
)
def test_content_that_is_empty_has_no_heading_or_has_a_nul_is_refused(env, content, status):
    res = put(env, "bad.md", content)
    assert res.status_code == status and files(env) == [] and env.embedder.calls == []


def test_content_must_be_a_string(env):
    assert env.client.put("/kb/x.md", json={"content": 123}).status_code == 422
    assert env.client.put("/kb/x.md", json={}).status_code == 422


def test_line_endings_and_a_bom_are_cleaned_and_blank_lines_before_the_heading_are_fine(env):
    assert put(env, "win.md", "﻿\r\n# Windows file\r\n\r\nLine one.\r\nLine two.").status_code == 201
    stored = (env.folder / "win.md").read_bytes()
    assert stored == b"\n# Windows file\n\nLine one.\nLine two.\n" or stored.startswith(b"\n# Windows")
    assert b"\r" not in stored and not stored.startswith(b"\xef\xbb\xbf")


# ---------- create, update, and the index ----------
def test_create_writes_the_file_indexes_it_and_audits_without_the_text(env):
    res = put(env, "vpn.md")
    assert res.status_code == 201
    assert res.json() == {"source": "vpn.md", "title": "VPN troubleshooting", "chunks": 1, "size": len(ARTICLE.encode()), "created": True}
    assert (env.folder / "vpn.md").read_text(encoding="utf-8") == ARTICLE
    assert env.db.sources() == ["vpn.md"] and len(env.db.chunks_of("vpn.md")) == 1
    assert env.db.audit == [{"actor": "user:3", "action": "kb_create", "payload": {"file": "vpn.md"}}]
    assert "Restart the VPN" not in str(env.db.audit)
    assert files(env) == ["vpn.md"]  # no temp file left behind


def test_update_replaces_the_file_and_the_chunks_and_audits_an_update(env):
    put(env, "vpn.md")
    res = put(env, "vpn.md", "# VPN v2\n\nA completely new article.\n")
    assert res.status_code == 200 and res.json()["created"] is False and res.json()["title"] == "VPN v2"
    assert (env.folder / "vpn.md").read_text(encoding="utf-8") == "# VPN v2\n\nA completely new article.\n"
    assert [d["title"] for d in env.db.docs.values()] == ["VPN v2"] and len(env.db.chunks_of("vpn.md")) == 1
    assert [a["action"] for a in env.db.audit] == ["kb_create", "kb_update"]


def test_only_the_saved_article_is_re_indexed(env):
    other = env.db.add_doc("other.md", title="Other", chunks=3)
    before = [dict(c) for c in env.db.chunks_of("other.md")]
    put(env, "vpn.md")
    assert env.db.sources() == ["other.md", "vpn.md"] and env.db.docs[other]["title"] == "Other"
    assert env.db.chunks_of("other.md") == before
    assert len(env.embedder.calls) == 1  # one embedding call, for this article only


def test_a_long_article_is_embedded_in_batches(env):
    body = "# Long\n\n" + "\n\n".join(f"Paragraph {i} " + "word " * 140 for i in range(70)) + "\n"
    res = put(env, "long.md", body)
    assert res.status_code == 201 and res.json()["chunks"] > 50
    assert [len(c["input"]) for c in env.embedder.calls][0] == 50 and len(env.embedder.calls) >= 2


def test_the_new_article_can_be_read_back(env):
    put(env, "vpn.md")
    body = env.client.get("/kb/vpn.md").json()
    assert body == {"source": "vpn.md", "content": ARTICLE, "size": len(ARTICLE.encode()), "title": "VPN troubleshooting"}
    assert env.client.get("/kb/missing.md").status_code == 404


def test_the_article_cap_stops_new_articles_but_not_updates(env):
    env.mp.setattr(settings, "kb_max_articles", 2)
    assert put(env, "a.md").status_code == 201 and put(env, "b.md").status_code == 201
    res = put(env, "c.md")
    assert res.status_code == 409 and "2 articles" in res.json()["detail"]
    assert put(env, "a.md", "# A again\n\nChanged.\n").status_code == 200
    assert files(env) == ["a.md", "b.md"]


# ---------- failures change nothing ----------
def test_a_failing_embedder_changes_nothing_for_a_new_article(env):
    env.embedder.error = unavailable()
    res = put(env, "vpn.md")
    assert res.status_code == 503 and res.json() == {"detail": llm.BUSY_MESSAGE} and res.headers["retry-after"] == "10"
    assert files(env) == [] and env.db.docs == {} and env.db.audit == []


def test_a_failing_embedder_leaves_the_existing_article_as_it_was(env):
    put(env, "vpn.md")
    env.embedder.error = unavailable()
    assert put(env, "vpn.md", "# Changed\n\nNew text.\n").status_code == 503
    assert (env.folder / "vpn.md").read_text(encoding="utf-8") == ARTICLE
    assert [d["title"] for d in env.db.docs.values()] == ["VPN troubleshooting"] and len(env.db.audit) == 1


def test_a_failing_database_leaves_the_file_and_the_index_as_they_were_and_removes_the_temp_file(env):
    put(env, "vpn.md")
    env.db.fail_on = "insert into kb_chunks"
    res = put(env, "vpn.md", "# Changed\n\nNew text.\n")
    assert res.status_code == 500 and res.json() == {"detail": "Something went wrong. Please try again."}
    assert (env.folder / "vpn.md").read_text(encoding="utf-8") == ARTICLE and files(env) == ["vpn.md"]
    assert [d["title"] for d in env.db.docs.values()] == ["VPN troubleshooting"] and len(env.db.audit) == 1


def test_a_failing_audit_write_rolls_the_whole_save_back(env):
    env.db.fail_on = "insert into audit_logs"
    assert put(env, "vpn.md").status_code == 500
    assert files(env) == [] and env.db.docs == {} and env.db.chunks == []


def test_if_the_file_cannot_be_moved_into_place_the_admin_is_told_to_save_again(env):
    def broken(src, dst):
        raise OSError("disk full")

    env.mp.setattr(kb.os, "replace", broken)
    res = put(env, "vpn.md")
    assert res.status_code == 500 and "Save it again" in res.json()["detail"]
    assert files(env) == []  # the temp file is cleaned up


# ---------- delete ----------
def test_delete_removes_the_file_and_the_index_rows_and_audits(env):
    put(env, "vpn.md")
    put(env, "wifi.md", "# Wi-Fi\n\nCheck the router.\n")
    res = env.client.request("DELETE", "/kb/vpn.md", json={})
    assert res.status_code == 200 and res.json() == {"source": "vpn.md", "deleted": True}
    assert files(env) == ["wifi.md"] and env.db.sources() == ["wifi.md"] and env.db.chunks_of("vpn.md") == []
    assert env.db.audit[-1] == {"actor": "user:3", "action": "kb_delete", "payload": {"file": "vpn.md"}}


def test_delete_cleans_up_an_article_that_has_only_a_file_or_only_index_rows(env):
    (env.folder / "only-file.md").write_text("# Only file\n", encoding="utf-8")
    env.db.add_doc("only-index.md")
    assert env.client.request("DELETE", "/kb/only-file.md", json={}).status_code == 200
    assert env.client.request("DELETE", "/kb/only-index.md", json={}).status_code == 200
    assert files(env) == [] and env.db.docs == {}


def test_deleting_something_that_does_not_exist_is_404_and_not_audited(env):
    res = env.client.request("DELETE", "/kb/nothing.md", json={})
    assert res.status_code == 404 and env.db.audit == []


def test_a_failing_database_keeps_the_file_on_delete(env):
    put(env, "vpn.md")
    env.db.fail_on = "delete from kb_documents"
    assert env.client.request("DELETE", "/kb/vpn.md", json={}).status_code == 500
    assert files(env) == ["vpn.md"] and env.db.sources() == ["vpn.md"]


def test_a_delete_must_be_json_like_every_state_change(env):
    put(env, "vpn.md")
    assert env.client.delete("/kb/vpn.md").status_code == 415
    assert files(env) == ["vpn.md"]


def test_a_put_must_be_json(env):
    assert env.client.put("/kb/vpn.md", content=ARTICLE, headers={"Content-Type": "text/plain"}).status_code == 415
    assert files(env) == []


# ---------- the list ----------
def test_the_list_shows_indexed_not_indexed_and_missing_files_with_zoned_times(env):
    put(env, "vpn.md")
    (env.folder / "draft.md").write_text("# Draft article\n\nNot indexed yet.\n", encoding="utf-8")
    env.db.add_doc("gone.md", title="Gone", chunks=4)
    (env.folder / "notes.txt").write_text("ignored", encoding="utf-8")
    (env.folder / "Bad Name.md").write_text("# ignored", encoding="utf-8")
    (env.folder / ".hidden.md.abc.tmp").write_text("# ignored", encoding="utf-8")
    items = {i["source"]: i for i in env.client.get("/kb").json()}
    assert sorted(items) == ["draft.md", "gone.md", "vpn.md"]
    assert (items["vpn.md"]["status"], items["vpn.md"]["chunks"], items["vpn.md"]["title"]) == ("indexed", 1, "VPN troubleshooting")
    assert items["vpn.md"]["indexed_at"].endswith(("Z", "+00:00")) and items["vpn.md"]["size"] == len(ARTICLE.encode())
    assert (items["draft.md"]["status"], items["draft.md"]["chunks"], items["draft.md"]["title"], items["draft.md"]["indexed_at"]) == ("not_indexed", 0, "Draft article", None)
    assert (items["gone.md"]["status"], items["gone.md"]["size"]) == ("file_missing", None)


# ---------- only a local database unless it is switched on ----------
REMOTE = "postgresql+psycopg2://u:p@db.supabase.co:5432/postgres"


def test_changes_are_refused_on_a_shared_database_by_default(env):
    put(env, "vpn.md")
    env.mp.setattr(settings, "database_url", REMOTE)
    res = put(env, "vpn.md", "# Changed\n\nText.\n")
    assert res.status_code == 403 and "KB_EDIT_ALLOW_REMOTE" in res.json()["detail"] and "team lead" in res.json()["detail"]
    assert env.client.request("DELETE", "/kb/vpn.md", json={}).status_code == 403
    assert (env.folder / "vpn.md").read_text(encoding="utf-8") == ARTICLE and env.db.sources() == ["vpn.md"]
    assert "supabase" not in res.text and "p@" not in res.text  # never the URL


def test_reading_and_testing_still_work_on_a_shared_database(env):
    put(env, "vpn.md")
    env.mp.setattr(settings, "database_url", REMOTE)
    env.mp.setattr(rag, "retrieve", lambda q, k=4: [])
    assert env.client.get("/kb").status_code == 200
    assert env.client.get("/kb/vpn.md").status_code == 200
    assert env.client.post("/kb/test", json={"question": "my vpn"}).status_code == 200


def test_the_switch_allows_changes_on_a_shared_database(env):
    env.mp.setattr(settings, "database_url", REMOTE)
    env.mp.setattr(settings, "kb_edit_allow_remote", True)
    assert put(env, "vpn.md").status_code == 201


@pytest.mark.parametrize("url", ["postgresql+psycopg2://u:p@db:5432/x", "postgresql+psycopg2://u:p@localhost:5433/x"])
def test_a_local_database_needs_no_switch(env, url):
    env.mp.setattr(settings, "database_url", url)
    assert put(env, "vpn.md").status_code == 201


def test_an_empty_host_is_not_treated_as_local(env):
    env.mp.setattr(settings, "database_url", "postgresql+psycopg2://u:p@/x")
    assert put(env, "vpn.md").status_code == 403


# ---------- test this article ----------
def chunk(source, distance, text="some text " * 60):
    return {"content": text, "metadata": {"source": source}, "distance": distance, "score": 0.0321}


def test_the_test_box_shows_chunks_distances_and_which_belong_to_the_article(env):
    env.mp.setattr(rag, "retrieve", lambda q, k=4: [chunk("vpn.md", 0.2111), chunk("wifi.md", 0.41), chunk("vpn.md", 0.77)])
    body = env.client.post("/kb/test", json={"question": "  my vpn will not connect ", "source": "vpn.md"}).json()
    assert body["question"] == "my vpn will not connect"
    assert [(c["source"], c["distance"], c["from_this_article"]) for c in body["chunks"]] == [
        ("vpn.md", 0.211, True), ("wifi.md", 0.41, False), ("vpn.md", 0.77, True)]
    assert all(len(c["snippet"]) <= kb.SNIPPET_CHARS for c in body["chunks"])
    assert (body["best_distance"], body["max_distance"], body["would_answer"], body["this_article_found"]) == (0.211, rag.MAX_DISTANCE, True, True)


def test_a_question_that_is_too_far_would_not_be_answered(env):
    env.mp.setattr(rag, "retrieve", lambda q, k=4: [chunk("wifi.md", 0.62)])
    body = env.client.post("/kb/test", json={"question": "payroll dates", "source": "vpn.md"}).json()
    assert body["would_answer"] is False and body["this_article_found"] is False


def test_with_nothing_indexed_there_are_no_chunks(env):
    env.mp.setattr(rag, "retrieve", lambda q, k=4: [])
    body = env.client.post("/kb/test", json={"question": "anything"}).json()
    assert body["chunks"] == [] and body["best_distance"] is None and body["would_answer"] is False


def test_the_test_box_uses_the_chat_search_with_one_embedding_call_and_no_answer_call(env):
    from app.ingest import client as real_client_unused  # noqa: F401

    seen = []
    env.mp.setattr(rag, "embed", lambda texts: seen.append(texts) or [[0.0] * 4])
    env.mp.setattr(rag, "engine", SimpleNamespace(connect=lambda: pytest.fail("not reached with a stubbed search")))
    env.mp.setattr(rag, "retrieve", lambda q, k=4: seen.append(("retrieve", q, k)) or [])
    env.client.post("/kb/test", json={"question": "my vpn"})
    assert seen == [("retrieve", "my vpn", kb.TEST_CHUNKS)]  # rag.answer is a trap in the fixture and was not hit


def test_the_test_box_validates_its_input(env):
    assert env.client.post("/kb/test", json={"question": "x"}).status_code == 422
    assert env.client.post("/kb/test", json={"question": "y" * 501}).status_code == 422
    assert env.client.post("/kb/test", json={"question": "fine question", "source": "../x.md"}).status_code == 400


def test_a_provider_failure_in_the_test_box_is_the_clear_503(env):
    def down(q, k=4):
        raise llm.ProviderUnavailable("server", 503)

    env.mp.setattr(rag, "retrieve", down)
    res = env.client.post("/kb/test", json={"question": "my vpn"})
    assert res.status_code == 503 and res.json() == {"detail": llm.BUSY_MESSAGE}


# ---------- rate limit ----------
def test_saving_deleting_and_testing_share_a_rate_limit(env):
    env.mp.setattr(settings, "rate_limit_enabled", True)
    env.mp.setattr(settings, "rate_limit_kb_per_min", 3)
    env.mp.setattr(rag, "retrieve", lambda q, k=4: [])
    assert put(env, "a.md").status_code == 201
    assert env.client.post("/kb/test", json={"question": "my vpn"}).status_code == 200
    assert env.client.request("DELETE", "/kb/a.md", json={}).status_code == 200
    res = put(env, "b.md")
    assert res.status_code == 429 and "Retry-After" in res.headers
    assert env.client.get("/kb").status_code == 200  # reading the list is not limited


def test_a_symlink_is_refused_even_where_symlinks_cannot_be_created(env):
    """The same rule as above, with the symlink check faked, so it runs on every system."""
    (env.folder / "link.md").write_text("# Link\n\nbody\n", encoding="utf-8")
    real = type(env.folder).is_symlink
    env.mp.setattr(type(env.folder), "is_symlink", lambda self: self.name == "link.md" or real(self))
    for res in (put(env, "link.md"), env.client.get("/kb/link.md"), env.client.request("DELETE", "/kb/link.md", json={})):
        assert res.status_code == 400
    assert env.db.docs == {} and env.embedder.calls == []
    assert env.client.get("/kb").json() == []  # not listed either


# ---------- the uploaded copy (Vercel has no /kb folder) ----------
def use_uploaded_copy(env):
    """KB_DIR at its default, no such folder, and the temporary folder standing in for backend/kb."""
    (env.folder / "vpn.md").write_text(ARTICLE, encoding="utf-8")
    env.mp.setattr(ingest, "DEFAULT_KB_DIR", str(env.folder / "not-mounted"))
    env.mp.setattr(ingest, "BUNDLED_KB_DIR", env.folder)
    env.mp.setattr(settings, "kb_dir", str(env.folder / "not-mounted"))


def test_without_a_kb_folder_the_uploaded_copy_is_listed_and_read(env):
    use_uploaded_copy(env)
    assert [a["source"] for a in env.client.get("/kb").json()] == ["vpn.md"]
    assert env.client.get("/kb/vpn.md").json()["content"] == ARTICLE


@pytest.mark.parametrize("allow_remote", [False, True])
def test_the_uploaded_copy_can_never_be_changed(env, allow_remote):
    use_uploaded_copy(env)
    env.mp.setattr(settings, "kb_edit_allow_remote", allow_remote)
    env.mp.setattr(settings, "database_url", "postgresql+psycopg2://u:p@localhost:5433/x")
    info = env.client.get("/kb/info").json()
    assert info["editable"] is False and info["read_only_reason"] == kb.BUNDLED_MESSAGE
    assert put(env, "vpn.md", "# New\n\nText.\n").status_code == 403
    assert put(env, "wifi.md").status_code == 403
    assert env.client.request("DELETE", "/kb/vpn.md", json={}).status_code == 403
    assert files(env) == ["vpn.md"]
    assert (env.folder / "vpn.md").read_text(encoding="utf-8") == ARTICLE
    assert env.embedder.calls == []


def test_the_default_kb_folder_wins_over_the_uploaded_copy(env, tmp_path):
    env.mp.setattr(ingest, "DEFAULT_KB_DIR", str(env.folder))
    env.mp.setattr(ingest, "BUNDLED_KB_DIR", tmp_path)
    assert ingest.kb_dir() == env.folder and not ingest.is_bundled_kb()


def test_the_uploaded_copy_matches_the_kb_folder():
    root = Path(__file__).resolve().parents[1]
    originals, copies = (
        {p.name: p.read_text(encoding="utf-8") for p in folder.glob("*.md")}
        for folder in (root / "kb", root / "backend" / "kb")
    )
    assert originals and copies == originals
