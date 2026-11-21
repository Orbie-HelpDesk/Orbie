import pytest

from app import init_db, passwords, safety
from app.config import settings


def test_hash_and_verify_round_trip():
    stored = passwords.hash_password("correct horse battery")
    assert stored.startswith("pbkdf2_sha256$600000$")
    assert "correct horse" not in stored
    assert passwords.verify_password("correct horse battery", stored)
    assert not passwords.verify_password("correct horse batterY", stored)


def test_each_hash_has_its_own_salt():
    assert passwords.hash_password("same-password") != passwords.hash_password("same-password")


@pytest.mark.parametrize("stored", [None, "", "plain-text", "md5$1$a$b", "pbkdf2_sha256$x$y$z", "pbkdf2_sha256$1$@@@$@@@"])
def test_a_missing_or_malformed_hash_never_verifies(stored):
    assert passwords.verify_password("anything", stored) is False


def test_the_dummy_hash_is_a_valid_hash_nobody_knows():
    assert passwords.DUMMY_HASH.startswith("pbkdf2_sha256$")
    assert not passwords.verify_password("password", passwords.DUMMY_HASH)


def test_password_rules():
    assert passwords.check_password_rules("short") is not None
    assert passwords.check_password_rules("long enough password") is None
    assert passwords.check_password_rules("x" * 201) is not None


# ---------- demo passwords in init_db ----------
class FakeResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class FakeConn:
    """Records updates. Users in `with_password` already have a hash and are skipped, like the SQL."""

    def __init__(self, with_password=()):
        self.with_password, self.updates = set(with_password), []

    def execute(self, stmt, params):
        assert "password_hash is null" in str(stmt)
        if params["e"] in self.with_password:
            return FakeResult(0)
        self.updates.append(params)
        return FakeResult(1)


def configure(monkeypatch, url, password):
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "demo_password", password)


LOCAL = "postgresql+psycopg2://aihas:x@db:5432/aihas"


def test_demo_passwords_are_set_on_a_local_database(monkeypatch):
    configure(monkeypatch, LOCAL, "demo-password-1")
    conn = FakeConn()
    note = init_db.set_demo_passwords(conn)
    assert [u["e"] for u in conn.updates] == [u["email"] for u in init_db.DEMO_USERS]
    assert all(passwords.verify_password("demo-password-1", u["h"]) for u in conn.updates)
    assert "3 user(s)" in note and "demo-password-1" not in note


def test_demo_passwords_never_overwrite_an_existing_password(monkeypatch):
    configure(monkeypatch, LOCAL, "demo-password-1")
    conn = FakeConn(with_password={"admin@example.com"})
    note = init_db.set_demo_passwords(conn)
    assert "admin@example.com" not in [u["e"] for u in conn.updates]
    assert "2 user(s)" in note


@pytest.mark.parametrize("url", ["postgresql+psycopg2://u:p@db.supabase.co:5432/postgres", "postgresql+psycopg2://u:p@/x"])
def test_demo_passwords_are_never_set_on_a_remote_database(monkeypatch, url):
    configure(monkeypatch, url, "demo-password-1")
    conn = FakeConn()
    assert "not local" in init_db.set_demo_passwords(conn)
    assert conn.updates == []


@pytest.mark.parametrize("password, text", [("", "empty"), ("short", "at least 8")])
def test_demo_passwords_need_an_acceptable_value(monkeypatch, password, text):
    configure(monkeypatch, LOCAL, password)
    conn = FakeConn()
    assert text in init_db.set_demo_passwords(conn)
    assert conn.updates == []


def test_is_local_db_ignores_the_allow_remote_flag():
    assert safety.is_local_db(LOCAL)
    assert safety.is_local_db("postgresql+psycopg2://u:p@localhost:5433/x")
    assert not safety.is_local_db("postgresql+psycopg2://u:p@db.supabase.co:5432/postgres")
    assert not safety.is_local_db("postgresql+psycopg2://u:p@db:5432/x?host=evil.example.com")
