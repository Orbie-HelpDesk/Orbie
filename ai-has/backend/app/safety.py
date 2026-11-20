"""Guard for scripts that write data, so nobody changes the shared Supabase by accident."""
import sys

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

LOCAL_HOSTS = {"db", "localhost"}
ALLOW_FLAG = "--allow-remote"


def require_local_db(database_url: str, argv: list[str] | None = None) -> None:
    """Exit with a clear message unless the database host is local, or --allow-remote is given."""
    argv = sys.argv[1:] if argv is None else argv
    if ALLOW_FLAG in argv:
        return
    try:
        host = make_url(database_url).host
    except ArgumentError:
        host = None
    if host in LOCAL_HOSTS:
        return
    shown = host or "(missing or invalid DATABASE_URL)"
    raise SystemExit(
        f"Refusing to write: DATABASE_URL host is {shown}, not db or localhost.\n"
        "This script changes data. Use the local-db profile (see README), "
        f"or pass {ALLOW_FLAG} if you really mean to write to a remote database."
    )
