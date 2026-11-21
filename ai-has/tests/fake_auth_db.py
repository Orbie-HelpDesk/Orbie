"""In-memory stand-in for the users and audit_logs tables, for the login tests."""
import json
from contextlib import contextmanager

from app.passwords import hash_password

# Few iterations on purpose: the stored hash says how many, and verifying stays fast in tests.
FAST = 1000
PASSWORD = "correct horse battery"


def make_user(user_id, email, name, role, password=PASSWORD):
    return {
        "id": user_id,
        "email": email,
        "name": name,
        "role": role,
        "password_hash": hash_password(password, iterations=FAST) if password else None,
    }


def default_users():
    users = [
        make_user(1, "employee@example.com", "Demo Employee", "employee"),
        make_user(2, "agent@example.com", "Demo Agent", "agent"),
        make_user(3, "admin@example.com", "Demo Admin", "admin"),
        make_user(4, "nopass@example.com", "No Password", "employee", password=None),
        make_user(5, "odd@example.com", "Odd Role", "superuser"),
    ]
    return {u["id"]: u for u in users}


class Result:
    def __init__(self, row=None):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeAuthDb:
    def __init__(self, users=None):
        self.users = users if users is not None else default_users()
        self.audit = []
        self.audit_broken = False

    @contextmanager
    def connect(self):
        yield FakeAuthConn(self)

    @contextmanager
    def begin(self):
        if self.audit_broken:
            raise RuntimeError("audit table missing")
        yield FakeAuthConn(self)


class FakeAuthConn:
    def __init__(self, db):
        self.db = db

    def execute(self, stmt, params=None):
        sql, db = str(stmt), self.db
        if "insert into audit_logs" in sql:
            db.audit.append({"actor": params["a"], "action": params["n"], "payload": json.loads(params["p"])})
            return Result()
        if "from users where email" in sql:
            row = next((u for u in db.users.values() if u["email"] == params["e"]), None)
            return Result(dict(row) if row else None)
        if "from users where id" in sql:
            row = db.users.get(params["i"])
            return Result({k: v for k, v in row.items() if k != "password_hash"} if row else None)
        raise AssertionError(f"unexpected sql: {sql}")
