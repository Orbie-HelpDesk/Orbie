"""Index the knowledge base: split each article into chunks, embed them, store them.

Run with: python -m app.ingest   (refuses a database that is not db or localhost, see safety.py)

The functions here are shared with the knowledge base page (kb.py), so the script and the API
index an article in exactly the same way.
"""
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from sqlalchemy import text

from . import llm
from .config import settings
from .db import engine
from .safety import require_local_db

# max_retries=0: retrying (with a limit on the total time) is done in llm.py, not hidden inside the SDK
client = OpenAI(
    api_key=settings.gemini_api_key,
    base_url=settings.llm_base_url,
    timeout=settings.chat_timeout_seconds,
    max_retries=0,
)

EMBED_BATCH = 50  # texts per embedding call, so one big article stays under the provider's batch limit


DEFAULT_KB_DIR = "/kb"  # where Docker mounts the articles
BUNDLED_KB_DIR = Path(__file__).resolve().parent.parent / "kb"  # backend/kb (the Vercel upload)


def kb_dir() -> Path:
    """The folder with the markdown articles. Read at call time, so tests can point it elsewhere.

    If KB_DIR is left at its default and that folder does not exist, the uploaded copy is used.
    A KB_DIR that was set to something else is never replaced, so a wrong value still shows an error.
    """
    configured = Path(settings.kb_dir)
    if settings.kb_dir == DEFAULT_KB_DIR and not configured.is_dir() and BUNDLED_KB_DIR.is_dir():
        return BUNDLED_KB_DIR
    return configured


def is_bundled_kb() -> bool:
    """True when the articles come from the uploaded copy, which must not be changed."""
    return kb_dir().resolve() == BUNDLED_KB_DIR.resolve()


def chunk_text(content: str, max_chars: int = 800) -> list[str]:
    """Split text into chunks of about max_chars, keeping paragraphs together."""
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        if current and len(current) + len(p) > max_chars:
            chunks.append(current)
            current = p
        else:
            current = f"{current}\n\n{p}" if current else p
    if current:
        chunks.append(current)
    return chunks


def embed(texts: list[str]) -> list[list[float]]:
    """Embeddings for the texts, in order. Sent in batches of EMBED_BATCH, each through llm.py."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        res = llm.embeddings(
            client, input=texts[start : start + EMBED_BATCH], dimensions=settings.embed_dimensions
        )
        vectors += [d.embedding for d in res.data]
    return vectors


def title_of(content: str, fallback: str) -> str:
    """The title: the first line without its leading #, or the fallback (the file name) if that is empty."""
    lines = content.splitlines()
    return (lines[0].lstrip("# ").strip() if lines else "") or fallback


@dataclass
class Prepared:
    """An article that is chunked and embedded, ready to be stored."""

    source: str  # the file name, for example vpn-troubleshooting.md
    title: str
    chunks: list[str]
    vectors: list[list[float]]


def prepare_article(source: str, content: str) -> Prepared:
    """Chunk and embed one article. Calls the embedding provider (through llm.py) and touches no database."""
    title = title_of(content, Path(source).stem)
    chunks = chunk_text(content)
    vectors = embed([f"{title}\n{c}" for c in chunks])
    return Prepared(source=source, title=title, chunks=chunks, vectors=vectors)


def remove_article(conn, source: str) -> int:
    """Deletes an article and, through the foreign key, its chunks. Returns how many documents were removed."""
    return conn.execute(text("delete from kb_documents where source_path = :p"), {"p": source}).rowcount


def store_article(conn, prepared: Prepared, content: str | None = None) -> int:
    """Stores a prepared article in an open transaction. An older version of it is replaced. Returns the document id.

    The full text is saved only when it is given. Without it, the content column is not needed.
    """
    remove_article(conn, prepared.source)
    columns, values = "title, source_path, chunk_count", ":t, :p, :c"
    row = {"t": prepared.title, "p": prepared.source, "c": len(prepared.chunks)}
    if content is not None:
        columns, values, row["x"] = columns + ", content", values + ", :x", content
    doc_id = conn.execute(
        text(f"insert into kb_documents ({columns}) values ({values}) returning id"), row
    ).scalar_one()

    for i, (chunk, vec) in enumerate(zip(prepared.chunks, prepared.vectors)):
        conn.execute(
            text(
                "insert into kb_chunks (document_id, chunk_index, content, metadata, embedding) "
                "values (:d, :i, :c, cast(:m as jsonb), cast(:e as vector))"
            ),
            {
                "d": doc_id,
                "i": i,
                "c": chunk,
                "m": json.dumps({"title": prepared.title, "source": prepared.source}),
                "e": "[" + ",".join(str(x) for x in vec) + "]",
            },
        )
    return doc_id


OVERWRITE_FLAG = "--overwrite-web-edits"


def has_database_articles(conn) -> bool:
    """True when the database holds article text, so the database and not the files has the latest articles.

    Written with to_jsonb so that it also works on a database that does not have the content column yet.
    """
    return bool(
        conn.execute(
            text("select exists (select 1 from kb_documents d where to_jsonb(d) ->> 'content' is not null)")
        ).scalar()
    )


def ingest(overwrite: bool = False):
    files = sorted(kb_dir().glob("*.md"))
    if not files:
        print(f"No .md files found in {kb_dir()}")
        return

    with engine.begin() as conn:
        managed = has_database_articles(conn)  # checked before any embedding call
        if managed and not overwrite:
            raise SystemExit(
                "Refusing to index: this database stores the article text, so the files here may be older. "
                "Indexing could undo changes made on the knowledge base page and bring back deleted articles. "
                f"Pass {OVERWRITE_FLAG} only if the files really should replace them."
            )
        for f in files:
            content = f.read_text(encoding="utf-8")
            prepared = prepare_article(f.name, content)
            # re-ingesting the same file replaces the old version
            store_article(conn, prepared, content=content if managed else None)
            print(f"Indexed {f.name}: {len(prepared.chunks)} chunks")


if __name__ == "__main__":
    require_local_db(settings.database_url)
    ingest(overwrite=OVERWRITE_FLAG in sys.argv[1:])
