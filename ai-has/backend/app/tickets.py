import logging

from sqlalchemy import text

from . import audit
from .db import engine
from .timeutil import as_utc

PRIORITIES = {"low", "medium", "high"}
STATUSES = {"open", "in_progress", "resolved"}
DEFAULT_USER = "employee@company.com"

logger = logging.getLogger(__name__)


def _ref(ticket_id: int) -> str:
    return f"TCK-{ticket_id:04d}"


def _find_user_id(conn, user_email: str) -> int:
    """Id of the requested user. If that email is not in the database, use the first employee."""
    user_id = conn.execute(
        text("select id from users where email = :e"), {"e": user_email}
    ).scalar()
    if user_id is not None:
        return user_id

    user_id = conn.execute(
        text("select id from users where role = 'employee' order by id limit 1")
    ).scalar()
    if user_id is None:
        raise ValueError("No employee user found to own the ticket")
    logger.warning("Ticket user %s not found, using fallback employee user id %s", user_email, user_id)
    return user_id


def resolve_user(user_email: str) -> dict:
    """The id and real email of the user, with the same fallback as ticket creation."""
    with engine.connect() as conn:
        user_id = _find_user_id(conn, user_email)
        email = conn.execute(text("select email from users where id = :i"), {"i": user_id}).scalar()
    return {"id": user_id, "email": email}


def create_ticket(
    summary: str,
    priority: str = "medium",
    user_email: str = DEFAULT_USER,
    source: str = "ai",
    conversation_id: int | None = None,
) -> dict:
    if priority not in PRIORITIES:
        priority = "medium"

    with engine.begin() as conn:
        user_id = _find_user_id(conn, user_email)

        row = conn.execute(
            text(
                "insert into tickets (user_id, conversation_id, summary, priority, source) "
                "values (:u, :c, :s, :p, :src) "
                "returning id, status, priority, created_at"
            ),
            {"u": user_id, "c": conversation_id, "s": summary, "p": priority, "src": source},
        ).mappings().one()

    return {
        "id": row["id"],
        "ref": _ref(row["id"]),
        "summary": summary,
        "status": row["status"],
        "priority": row["priority"],
        "created_at": as_utc(row["created_at"]),
    }


def set_external_ref(ticket_id: int, external_ref: str) -> None:
    """Saves the key the Jira-style system gave to this ticket."""
    with engine.begin() as conn:
        conn.execute(
            text("update tickets set external_ref = :r where id = :id"),
            {"r": external_ref, "id": ticket_id},
        )


def list_tickets(status: str | None = None, limit: int = 50) -> list[dict]:
    sql = (
        "select t.id, t.summary, t.priority, t.status, t.source, "
        "t.created_at, t.resolved_at, t.external_ref, u.email, u.name "
        "from tickets t join users u on u.id = t.user_id "
    )
    params: dict = {"limit": limit}
    if status:
        sql += "where t.status = :status "
        params["status"] = status
    sql += "order by t.created_at desc limit :limit"

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    return [
        {
            "id": r["id"],
            "ref": _ref(r["id"]),
            "summary": r["summary"],
            "priority": r["priority"],
            "status": r["status"],
            "source": r["source"],
            "external_ref": r["external_ref"],  # Jira-style key such as HELP-1234, or None
            "created_at": as_utc(r["created_at"]),
            "resolved_at": as_utc(r["resolved_at"]),
            "requester": r["name"],
            "requester_email": r["email"],
        }
        for r in rows
    ]


def update_status(ticket_id: int, status: str, actor: str | None = None) -> dict | None:
    """Changes a ticket status. With an actor, the change is audited in the same transaction."""
    if status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(sorted(STATUSES))}")

    with engine.begin() as conn:
        old = conn.execute(
            text("select status from tickets where id = :id for update"), {"id": ticket_id}
        ).scalar()
        if old is None:
            return None
        row = conn.execute(
            text(
                "update tickets set status = :s, "
                "resolved_at = case when :s = 'resolved' then now() else null end "
                "where id = :id returning id, status, resolved_at"
            ),
            {"s": status, "id": ticket_id},
        ).mappings().first()
        if actor is not None:
            audit.record(actor, "ticket_status_change", {"ticket_id": ticket_id, "from": old, "to": status}, conn=conn)

    if row is None:
        return None
    return {
        "id": row["id"],
        "ref": _ref(row["id"]),
        "status": row["status"],
        "resolved_at": as_utc(row["resolved_at"]),
    }


def get_stats(tz: str = "UTC") -> dict:
    """Ticket numbers. "Created today" is the day that is running in the time zone `tz`."""
    with engine.connect() as conn:
        totals = conn.execute(
            text(
                "select "
                "count(*) as total, "
                "count(*) filter (where status = 'open') as open, "
                "count(*) filter (where status = 'in_progress') as in_progress, "
                "count(*) filter (where status = 'resolved') as resolved, "
                "count(*) filter (where created_at >= "
                "  ((date_trunc('day', now() at time zone :tz) at time zone :tz) at time zone 'UTC')) as today, "
                "avg(extract(epoch from (resolved_at - created_at)) / 3600) "
                "  filter (where status = 'resolved') as avg_hours "
                "from tickets"
            ),
            {"tz": tz},
        ).mappings().one()

        prio_rows = conn.execute(
            text("select priority, count(*) as n from tickets group by priority")
        ).mappings().all()

    by_priority = {"low": 0, "medium": 0, "high": 0}
    for r in prio_rows:
        by_priority[r["priority"]] = r["n"]

    avg = totals["avg_hours"]
    return {
        "total": totals["total"],
        "open": totals["open"],
        "in_progress": totals["in_progress"],
        "resolved": totals["resolved"],
        "today": totals["today"],
        "avg_resolution_hours": round(float(avg), 1) if avg is not None else None,
        "by_priority": by_priority,
    }