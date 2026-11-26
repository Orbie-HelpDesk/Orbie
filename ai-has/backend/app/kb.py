"""Knowledge base management: list, read, save (and re-index), delete, and test retrieval.

Used by the admin-only routes in main.py. The articles live in the kb_articles table
(slug, title, body), so the deployed site can change them. The search index is kb_documents and
kb_chunks, filled by the same functions as `python -m app.ingest` (ingest.prepare_article and
ingest.store_article).

Safety rules
- A name is lowercase letters, digits and dashes plus ".md", and not a Windows reserved name.
- An article is at most KB_MAX_BYTES, starts with a "# Title" line, and there are at most KB_MAX_ARTICLES.
- A save embeds first (a failure changes nothing), then writes the article, its index rows and its
  audit row in one database transaction.
- The audit row has the file name only, never the article text.
"""
import logging
import re

from sqlalchemy import text

from . import audit, ingest, rag
from .config import settings
from .db import engine
from .timeutil import as_utc

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}\.md$")
WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
SNIPPET_CHARS = 300
TEST_CHUNKS = 6
HEADING_RE = re.compile(r"^#\s+\S")


class KbError(Exception):
    """A request that cannot be done. The message is safe to show to the admin."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ---------- names ----------
def check_name(source: str) -> str:
    """The file name if it is allowed, else a 400. Only lowercase letters, digits, dashes and .md."""
    if not isinstance(source, str) or not NAME_RE.match(source) or source[:-3] in WINDOWS_RESERVED:
        raise KbError(
            400,
            "Use a file name with lowercase letters, digits and dashes, ending in .md (for example vpn-troubleshooting.md).",
        )
    return source


def read_only_reason() -> str | None:
    """Articles are stored in the database, so changes are always allowed here.

    The routes still require an admin, rate-limit the calls and write an audit row.
    """
    return None


def normalize_content(content: str) -> str:
    """The text to store: line endings and BOM cleaned, size and heading checked."""
    if not isinstance(content, str) or "\x00" in content:
        raise KbError(422, "The article must be plain text.")
    cleaned = content.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not cleaned.strip():
        raise KbError(422, "The article is empty.")
    if len(cleaned.encode("utf-8")) > settings.kb_max_bytes:
        raise KbError(413, f"The article is too long. The limit is {settings.kb_max_bytes} bytes.")
    first = next(line for line in cleaned.splitlines() if line.strip())
    if not HEADING_RE.match(first):
        raise KbError(422, "The first line must be a heading that starts with '# ', because it becomes the title.")
    return cleaned if cleaned.endswith("\n") else cleaned + "\n"


def info() -> dict:
    """What the page needs to know: the limits, and whether changes are allowed."""
    reason = read_only_reason()
    return {
        "max_bytes": settings.kb_max_bytes,
        "max_articles": settings.kb_max_articles,
        "max_distance": rag.MAX_DISTANCE,
        "editable": reason is None,
        "read_only_reason": reason,
        "name_pattern": NAME_RE.pattern,
    }


# ---------- reading ----------
def list_articles() -> list[dict]:
    """Every stored article and every indexed one, with a status: indexed, not_indexed or file_missing."""
    with engine.connect() as conn:
        docs = conn.execute(
            text("select source_path, title, chunk_count, indexed_at from kb_documents")
        ).mappings().all()
        arts = conn.execute(
            text("select slug, title, octet_length(body) as size from kb_articles")
        ).mappings().all()
    indexed = {r["source_path"]: r for r in docs}
    stored = {r["slug"]: r for r in arts}
    items = []
    for source in sorted(set(indexed) | set(stored)):
        doc = indexed.get(source)
        art = stored.get(source)
        items.append({
            "source": source,
            "title": (doc or art)["title"],
            "chunks": doc["chunk_count"] if doc else 0,
            "indexed_at": as_utc(doc["indexed_at"]) if doc else None,
            "size": art["size"] if art else None,
            # "file_missing" is the name the page already knows: indexed, but no stored article
            "status": "file_missing" if art is None else ("indexed" if doc else "not_indexed"),
        })
    return items


def read_article(source: str) -> dict:
    check_name(source)
    with engine.connect() as conn:
        row = conn.execute(
            text("select title, body from kb_articles where slug = :s"), {"s": source}
        ).mappings().first()
    if row is None:
        raise KbError(404, "Article not found.")
    body = row["body"]
    return {"source": source, "content": body, "size": len(body.encode("utf-8")), "title": row["title"]}


# ---------- changing ----------
def save_article(source: str, content: str, actor: str) -> dict:
    """Creates or updates an article and re-indexes only it. Raises KbError, or the provider error if embedding fails."""
    check_name(source)
    cleaned = normalize_content(content)

    with engine.connect() as conn:
        existed = conn.execute(
            text(
                "select exists (select 1 from kb_articles where slug = :s) "
                "or exists (select 1 from kb_documents where source_path = :s)"
            ),
            {"s": source},
        ).scalar()
        others = conn.execute(
            text("select count(*) from kb_articles where slug <> :s"), {"s": source}
        ).scalar()
    if not existed and others >= settings.kb_max_articles:
        raise KbError(409, f"The knowledge base already has {settings.kb_max_articles} articles.")

    prepared = ingest.prepare_article(source, cleaned)  # embeds first: if this fails nothing has changed

    with engine.begin() as conn:  # the article, its index rows and the audit row succeed or fail together
        ingest.store_article(conn, prepared)
        conn.execute(
            text(
                "insert into kb_articles (slug, title, body, updated_at) values (:s, :t, :b, now()) "
                "on conflict (slug) do update set title = excluded.title, body = excluded.body, updated_at = now()"
            ),
            {"s": source, "t": prepared.title, "b": cleaned},
        )
        audit.record(actor, "kb_update" if existed else "kb_create", {"file": source}, conn=conn)
    return {
        "source": source, "title": prepared.title, "chunks": len(prepared.chunks),
        "size": len(cleaned.encode("utf-8")), "created": not existed,
    }


def delete_article(source: str, actor: str) -> dict:
    """Removes the stored article and its index rows. 404 if neither exists."""
    check_name(source)
    with engine.begin() as conn:
        removed_article = conn.execute(text("delete from kb_articles where slug = :s"), {"s": source}).rowcount
        removed_index = ingest.remove_article(conn, source)
        if not removed_article and not removed_index:
            raise KbError(404, "Article not found.")
        audit.record(actor, "kb_delete", {"file": source}, conn=conn)
    return {"source": source, "deleted": True}


# ---------- testing ----------
def try_retrieval(question: str, source: str | None = None) -> dict:
    """One question through the same search the chat uses. One embedding call, no answer call."""
    if source is not None:
        check_name(source)
    found = rag.retrieve(question, k=TEST_CHUNKS)
    chunks = [
        {
            "source": c["metadata"].get("source"),
            "distance": round(c["distance"], 3),
            "score": round(c["score"], 5),
            "snippet": c["content"][:SNIPPET_CHARS],
            "from_this_article": source is not None and c["metadata"].get("source") == source,
        }
        for c in found
    ]
    best = min((c["distance"] for c in chunks), default=None)
    return {
        "question": question,
        "chunks": chunks,
        "best_distance": best,
        "max_distance": rag.MAX_DISTANCE,
        "would_answer": best is not None and best <= rag.MAX_DISTANCE,
        "this_article_found": any(c["from_this_article"] for c in chunks),
    }