"""In-memory stand-in for the pending_actions and audit_logs tables.

It follows the same rules as the real SQL in proposals.py: a claim succeeds only for the
owner, only while the status is 'pending' and only before it expires, and it is done under
a lock like a single UPDATE statement.
"""
import json
import threading
from contextlib import contextmanager


class Result:
    def __init__(self, rowcount=0, row=None):
        self.rowcount, self._row = rowcount, row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeActionsDb:
    def __init__(self):
        self.pending = {}
        self.audit = []
        self.lock = threading.Lock()

    def expire(self, action_id):
        self.pending[action_id]["expired"] = True

    def connect(self):
        return self._conn()

    @contextmanager
    def begin(self):
        yield self._conn()

    def _conn(self):
        return FakeActionsConn(self)


class FakeActionsConn:
    def __init__(self, db):
        self.db = db

    def execute(self, stmt, params=None):
        sql, p, db = str(stmt), params, self.db
        if "insert into pending_actions" in sql:
            db.pending[p["id"]] = {
                "owner": p["u"], "name": p["n"], "arguments": json.loads(p["a"]),
                "status": "pending", "expired": False,
            }
            return Result(1)
        if "insert into audit_logs" in sql:
            db.audit.append({"actor": p["a"], "action": p["n"], "payload": json.loads(p["p"])})
            return Result(1)
        if "decided_at = now()" in sql:  # the atomic claim
            with db.lock:
                row = db.pending.get(p["id"])
                if row and row["owner"] == p["u"] and row["status"] == "pending" and not row["expired"]:
                    row["status"] = p["s"]
                    return Result(1, {"name": row["name"], "arguments": row["arguments"]})
            return Result(0)
        if "select owner_user_id" in sql:
            row = db.pending.get(p["id"])
            if row is None:
                return Result(0)
            return Result(1, {"owner_user_id": row["owner"], "status": row["status"], "expired": row["expired"]})
        if "where id = :id and status = 'confirmed'" in sql:  # finish
            row = db.pending.get(p["id"])
            if row and row["status"] == "confirmed":
                row["status"] = p["s"]
            return Result(1)
        raise AssertionError(f"unexpected sql: {sql}")
