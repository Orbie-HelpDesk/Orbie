"""Timestamps leave the API as timezone-aware ISO 8601, in UTC.

The database columns are "timestamp without time zone" and hold UTC (now() on a UTC server). The
driver returns them as plain datetimes, and a plain datetime is sent without an offset, which a
browser reads as local time. as_utc() marks them as UTC so the JSON carries +00:00.
"""
from datetime import UTC, datetime


def as_utc(value: datetime | None) -> datetime | None:
    """A plain datetime is taken to be UTC. An aware one is converted to UTC. None stays None."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"as_utc needs a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
