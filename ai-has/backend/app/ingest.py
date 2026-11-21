"""Index the knowledge base: split each article into chunks, embed them, store them.

Run with: python -m app.ingest   (refuses a database that is not db or localhost, see safety.py)

The functions here are shared with the knowledge base page (kb.py), so the script and the API
index an article in exactly the same way.
"""
import json
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


def kb_dir() -> Path:
    """The folder with the markdown articles. Read at call time, so tests can point it elsewhere."""
    return Path(settings.kb_dir)


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


def store_article(conn, prepared: Prepared) -> int:
    """Stores a prepared article in an open transaction. An older version of it is replaced. Returns the document id."""
    remove_article(conn, prepared.source)
    doc_id = conn.execute(
        text(
            "insert into kb_documents (title, source_path, chunk_count) "
            "values (:t, :p, :c) returning id"
        ),
        {"t": prepared.title, "p": prepared.source, "c": len(prepared.chunks)},
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


def ingest():
    files = sorted(kb_dir().glob("*.md"))
    if not files:
        print(f"No .md files found in {kb_dir()}")
        return

    with engine.begin() as conn:
        for f in files:
            prepared = prepare_article(f.name, f.read_text(encoding="utf-8"))
            store_article(conn, prepared)  # re-ingesting the same file replaces the old version
            print(f"Indexed {f.name}: {len(prepared.chunks)} chunks")


if __name__ == "__main__":
    require_local_db(settings.database_url)
    ingest()
