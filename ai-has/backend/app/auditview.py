"""What the audit log page may show, and the query behind it.

Two rules shape this module.
1. A row is never shown as stored. The actor is shown only if it looks like one of our own actors
   (user:<id>, anonymous, cli, ai), and the payload is never sent at all: a one-line summary is built
   from a whitelist, and every value in it is checked (an integer, a word from a fixed list, or a
   knowledge base file name). Anything else is left out, so an email, a password or message text that
   ever ended up in a payload cannot reach this page.
2. The query text is made only of fixed pieces. Every value from a request (action, actor, dates, time
   zone, page) is a bound parameter. A request value is never added to the SQL text.
"""
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text

from . import audit
from .db import engine
from .timeutil import as_utc

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100  # hard maximum
MAX_PAGE = 2000  # keeps the offset bounded: at most 2000 * 100 rows are skipped
MAX_ACTOR_FILTER = 80

# The actions the app writes, for the filter dropdown. Other action names can still be filtered by hand.
KNOWN_ACTIONS = (
    "login", "login_failed", "logout", "ticket_status_change", "create_ticket", "reset_password",
    "conversation_read", "conversation_deleted", "password_set", "kb_create", "kb_update", "kb_delete",
    "messages_purged", "demo_seeded", "audit_viewed",
)

ACTION_RE = re.compile(r"^[a-z][a-z_]{0,59}$")
ACTOR_RE = re.compile(r"^(user:\d{1,9}|anonymous|cli|ai)$")
FILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}\.md$")  # the same pattern as kb.NAME_RE

ROLES = frozenset({"employee", "agent", "admin"})
STATUSES = frozenset({"open", "in_progress", "resolved"})
PRIORITIES = frozenset({"low", "medium", "high"})
DECISIONS = frozenset({"confirmed", "cancelled"})
OUTCOMES = frozenset({"done", "failed"})
JIRA = frozenset({"synced", "pending"})
RESET = frozenset({"reset_link_sent"})
FAILURE_REASONS = frozenset({"bad_credentials"})
NO_SUMMARY = "(no summary)"
HIDDEN = "(hidden)"
OTHER = "(other)"


# ---------- showing a row ----------
def safe_actor(actor) -> str:
    """The actor if it is one of ours. Anything else, such as an email from an old row, is hidden."""
    return actor if isinstance(actor, str) and ACTOR_RE.match(actor) else HIDDEN


def safe_action(action) -> str:
    return action if isinstance(action, str) and ACTION_RE.match(action) else OTHER


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**31 else None


def _word(value, allowed: frozenset) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _join(parts) -> str:
    return ", ".join(p for p in parts if p)


def _action_summary(payload: dict) -> str:
    """Create ticket and reset password rows: the decision, how it ended and a few checked facts."""
    ticket = _int(payload.get("ticket_id"))
    return _join([
        _word(payload.get("decision"), DECISIONS),
        _word(payload.get("outcome"), OUTCOMES),
        f"ticket {ticket}" if ticket is not None else None,
        f"{p} priority" if (p := _word(payload.get("priority"), PRIORITIES)) else None,
        f"jira {j}" if (j := _word(payload.get("jira_sync"), JIRA)) else None,
        _word(payload.get("status"), RESET),
    ])


def _status_change(payload: dict) -> str:
    ticket = _int(payload.get("ticket_id"))
    old, new = _word(payload.get("from"), STATUSES), _word(payload.get("to"), STATUSES)
    if ticket is None:
        return ""
    return f"ticket {ticket}: {old} to {new}" if old and new else f"ticket {ticket}"


def _file(payload: dict) -> str:
    name = payload.get("file")
    return f"file {name}" if isinstance(name, str) and FILE_RE.match(name) else ""


def _conversation(payload: dict) -> str:
    cid = _int(payload.get("conversation_id"))
    return f"conversation {cid}" if cid is not None else ""


def _purged(payload: dict) -> str:
    days, messages, conversations = (_int(payload.get(k)) for k in ("days", "messages", "conversations"))
    if None in (days, messages, conversations):
        return ""
    return f"{messages} messages and {conversations} conversations older than {days} days"


