"""Saves what happens in the chat: conversations and messages.

Only PII-redacted text is ever saved. A blocked message is saved as a fixed placeholder, never
as the raw text. The id of a proposed action is not saved, because it works like a one-time key.
"""
import json
import logging

from sqlalchemy import text

from . import audit
from .db import engine
from .timeutil import as_utc

logger = logging.getLogger(__name__)

BLOCKED_TEXT = "[blocked message]"
PURGED_TEXT = "[purged]"  # what purge_old leaves in place of old message text
TITLE_LENGTH = 60
# Same scale as the confidence gauge in the chat: distance 0.5 is 0 percent, 0.25 is 100 percent.
CONFIDENCE_FAR = 0.5
CONFIDENCE_RANGE = 0.25

# What happened to a message (messages.outcome). Old rows hold these values in messages.intent.
OUTCOME_ANSWERED = "answered"
OUTCOME_ESCALATED = "escalated"
OUTCOME_ACTION = "action_proposed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_REFUSED = "refused"
OUTCOME_CLARIFY = "clarify"

INTENT_ANSWERED = OUTCOME_ANSWERED  # the older names, still used by the route
INTENT_ESCALATED = OUTCOME_ESCALATED
INTENT_ACTION = OUTCOME_ACTION
INTENT_BLOCKED = OUTCOME_BLOCKED


def confidence_from_distances(distances: list[float] | None) -> float | None:
    """0 to 1 from the best (smallest) distance, or None when nothing was searched."""
    if not distances:
        return None
    value = (CONFIDENCE_FAR - min(distances)) / CONFIDENCE_RANGE
    return round(max(0.0, min(1.0, value)), 3)


def title_from(message: str) -> str:
    """A short title for the history list, from the first (already redacted) question."""
    one_line = " ".join(message.split())
    if len(one_line) > TITLE_LENGTH:
        one_line = one_line[: TITLE_LENGTH - 3].rstrip() + "..."
    return one_line or "New chat"


def blocked_intent(reason: str | None) -> str:
    return f"{INTENT_BLOCKED}:{reason}"[:40] if reason else INTENT_BLOCKED


def owns(conversation_id: int, user_id: int) -> bool:
    """True if this conversation exists and belongs to this user. Raises if the database fails."""
    with engine.connect() as conn:
        found = conn.execute(
            text("select 1 from conversations where id = :c and user_id = :u"),
            {"c": conversation_id, "u": user_id},
        ).scalar()
    return found is not None


def save_exchange(
    user_id: int,
    conversation_id: int | None,
    user_text: str,
    answer: str,
    *,
    intent: str,
    outcome: str | None = None,
    citations: list[str] | None = None,
    confidence: float | None = None,
    latency_ms: int | None = None,
    escalated: bool = False,
    blocked: bool = False,
) -> dict:
    """Saves the user message and the assistant message in one transaction.

    Creates the conversation when conversation_id is None. Returns the conversation id and the
    id of the assistant message (the one that can be rated).
    """
    shown_text = BLOCKED_TEXT if blocked else user_text
    with engine.begin() as conn:
        if conversation_id is None:
            title = "Blocked message" if blocked else title_from(user_text)
            conversation_id = conn.execute(
                text(
                    "insert into conversations (user_id, status, title) "
                    "values (:u, 'open', :t) returning id"
                ),
                {"u": user_id, "t": title},
            ).scalar()

        insert = text(
            "insert into messages (conversation_id, role, content, intent, outcome, citations, "
            "confidence, latency_ms, escalated, blocked) "
            "values (:c, :r, :t, :i, :o, cast(:ci as json), :cf, :l, :e, :b) returning id"
        )
        conn.execute(
            insert,
            {"c": conversation_id, "r": "user", "t": shown_text, "i": None, "o": None, "ci": None,
             "cf": None, "l": None, "e": False, "b": blocked},
        )
        message_id = conn.execute(
            insert,
            {"c": conversation_id, "r": "assistant", "t": answer, "i": intent, "o": outcome,
             "ci": json.dumps(citations or []), "cf": confidence, "l": latency_ms,
             "e": escalated, "b": blocked},
        ).scalar()

        if escalated:
            conn.execute(
                text("update conversations set status = 'escalated' where id = :c"), {"c": conversation_id}
            )
    return {"conversation_id": conversation_id, "message_id": message_id}


