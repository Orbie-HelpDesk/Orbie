"""One-time step before KB_STORE=database: save the text of the indexed articles in the database.

Run with: python -m app.import_articles   (refuses a database that is not db or localhost, see safety.py)

It only fills content where it is empty, so it never overwrites an edit and is safe to run again.
Every file must be indexed with the same text. If one is not, nothing is changed. No AI calls.
"""
from sqlalchemy import text

from . import ingest, kb
from .config import settings
from .db import engine
from .safety import require_local_db


def _unix(value: str) -> str:
    return value.replace("\r\n", "\n")


def import_articles() -> None:
    files = sorted(ingest.kb_dir().glob("*.md"))
    if not files:
        raise SystemExit(f"No .md files found in {ingest.kb_dir()}")

    with engine.begin() as conn:
        rows = conn.execute(
            text("select id, source_path, content is not null as has_text from kb_documents order by id")
        ).mappings().all()
        documents: dict[str, list] = {}
        for row in rows:
            documents.setdefault(row["source_path"], []).append(row)

        todo, problems, skipped = [], [], 0
        for f in files:
            found = documents.get(f.name, [])
            if len(found) != 1:
                problems.append(f"{f.name}: " + ("indexed more than once" if found else "not indexed"))
                continue
            if found[0]["has_text"]:
                skipped += 1
                continue
            content = kb.normalize_content(f.read_text(encoding="utf-8"))
            stored = conn.execute(
                text("select content from kb_chunks where document_id = :d order by chunk_index"),
                {"d": found[0]["id"]},
            ).mappings().all()
            if [_unix(c["content"]) for c in stored] != ingest.chunk_text(content):
                problems.append(f"{f.name}: the file differs from the indexed text")
                continue
            todo.append((found[0]["id"], f.name, content))

        if problems:
            raise SystemExit("Nothing was changed. Fix these first:\n  " + "\n  ".join(problems))

        for doc_id, _, content in todo:
            conn.execute(
                text("update kb_documents set content = :x where id = :d and content is null"),
                {"x": content, "d": doc_id},
            )

    for _, name, _ in todo:
        print(f"Saved the text of {name}")
    print(f"Done: {len(todo)} saved, {skipped} already had text.")
    for source in sorted(set(documents) - {f.name for f in files}):
        print(f"No file for {source}, so no text was saved. Delete it on the knowledge base page if it is not needed.")


if __name__ == "__main__":
    require_local_db(settings.database_url)
    import_articles()
