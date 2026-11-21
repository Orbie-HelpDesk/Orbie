"""Retention: remove the text of old chat messages, keep the numbers.

Run with: docker compose run --rm backend python -m app.purge_old --days 90
Add --dry-run to only count what would change.

For messages older than N days the text (content) is replaced by "[purged]". For every
conversation that has such a message, the title (made from the first question) is replaced
too. Rows, timestamps, citations, confidence, latency, feedback and the escalated flag stay, so
the analytics counts survive. Nothing is deleted. Tickets are not touched.

Refuses to run unless DATABASE_URL points at db or localhost (override: --allow-remote, only
when the team lead has said to change data on Supabase). The output has counts only, no text.
"""
import argparse
import sys

from sqlalchemy import text

from . import audit
from .chatlog import BLOCKED_TEXT, PURGED_TEXT
from .config import settings
from .db import engine
from .safety import ALLOW_FLAG, require_local_db

OLD = "created_at < now() - make_interval(days => :d)"
KEEP_TITLE = "Blocked message"  # not made from a question, so nothing personal to remove

MESSAGES_UPDATE = text(
    f"update messages set content = :p where {OLD} and content not in (:p, :b)"
)
MESSAGES_COUNT = text(
    f"select count(*) from messages where {OLD} and content not in (:p, :b)"
)
TITLES_WHERE = (
    f"title is distinct from :p and title <> :k and id in "
    f"(select conversation_id from messages where {OLD})"
)
TITLES_UPDATE = text(f"update conversations set title = :p where {TITLES_WHERE}")
TITLES_COUNT = text(f"select count(*) from conversations where {TITLES_WHERE}")


def purge(days: int, dry_run: bool = False) -> dict:
    """Purges (or, with dry_run, only counts) messages and titles older than `days` days."""
    params = {"d": days, "p": PURGED_TEXT, "b": BLOCKED_TEXT, "k": KEEP_TITLE}
    with engine.begin() as conn:
        if dry_run:
            messages = conn.execute(MESSAGES_COUNT, params).scalar()
            titles = conn.execute(TITLES_COUNT, params).scalar()
        else:
            # Titles first: they are found through the old messages, which stay in place either way
            titles = conn.execute(TITLES_UPDATE, params).rowcount
            messages = conn.execute(MESSAGES_UPDATE, params).rowcount
            audit.record(
                "cli", "messages_purged", {"days": days, "messages": messages, "conversations": titles}, conn=conn
            )
    return {"messages": int(messages), "titles": int(titles)}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="Remove the text of chat messages older than N days.")
    parser.add_argument("--days", type=int, required=True, help="age in days, at least 1")
    parser.add_argument("--dry-run", action="store_true", help="only count, change nothing")
    parser.add_argument(ALLOW_FLAG, action="store_true", help="allow a database that is not db or localhost")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    require_local_db(settings.database_url, argv)

    result = purge(args.days, args.dry_run)
    verb = "Would purge" if args.dry_run else "Purged"
    print(f"{verb} {result['messages']} message(s) and {result['titles']} title(s) older than {args.days} days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
