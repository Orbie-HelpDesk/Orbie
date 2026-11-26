"""Knowledge base management: list, read, save (and re-index), delete, and test retrieval.

Used by the admin-only routes in main.py. The articles are markdown files in the KB folder
(settings.kb_dir, /kb in Docker). The index is kb_documents and kb_chunks, filled by the same
functions as `python -m app.ingest` (ingest.prepare_article and ingest.store_article).

Safety rules
- A name is lowercase letters, digits and dashes plus ".md" (so also no two names that differ only by
  case on a case-insensitive disk), and not a Windows reserved name. The resolved path must sit
  directly inside the KB folder, and a symlink is never followed.
- An article is at most KB_MAX_BYTES, starts with a "# Title" line, and there are at most KB_MAX_ARTICLES.
- Changing data is refused unless the database is local (db or localhost) or KB_EDIT_ALLOW_REMOTE is
  true, because the files live in one checkout and the index may be shared.
- A save embeds first (a failure changes nothing), writes a temp file, commits the database change
  together with its audit row, and only then moves the file into place.
- The audit row has the file name only, never the article text.
"""
import logging
import os
import re
import secrets
from pathlib import Path

from sqlalchemy import text

from . import audit, ingest, rag
from .config import settings
from .db import engine
from .safety import is_local_db
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


# ---------- names and paths ----------
def check_name(source: str) -> str:
    """The file name if it is allowed, else a 400. Only lowercase letters, digits, dashes and .md."""
    if not isinstance(source, str) or not NAME_RE.match(source) or source[:-3] in WINDOWS_RESERVED:
        raise KbError(
            400,
            "Use a file name with lowercase letters, digits and dashes, ending in .md (for example vpn-troubleshooting.md).",
        )
    return source


def folder() -> Path:
    path = ingest.kb_dir()
    if not path.is_dir():
        raise KbError(503, "The knowledge base folder is not available on the server.")
    return path.resolve()


def path_for(source: str) -> Path:
    """The file for this article. It must be directly inside the KB folder and not a symlink."""
    check_name(source)
    base = folder()
    path = (base / source).resolve()
    if path.parent != base:
        raise KbError(400, "That file name is not allowed.")
    if (base / source).is_symlink():
        raise KbError(400, "That file name is not allowed.")
    return path


BUNDLED_MESSAGE = (
    "This site shows a copy of the articles that came with the deployment, so the page is read only. "
    "Change articles in the project's kb folder, then deploy again."
)
REMOTE_MESSAGE = (
    "Editing the knowledge base is switched off for this database, so the page is read only. "
    "Only the team lead should enable KB_EDIT_ALLOW_REMOTE on a shared database."
)


def read_only_reason() -> str | None:
    """Why changes are refused, or None when they are allowed."""
    if ingest.is_bundled_kb():  # first, because KB_EDIT_ALLOW_REMOTE cannot make this copy writable
        return BUNDLED_MESSAGE
    if settings.kb_edit_allow_remote or is_local_db(settings.database_url):
        return None
    return REMOTE_MESSAGE


def require_editable() -> None:
    """Changes need the real KB folder, and a local database unless KB_EDIT_ALLOW_REMOTE is switched on."""
    reason = read_only_reason()
    if reason:
        raise KbError(403, reason)


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


def _article_files() -> list[Path]:
    base = folder()
    return sorted(p for p in base.glob("*.md") if NAME_RE.match(p.name) and not p.is_symlink())


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
    """Every article on disk and in the index, with a status: indexed, not_indexed or file_missing."""
    with engine.connect() as conn:
        rows = conn.execute(text("select source_path, title, chunk_count, indexed_at from kb_documents")).mappings().all()
    indexed = {r["source_path"]: r for r in rows}
    items = []
    on_disk = set()
    for path in _article_files():
        on_disk.add(path.name)
        row = indexed.get(path.name)
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        items.append({
            "source": path.name,
            "title": row["title"] if row else ingest.title_of(first[0] if first else "", path.stem),
            "chunks": row["chunk_count"] if row else 0,
            "indexed_at": as_utc(row["indexed_at"]) if row else None,
            "size": path.stat().st_size,
            "status": "indexed" if row else "not_indexed",
        })
    for source, row in indexed.items():
        if source not in on_disk:
            items.append({
                "source": source, "title": row["title"], "chunks": row["chunk_count"],
                "indexed_at": as_utc(row["indexed_at"]), "size": None, "status": "file_missing",
            })
    return sorted(items, key=lambda i: i["source"])


def read_article(source: str) -> dict:
    path = path_for(source)
    if not path.is_file():
        raise KbError(404, "Article not found.")
    content = path.read_text(encoding="utf-8")
    return {"source": source, "content": content, "size": len(content.encode("utf-8")), "title": ingest.title_of(content, path.stem)}


# ---------- changing ----------
def save_article(source: str, content: str, actor: str) -> dict:
    """Creates or updates an article and re-indexes only it. Raises KbError, or the provider error if embedding fails."""
    path = path_for(source)
    require_editable()
    cleaned = normalize_content(content)
    existed = path.is_file()
    if not existed and len([p for p in _article_files() if p.name != source]) >= settings.kb_max_articles:
        raise KbError(409, f"The knowledge base already has {settings.kb_max_articles} articles.")

    prepared = ingest.prepare_article(source, cleaned)  # embeds first: if this fails nothing has changed

    temp = path.with_name(f".{source}.{secrets.token_hex(4)}.tmp")
    with open(temp, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(cleaned)
    try:
        with engine.begin() as conn:
            ingest.store_article(conn, prepared)
            audit.record(actor, "kb_update" if existed else "kb_create", {"file": source}, conn=conn)
    except Exception:
        temp.unlink(missing_ok=True)  # the old article is untouched
        raise
    try:
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        logger.exception("The index for %s was saved but the file could not be written", source)
        raise KbError(500, "The article was indexed but its file could not be written. Save it again.") from None
    return {
        "source": source, "title": prepared.title, "chunks": len(prepared.chunks),
        "size": len(cleaned.encode("utf-8")), "created": not existed,
    }


def delete_article(source: str, actor: str) -> dict:
    """Removes the article's index rows and its file. 404 if neither exists."""
    path = path_for(source)
    require_editable()
    has_file = path.is_file()
    with engine.begin() as conn:
        removed = ingest.remove_article(conn, source)
        if not has_file and not removed:
            raise KbError(404, "Article not found.")
        audit.record(actor, "kb_delete", {"file": source}, conn=conn)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.exception("The index for %s was removed but its file could not be deleted", source)
        raise KbError(500, "The article was removed from the index but its file could not be deleted.") from None
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
