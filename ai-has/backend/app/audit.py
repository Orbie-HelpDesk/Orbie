"""Writes audit_logs rows. Payloads must never hold an email, a name or free text."""
import json
import logging

from sqlalchemy import text

from .db import engine

logger = logging.getLogger(__name__)

INSERT = text("insert into audit_logs (actor, action, payload) values (:a, :n, cast(:p as json))")


def actor_for(user_id: int | None) -> str:
    return f"user:{user_id}" if user_id is not None else "anonymous"


def record(actor: str, action: str, payload: dict | None = None, conn=None) -> None:
    """Writes one row. With a connection it joins that transaction and errors are raised.

    Without one it writes on its own, and a failure is logged and swallowed, so a broken
    audit table cannot stop a login or a logout.
    """
    params = {"a": actor, "n": action, "p": json.dumps(payload or {})}
    if conn is not None:
        conn.execute(INSERT, params)
        return
    try:
        with engine.begin() as own:
            own.execute(INSERT, params)
    except Exception:
        logger.exception("Could not write the audit log for %s", action)