def _demo(payload: dict) -> str:
    counts = [_int(payload.get(k)) for k in ("tickets", "conversations", "messages")]
    if None in counts or payload.get("synthetic") is not True:
        return ""
    return f"synthetic demo data: {counts[0]} tickets, {counts[1]} conversations, {counts[2]} messages"


SUMMARIES = {
    "login": lambda p: f"role {r}" if (r := _word(p.get("role"), ROLES)) else "",
    "login_failed": lambda p: f"reason {r}" if (r := _word(p.get("reason"), FAILURE_REASONS)) else "",
    "logout": lambda p: "",
    "ticket_status_change": _status_change,
    "create_ticket": _action_summary,
    "reset_password": _action_summary,
    "conversation_read": _conversation,
    "conversation_deleted": _conversation,
    "password_set": lambda p: f"user {u}" if (u := _int(p.get("user_id"))) is not None else "",
    "kb_create": _file,
    "kb_update": _file,
    "kb_delete": _file,
    "messages_purged": _purged,
    "demo_seeded": _demo,
    "audit_viewed": lambda p: "",
}


def summary(action, payload) -> str:
    """A one-line summary built from checked values only. The payload itself is never returned."""
    build = SUMMARIES.get(action) if isinstance(action, str) else None
    if build is None:
        return NO_SUMMARY
    return build(payload if isinstance(payload, dict) else {})


def to_item(row) -> dict:
    return {
        "id": row["id"],
        "time": as_utc(row["created_at"]),
        "actor": safe_actor(row["actor"]),
        "action": safe_action(row["action"]),
        "summary": summary(row["action"], row["payload"]),
    }


def page_of(rows, page_size: int) -> tuple[list[dict], bool]:
    """The rows of one page and whether there is an older page. The query asks for one row more than it shows."""
    return [to_item(r) for r in rows[:page_size]], len(rows) > page_size


# ---------- the query ----------
@dataclass(frozen=True)
class Filters:
    action: str | None = None
    actor: str | None = None
    date_from: date | None = None
    date_to: date | None = None  # inclusive
    tz: str = "UTC"  # already checked against pg_timezone_names (timeutil.check_timezone)


def escape_like(value: str) -> str:
    """So that % and _ typed by the admin match themselves and do not act as wildcards."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Fixed pieces. A request value is never put into any of these strings.
SELECT_SQL = "select id, created_at, actor, action, payload from audit_logs"
ACTION_COND = "action = :action"
ACTOR_COND = "actor ilike :actor escape '\\'"
FROM_COND = "created_at >= (cast(:date_from as date)::timestamp at time zone :tz) at time zone 'UTC'"
TO_COND = "created_at < ((cast(:date_to as date) + 1)::timestamp at time zone :tz) at time zone 'UTC'"
ORDER_SQL = " order by created_at desc, id desc limit :limit offset :offset"


def build_query(filters: Filters, page: int, page_size: int):
    """The statement and its bound parameters for one page (newest first)."""
    conditions: list[str] = []
    params: dict = {"limit": page_size + 1, "offset": (page - 1) * page_size}
    if filters.action is not None:
        conditions.append(ACTION_COND)
        params["action"] = filters.action
    if filters.actor is not None:
        conditions.append(ACTOR_COND)
        params["actor"] = f"%{escape_like(filters.actor)}%"
    if filters.date_from is not None:
        conditions.append(FROM_COND)
        params["date_from"] = filters.date_from
    if filters.date_to is not None:
        conditions.append(TO_COND)
        params["date_to"] = filters.date_to
    if filters.date_from is not None or filters.date_to is not None:
        params["tz"] = filters.tz
    where = (" where " + " and ".join(conditions)) if conditions else ""
    return text(SELECT_SQL + where + ORDER_SQL), params


def read_page(admin_id: int, filters: Filters, page: int, page_size: int) -> dict:
    """One page of the log. The read is audited in the same transaction, and if that row cannot be
    written the read fails and nothing is returned, so there is never an unlogged read.

    The audit_viewed row has the actor only. It never holds the filters.
    """
    statement, params = build_query(filters, page, page_size)
    with engine.begin() as conn:
        rows = conn.execute(statement, params).mappings().all()
        audit.record(audit.actor_for(admin_id), "audit_viewed", {}, conn=conn)
    items, has_next = page_of(rows, page_size)
    return {"items": items, "page": page, "page_size": page_size, "has_next": has_next}
