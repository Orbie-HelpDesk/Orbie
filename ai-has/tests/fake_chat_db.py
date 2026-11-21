"""In-memory stand-in for the conversations and messages tables, for the chat saving tests."""
import json
from contextlib import contextmanager


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
                "intent": params["i"], "citations": json.loads(params["ci"]) if params["ci"] else None,
                "confidence": params["cf"], "latency_ms": params["l"], "escalated": params["e"],
                "blocked": params["b"], "feedback": None,
            })
            return Result(mid)
        if "update conversations set status = 'escalated'" in sql:
            db.conversations[params["c"]]["status"] = "escalated"
            return Result(rowcount=1)
        raise AssertionError(f"unexpected sql: {sql}")
