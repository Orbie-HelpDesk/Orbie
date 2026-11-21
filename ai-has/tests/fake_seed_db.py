"""An in-memory stand-in for the tables seed_demo touches. It records every statement, so a test can
check that nothing outside the demo tables is ever deleted."""
import json
from contextlib import contextmanager


class Result:
    def __init__(self, value=None, rowcount=0):
        self._value, self.rowcount = value, rowcount

    def scalar(self):
        return self._value


DELETABLE = ("pending_actions", "tickets", "messages", "conversations", "audit_logs")


class FakeSeedDb:
    def __init__(self, with_employee=True):
        self.users = {"employee@example.com": 1, "agent@example.com": 2, "admin@example.com": 3} if with_employee else {}
        # rows that must survive --reset
        self.kb_documents = [{"id": 1, "source_path": "vpn-troubleshooting.md"}]
        self.kb_chunks = [{"id": 1, "document_id": 1}]
        # leftovers from development
        self.tickets = [{"id": 90, "summary": "as", "user_id": 1}, {"id": 91, "summary": "Linked ticket", "user_id": 1}]
        self.conversations = [{"id": 90, "user_id": 1, "title": "how to run"}]
        self.messages = [{"conversation_id": 90, "role": "user", "content": "how to run"}]
        self.pending_actions = [{"id": "abc"}]
        self.audit_logs = [{"actor": "user:1", "action": "login", "payload": {}}]
        self.statements = []
        self.commits = 0
        self.fail_on = None
        self._next = 100

    def snapshot(self):
        return {k: [dict(r) for r in getattr(self, k)] for k in (
            "tickets", "conversations", "messages", "pending_actions", "audit_logs", "kb_documents", "kb_chunks")}

    @contextmanager
    def begin(self):
        before = self.snapshot()
        try:
            yield FakeSeedConn(self)
            self.commits += 1
        except BaseException:
            for key, rows in before.items():
                setattr(self, key, rows)  # roll back
            raise


class FakeSeedConn:
    def __init__(self, db):
        self.db = db

    def execute(self, stmt, params=None):
        sql, db = " ".join(str(stmt).split()), self.db
        db.statements.append(sql)
        if db.fail_on and db.fail_on in sql:
            raise RuntimeError("database failure (test)")
        if sql.startswith("select id from users where email"):
            return Result(db.users.get(params["e"]))
        if sql.startswith("delete from "):
            table = sql.split()[2]
            assert table in DELETABLE, f"seed_demo must never delete from {table}"
            count = len(getattr(db, table))
            setattr(db, table, [])
            return Result(rowcount=count)
        if sql.startswith("select id from conversations where user_id"):
            row = next((c for c in db.conversations if c["user_id"] == params["u"] and c["title"] == params["t"]), None)
            return Result(row["id"] if row else None)
        if sql.startswith("insert into conversations"):
            db._next += 1
            db.conversations.append({"id": db._next, "user_id": params["u"], "title": params["t"], "status": params["s"], "created_at": params["c"]})
            return Result(db._next)
        if sql.startswith("insert into messages"):
            db.messages.append({
                "conversation_id": params["c"], "role": params["r"], "content": params["t"], "intent": params["i"],
                "outcome": params["o"], "citations": json.loads(params["ci"]) if params["ci"] else None,
                "confidence": params["cf"], "latency_ms": params["l"], "feedback": params["f"],
                "escalated": params["e"], "created_at": params["at"],
            })
            return Result()
        if sql.startswith("select id from tickets where user_id"):
            row = next((t for t in db.tickets if t["user_id"] == params["u"] and t["summary"] == params["s"]), None)
            return Result(row["id"] if row else None)
        if sql.startswith("insert into tickets"):
            db.tickets.append({
                "conversation_id": params["c"], "user_id": params["u"], "summary": params["s"], "priority": params["p"],
                "status": params["st"], "source": params["src"], "external_ref": params["r"],
                "created_at": params["ca"], "resolved_at": params["ra"], "id": len(db.tickets) + 200,
            })
            return Result()
        if sql.startswith("insert into audit_logs"):
            db.audit_logs.append({"actor": params["a"], "action": params["n"], "payload": json.loads(params["p"])})
            return Result()
        raise AssertionError(f"unexpected sql: {sql}")
