"""In-memory stand-in for the conversations and messages tables, for the chat saving tests."""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta

BASE = datetime(2026, 11, 21, 3, 0, 0)  # plain, like the database driver returns


class Result:
    def __init__(self, value=None, rows=None, rowcount=0):
        self._value, self._rows, self.rowcount = value, rows or [], rowcount

    def scalar(self):
        return self._value

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class FakeChatDb:
    def __init__(self):
        self.conversations = {}  # id -> dict
        self.messages = []  # dicts with id, conversation_id, role, content, ...
        self.tickets = {}  # ticket id -> conversation id
        self.audit = []
        self.broken = False
        self._next_conversation = 1
        self._next_message = 1

    def add_conversation(self, user_id, title="Old chat", status="open"):
        cid = self._next_conversation
        self._next_conversation += 1
        self.conversations[cid] = {"id": cid, "user_id": user_id, "title": title, "status": status}
        return cid

    @contextmanager
    def connect(self):
        if self.broken:
            raise RuntimeError("database down")
        yield FakeChatConn(self)

    @contextmanager
    def begin(self):
        if self.broken:
            raise RuntimeError("database down")
        yield FakeChatConn(self)

    def dump(self):
        return json.dumps({"c": self.conversations, "m": self.messages}, default=str)


class FakeChatConn:
    def __init__(self, db):
        self.db = db

    def execute(self, stmt, params=None):
        sql, db = str(stmt), self.db
        if "select 1 from conversations where id" in sql:
            row = db.conversations.get(params["c"])
            return Result(1 if row and row["user_id"] == params["u"] else None)
        if "insert into conversations" in sql:
            return Result(db.add_conversation(params["u"], params["t"]))
        if "insert into messages" in sql:
            mid = db._next_message
            db._next_message += 1
            db.messages.append({
                "id": mid, "conversation_id": params["c"], "role": params["r"], "content": params["t"],
                "intent": params["i"], "outcome": params["o"], "citations": json.loads(params["ci"]) if params["ci"] else None,
                "confidence": params["cf"], "latency_ms": params["l"], "escalated": params["e"],
                "blocked": params["b"], "feedback": None,
                "created_at": BASE + timedelta(seconds=mid),
            })
            return Result(mid)
        if "update conversations set status = 'escalated'" in sql:
            db.conversations[params["c"]]["status"] = "escalated"
            return Result(rowcount=1)
        if "select coalesce(outcome, intent) from messages" in sql:
            mine = [m for m in db.messages if m["conversation_id"] == params["c"] and m["role"] == "assistant"]
            return Result(mine[-1].get("outcome") or mine[-1]["intent"] if mine else None)
        if "update messages set feedback" in sql:
            msg = next((m for m in db.messages if m["id"] == params["m"]), None)
            owner = db.conversations.get(msg["conversation_id"], {}).get("user_id") if msg else None
            if msg and msg["role"] == "assistant" and owner == params["u"]:
                msg["feedback"] = params["v"]
                return Result(rowcount=1)
            return Result(rowcount=0)
        if "from conversations c where c.user_id" in sql:
            rows = []
            for c in db.conversations.values():
                if c["user_id"] != params["u"]:
                    continue
                mine = [m for m in db.messages if m["conversation_id"] == c["id"]]
                rows.append({
                    "id": c["id"], "title": c["title"], "status": c["status"], "created_at": c.get("created_at", BASE),
                    "questions": sum(1 for m in mine if m["role"] == "user"),
                    "last_at": mine[-1]["created_at"] if mine else None,
                })
            rows.sort(key=lambda r: (r["last_at"] is not None, r["last_at"] or BASE, r["id"]), reverse=True)
            return Result(rows=rows[: params["n"]])
        if "select id, user_id, title, status, created_at from conversations" in sql:
            c = db.conversations.get(params["c"])
            return Result(rows=[{**c, "created_at": c.get("created_at", BASE)}] if c else [])
        if "from messages where conversation_id = :c order by id" in sql:
            return Result(rows=[m for m in db.messages if m["conversation_id"] == params["c"]])
        if "update tickets set conversation_id = null" in sql:
            for tid, cid in db.tickets.items():
                if cid == params["c"]:
                    db.tickets[tid] = None
            return Result()
        if "delete from messages" in sql:
            db.messages = [m for m in db.messages if m["conversation_id"] != params["c"]]
            return Result()
        if "delete from conversations" in sql:
            db.conversations.pop(params["c"], None)
            return Result()
        if "insert into audit_logs" in sql:
            db.audit.append({"actor": params["a"], "action": params["n"], "payload": json.loads(params["p"])})
            return Result()
        raise AssertionError(f"unexpected sql: {sql}")
