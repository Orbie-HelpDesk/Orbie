from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main


def test_static_files_must_be_revalidated(tmp_path):
    (tmp_path / "app.js").write_text("console.log('hi')", encoding="utf-8")
    app = FastAPI()
    app.mount("/static", main.NoCacheStaticFiles(directory=tmp_path), name="static")

    res = TestClient(app).get("/static/app.js")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"
    assert "etag" in res.headers  # revalidation still gets a cheap 304 when nothing changed


def test_chat_and_admin_pages_must_be_revalidated(tmp_path, monkeypatch):
    chat, admin = tmp_path / "chat", tmp_path / "admin"
    chat.mkdir()
    admin.mkdir()
    (chat / "index.html").write_text("<h1>chat</h1>", encoding="utf-8")
    (admin / "admin.html").write_text("<h1>admin</h1>", encoding="utf-8")
    monkeypatch.setattr(main, "CHAT_DIR", chat)
    monkeypatch.setattr(main, "ADMIN_DIR", admin)

    staff = {"id": 2, "email": "agent@example.com", "name": "Agent", "role": "agent"}
    monkeypatch.setattr(main, "user_from_request", lambda request: staff)
    client = TestClient(main.app)
    for path in ("/", "/admin"):
        res = client.get(path)
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-cache"
