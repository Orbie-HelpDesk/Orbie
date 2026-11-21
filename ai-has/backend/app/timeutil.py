"""Timestamps leave the API as timezone-aware ISO 8601, in UTC.

The database columns are "timestamp without time zone" and hold UTC (now() on a UTC server). The
driver returns them as plain datetimes, and a plain datetime is sent without an offset, which a
browser reads as local time. as_utc() marks them as UTC so the JSON carries +00:00.
"""
from datetime import UTC, datetime

from sqlalchemy import text

from .db import engine

DEFAULT_TZ = "UTC"
MAX_TZ_LENGTH = 64
KNOWN_TZ = text("select 1 from pg_timezone_names where name = :tz")


def check_timezone(tz: str | None) -> str:
    """A time zone name the database knows, for grouping by the viewer's day. None means UTC.

    The name is looked up in pg_timezone_names with a bound parameter. Only a name found there is
    returned, and it only ever reaches a query as a bound parameter, never as part of the SQL text.
    Raises ValueError for anything else.
    """
    if tz is None:
        return DEFAULT_TZ
    if not tz or len(tz) > MAX_TZ_LENGTH:
        raise ValueError("Unknown time zone.")
    with engine.connect() as conn:
        found = conn.execute(KNOWN_TZ, {"tz": tz}).scalar()
    if found is None:
        raise ValueError("Unknown time zone.")
    return tz


def as_utc(value: datetime | None) -> datetime | None:
    """A plain datetime is taken to be UTC. An aware one is converted to UTC. None stays None."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"as_utc needs a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