def last_outcome(conversation_id: int | None) -> str | None:
    """What happened to the last assistant message of a conversation, or None. Never raises."""
    if not conversation_id:
        return None
    try:
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "select coalesce(outcome, intent) from messages "
                    "where conversation_id = :c and role = 'assistant' order by id desc limit 1"
                ),
                {"c": conversation_id},
            ).scalar()
    except Exception:
        logger.exception("Could not read the last outcome of conversation %s", conversation_id)
        return None


def mark_escalated(conversation_id: int) -> None:
    """A ticket was made from this conversation. Best effort: a failure is logged, not raised."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("update conversations set status = 'escalated' where id = :c"), {"c": conversation_id}
            )
    except Exception:
        logger.exception("Could not mark conversation %s as escalated", conversation_id)


# ---------- Reading, rating and deleting ----------
def set_feedback(message_id: int, user_id: int, value: int) -> bool:
    """Rates an assistant message in one of this user's own conversations. False if there is none."""
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "update messages set feedback = :v where id = :m and role = 'assistant' "
                "and conversation_id in (select id from conversations where user_id = :u)"
            ),
            {"v": value, "m": message_id, "u": user_id},
        )
    return result.rowcount == 1


def list_conversations(user_id: int, limit: int = 50) -> list[dict]:
    """This user's conversations, the most recently active first."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "select c.id, c.title, c.status, c.created_at, "
                "(select count(*) from messages m where m.conversation_id = c.id and m.role = 'user') as questions, "
                "(select max(m.created_at) from messages m where m.conversation_id = c.id) as last_at "
                "from conversations c where c.user_id = :u "
                "order by last_at desc nulls last, c.id desc limit :n"
            ),
            {"u": user_id, "n": limit},
        ).mappings().all()
    return [
        {
            "id": r["id"],
            "title": r["title"] or "Chat",
            "status": r["status"],
            "created_at": as_utc(r["created_at"]),
            "last_at": as_utc(r["last_at"] or r["created_at"]),
            "questions": r["questions"],
        }
        for r in rows
    ]


def get_conversation(conversation_id: int) -> dict | None:
    """The conversation and its messages, or None. The caller decides who may see it."""
    with engine.connect() as conn:
        head = conn.execute(
            text("select id, user_id, title, status, created_at from conversations where id = :c"),
            {"c": conversation_id},
        ).mappings().first()
        if head is None:
            return None
        rows = conn.execute(
            text(
                "select id, role, content, intent, outcome, citations, confidence, latency_ms, feedback, "
                "escalated, blocked, created_at from messages where conversation_id = :c order by id"
            ),
            {"c": conversation_id},
        ).mappings().all()
    return {
        "id": head["id"],
        "user_id": head["user_id"],
        "title": head["title"] or "Chat",
        "status": head["status"],
        "created_at": as_utc(head["created_at"]),
        "messages": [{**r, "created_at": as_utc(r["created_at"])} for r in rows],
    }


def delete_conversation(conversation_id: int, user_id: int, actor: str) -> bool:
    """Deletes one of the user's own conversations and its messages. Tickets stay, unlinked.

    The deletion is audited in the same transaction, with the conversation id only.
    """
    with engine.begin() as conn:
        owned = conn.execute(
            text("select 1 from conversations where id = :c and user_id = :u"),
            {"c": conversation_id, "u": user_id},
        ).scalar()
        if owned is None:
            return False
        conn.execute(text("update tickets set conversation_id = null where conversation_id = :c"), {"c": conversation_id})
        conn.execute(text("delete from messages where conversation_id = :c"), {"c": conversation_id})
        conn.execute(text("delete from conversations where id = :c"), {"c": conversation_id})
        audit.record(actor, "conversation_deleted", {"conversation_id": conversation_id}, conn=conn)
    return True
