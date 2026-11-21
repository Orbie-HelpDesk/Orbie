"""Saves what happens in the chat: conversations and messages.

Only PII-redacted text is ever saved. A blocked message is saved as a fixed placeholder, never
as the raw text. The id of a proposed action is not saved, because it works like a one-time key.
"""
import json
import logging

from sqlalchemy import text

from .db import engine

logger = logging.getLogger(__name__)

BLOCKED_TEXT = "[blocked message]"
TITLE_LENGTH = 60
# Same scale as the confidence gauge in the chat: distance 0.5 is 0 percent, 0.25 is 100 percent.
CONFIDENCE_FAR = 0.5
CONFIDENCE_RANGE = 0.25

INTENT_ANSWERED = "answered"
INTENT_ESCALATED = "escalated"
INTENT_ACTION = "action_proposed"
INTENT_BLOCKED = "blocked"


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
            "insert into messages (conversation_id, role, content, intent, citations, "
            "confidence, latency_ms, escalated, blocked) "
            "values (:c, :r, :t, :i, cast(:ci as json), :cf, :l, :e, :b) returning id"
        )
        conn.execute(
            insert,
            {"c": conversation_id, "r": "user", "t": shown_text, "i": None, "ci": None,
             "cf": None, "l": None, "e": False, "b": blocked},
        )
        message_id = conn.execute(
            insert,
            {"c": conversation_id, "r": "assistant", "t": answer, "i": intent,
             "ci": json.dumps(citations or []), "cf": confidence, "l": latency_ms,
             "e": escalated, "b": blocked},
        ).scalar()

        if escalated:
            conn.execute(
                text("update conversations set status = 'escalated' where id = :c"), {"c": conversation_id}
            )
    return {"conversation_id": conversation_id, "message_id": message_id}


def mark_escalated(conversation_id: int) -> None:
    """A ticket was made from this conversation. Best effort: a failure is logged, not raised."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("update conversations set status = 'escalated' where id = :c"), {"c": conversation_id}
            )
    except Exception:
        logger.exception("Could not mark conversation %s as escalated", conversation_id)
