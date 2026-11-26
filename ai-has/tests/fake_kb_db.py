"""In-memory stand-ins for the knowledge base tables and for the embedding provider. No Gemini, no database."""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.exc import ProgrammingError

BASE = datetime(2026, 11, 22, 3, 0, 0)  # plain datetimes, like the database driver returns


class Result:
    def __init__(self, rows=None, value=None, rowcount=0):
        self._rows, self._value, self.rowcount = rows or [], value, rowcount

    def scalar_one(self):
        return self._value

    def scalar(self):
        return self._value

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class FakeKbDb:
    """kb_documents, kb_chunks and audit_logs. Opening a transaction copies the state, so a failure rolls back."""

    def __init__(self):
        self.docs = {}  # id -> row
        self.chunks = []  # rows
        self.audit = []
        self.fail_on = None  # a substring of a statement that should raise, to test a failing database
        self.commits = 0
        self.has_content_column = True  # False: a database where schema.sql was not applied again
        self.statements = []
        self._next = 1

    # ----- helpers for tests
    def add_doc(self, source, title="Title", chunks=2, indexed_at=None, content=None):
        doc_id = self._next
        self._next += 1
        self.docs[doc_id] = {
            "id": doc_id, "title": title, "source_path": source, "chunk_count": chunks,
            "indexed_at": indexed_at or BASE + timedelta(minutes=doc_id), "content": content,
        }
        for i in range(chunks):
            self.chunks.append({"document_id": doc_id, "chunk_index": i, "content": f"{source} chunk {i}", "metadata": {}, "embedding": []})
        return doc_id

    def sources(self):
        return sorted(d["source_path"] for d in self.docs.values())

    def chunks_of(self, source):
        ids = [d["id"] for d in self.docs.values() if d["source_path"] == source]
        return [c for c in self.chunks if c["document_id"] in ids]

    # ----- the engine interface
    @contextmanager
    def connect(self):
        yield FakeKbConn(self)

    @contextmanager
    def begin(self):
        snapshot = (dict(self.docs), list(self.chunks), list(self.audit), self._next)
        try:
            yield FakeKbConn(self)
            self.commits += 1
        except Exception:
            self.docs, self.chunks, self.audit, self._next = snapshot  # roll back
            raise


class FakeKbConn:
    def __init__(self, db):
        self.db = db

    def execute(self, stmt, params=None):
        sql, db = " ".join(str(stmt).split()), self.db
        db.statements.append(sql)
        if db.fail_on and db.fail_on in sql:
            raise RuntimeError("database failure (test)")
        if sql.startswith("select exists (select 1 from kb_documents d where to_jsonb(d)"):
            return Result(value=any(d["content"] is not None for d in db.docs.values()))
        if not db.has_content_column and "content" in sql and "kb_documents" in sql:  # kb_chunks has its own
            orig = Exception('column "content" does not exist')
            orig.pgcode = "42703"
            raise ProgrammingError(sql, params, orig)
        if sql.startswith("select pg_advisory_xact_lock"):
            return Result()
        if sql == "select content from kb_documents limit 0":
            return Result()
        if sql.startswith("select content from kb_documents where source_path"):
            rows = [d for d in db.docs.values() if d["source_path"] == params["p"]]
            return Result(rows=[{"content": d["content"]} for d in sorted(rows, key=lambda d: -d["id"])])
        if sql == "select source_path from kb_documents":
            return Result(rows=[{"source_path": d["source_path"]} for d in db.docs.values()])
        if sql.startswith("select source_path, title, chunk_count, indexed_at, octet_length(content)"):
            rows = sorted(db.docs.values(), key=lambda d: (d["source_path"], d["id"]))
            return Result(rows=[
                {**d, "size": None if d["content"] is None else len(d["content"].encode("utf-8"))} for d in rows
            ])
        if sql.startswith("select id, source_path, content is not null as has_text"):
            return Result(rows=[
                {"id": i, "source_path": d["source_path"], "has_text": d["content"] is not None}
                for i, d in sorted(db.docs.items())
            ])
        if sql.startswith("select content from kb_chunks where document_id"):
            rows = sorted((c for c in db.chunks if c["document_id"] == params["d"]), key=lambda c: c["chunk_index"])
            return Result(rows=[{"content": c["content"]} for c in rows])
        if sql.startswith("update kb_documents set content"):
            doc = db.docs[params["d"]]
            if doc["content"] is not None:
                return Result()
            db.docs[params["d"]] = {**doc, "content": params["x"]}  # a copy, so a rollback restores the old row
            return Result(rowcount=1)
        if sql.startswith("delete from kb_documents where source_path"):
            ids = [i for i, d in db.docs.items() if d["source_path"] == params["p"]]
            for i in ids:
                del db.docs[i]
            db.chunks = [c for c in db.chunks if c["document_id"] not in ids]
            return Result(rowcount=len(ids))
        if sql.startswith("insert into kb_documents"):
            doc_id = db._next
            db._next += 1
            db.docs[doc_id] = {
                "id": doc_id, "title": params["t"], "source_path": params["p"], "chunk_count": params["c"],
                "indexed_at": BASE + timedelta(hours=doc_id), "content": params.get("x"),
            }
            return Result(value=doc_id)
        if sql.startswith("insert into kb_chunks"):
            db.chunks.append({
                "document_id": params["d"], "chunk_index": params["i"], "content": params["c"],
                "metadata": json.loads(params["m"]), "embedding": json.loads(params["e"]),
            })
            return Result()
        if sql.startswith("insert into audit_logs"):
            db.audit.append({"actor": params["a"], "action": params["n"], "payload": json.loads(params["p"])})
            return Result()
        if sql.startswith("select source_path, title, chunk_count, indexed_at from kb_documents"):
            rows = sorted(db.docs.values(), key=lambda d: d["source_path"])
            return Result(rows=[dict(r) for r in rows])
        raise AssertionError(f"unexpected sql: {sql}")


class FakeEmbedder:
    """Stands in for the OpenAI client: embeddings.create returns one small vector per text. Records the calls."""

    def __init__(self, dimensions=4):
        self.calls, self.error, self.dimensions = [], None, dimensions
        self.embeddings = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        texts = kwargs["input"]
        return SimpleNamespace(data=[SimpleNamespace(embedding=[float(len(t) % 7)] * self.dimensions) for t in texts])
