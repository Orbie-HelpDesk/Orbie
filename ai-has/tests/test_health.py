from contextlib import contextmanager

from fastapi.testclient import TestClient

from app import main


class FakeConn:
    def execute(self, *args, **kwargs):
        return None


class FakeEngine:
    @contextmanager
    def connect(self):
        yield FakeConn()


class BrokenEngine:
    def connect(self):
        raise ConnectionError("db down")


def test_models_import_and_register_tables():
    from app import models

    assert {"users", "conversations", "messages", "tickets", "kb_documents", "audit_logs"} <= set(
        models.Base.metadata.tables
    )


def test_health_ok_with_stubbed_database(monkeypatch):
    monkeypatch.setattr(main, "engine", FakeEngine())
    res = TestClient(main.app).get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "database": "connected"}


def test_health_reports_database_error(monkeypatch):
    monkeypatch.setattr(main, "engine", BrokenEngine())
    res = TestClient(main.app).get("/health")
    assert res.status_code == 200
    assert res.json()["database"] == "error: ConnectionError"
