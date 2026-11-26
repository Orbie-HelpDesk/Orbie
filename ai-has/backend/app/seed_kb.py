"""One-time load of the kb/*.md files into the kb_articles table.

Run from the ai-has folder (so .env is found):
    $env:PYTHONPATH = "backend"
    python -m app.seed_kb              # reads the ./kb folder
    python -m app.seed_kb path\\to\\kb   # or another folder

Only adds articles that are not in the table yet, so running it again never overwrites an edit
made in the admin page.
"""
import sys
from pathlib import Path

from sqlalchemy import text

from . import ingest
from .db import engine
from .kb import NAME_RE, KbError, normalize_content

CREATE = """
create table if not exists kb_articles (
    slug text primary key,
    title text not null,
    body text not null,
    updated_at timestamptz not null default now()
)
"""


def main() -> None:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "kb")
    if not folder.is_dir():
        sys.exit(f"Folder not found: {folder.resolve()}")
    added = skipped = 0
    with engine.begin() as conn:
        conn.execute(text(CREATE))
        conn.execute(text("alter table kb_articles enable row level security"))
        for path in sorted(folder.glob("*.md")):
            if not NAME_RE.match(path.name):
                print(f"skip (name not allowed): {path.name}")
                continue
            try:
                body = normalize_content(path.read_text(encoding="utf-8"))
            except KbError as e:
                print(f"skip {path.name}: {e.message}")
                continue
            result = conn.execute(
                text(
                    "insert into kb_articles (slug, title, body) values (:s, :t, :b) "
                    "on conflict (slug) do nothing"
                ),
                {"s": path.name, "t": ingest.title_of(body, path.stem), "b": body},
            )
            if result.rowcount:
                added += 1
            else:
                skipped += 1
    print(f"Done: {added} added, {skipped} already in the table.")


if __name__ == "__main__":
    main()