"""Guard for scripts that write data, so nobody changes the shared Supabase by accident."""
import re
import sys

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

LOCAL_HOSTS = {"db", "localhost"}
ALLOW_FLAG = "--allow-remote"
# Query parameters that psycopg2 uses instead of the host in the URL.
HOST_QUERY_KEYS = ("host", "hostaddr")
SAFE_TO_SHOW = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def _hosts(database_url: str) -> list[str | None]:
    """Every host the engine could connect to. Parsed by SQLAlchemy, like the engine does."""
    try:
        url = make_url(database_url)
    except ArgumentError:
        return [None]
    hosts = [url.host]
    for key in HOST_QUERY_KEYS:
        value = url.query.get(key)
        if value is None:
            continue
        # a repeated query parameter comes back as a tuple
        hosts.extend(value if isinstance(value, tuple) else [value])
    return hosts


def require_local_db(database_url: str, argv: list[str] | None = None) -> None:
    """Exit with a clear message unless every database host is local, or --allow-remote is given.

    The message never contains the URL or the password.
    """
    argv = sys.argv[1:] if argv is None else argv
    if ALLOW_FLAG in argv:
        return
    hosts = _hosts(database_url)
    if hosts and all(h in LOCAL_HOSTS for h in hosts):
        return
    if not all(hosts):
        # An empty host makes libpq fall back to PGHOST or a local socket, so it is not checkable.
        raise SystemExit(
            "Refusing to write: DATABASE_URL has a missing or empty host, so the real target "
            "would come from PGHOST or a default.\n"
            "Set an explicit host (db or localhost), or pass "
            f"{ALLOW_FLAG} if you really mean to write to a remote database."
        )
    bad = next(h for h in hosts if h not in LOCAL_HOSTS)
    # A badly written password can end up inside the parsed host, so only show plain hostnames.
    shown = bad if bad and SAFE_TO_SHOW.match(bad) else "(missing, invalid or not a plain hostname)"
    raise SystemExit(
        f"Refusing to write: DATABASE_URL host is {shown}, not db or localhost.\n"
        "This script changes data. Use the local-db profile (see README), "
        f"or pass {ALLOW_FLAG} if you really mean to write to a remote database."
    )
