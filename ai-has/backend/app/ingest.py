import json
from pathlib import Path

from openai import OpenAI
from sqlalchemy import text

from .config import settings
from .db import engine
from .safety import require_local_db

client = OpenAI(api_key=settings.gemini_api_key, base_url=settings.llm_base_url)
KB_DIR = Path("/kb")


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
    res = client.embeddings.create(
        model=settings.embed_model,
        input=texts,
        dimensions=settings.embed_dimensions,
    )
    return [d.embedding for d in res.data]


def ingest():
    files = sorted(KB_DIR.glob("*.md"))
    if not files:
        print("No .md files found in /kb")
        return

    with engine.begin() as conn:
        for f in files:
            content = f.read_text(encoding="utf-8")
            title = content.splitlines()[0].lstrip("# ").strip() or f.stem
            chunks = chunk_text(content)
            vectors = embed([f"{title}\n{c}" for c in chunks])

            # re-ingesting the same file replaces the old version
            conn.execute(text("delete from kb_documents where source_path = :p"), {"p": f.name})
            doc_id = conn.execute(
                text(
                    "insert into kb_documents (title, source_path, chunk_count) "
                    "values (:t, :p, :c) returning id"
                ),
                {"t": title, "p": f.name, "c": len(chunks)},
            ).scalar_one()

            for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
                conn.execute(
                    text(
                        "insert into kb_chunks (document_id, chunk_index, content, metadata, embedding) "
                        "values (:d, :i, :c, cast(:m as jsonb), cast(:e as vector))"
                    ),
                    {
                        "d": doc_id,
                        "i": i,
                        "c": chunk,
                        "m": json.dumps({"title": title, "source": f.name}),
                        "e": "[" + ",".join(str(x) for x in vec) + "]",
                    },
                )
            print(f"Indexed {f.name}: {len(chunks)} chunks")


if __name__ == "__main__":
    require_local_db(settings.database_url)
    ingest()