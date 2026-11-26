"""Proposed actions: the model proposes, the user confirms, then (and only then) it runs.

A proposal is stored in pending_actions with an unguessable id and its owner. The client
sends back only the id, never the arguments, so a request cannot change what runs.
Confirming is one atomic UPDATE ... WHERE status = 'pending', so a double click or a
replay cannot run an action twice. Every decision writes an audit_logs row.
"""
import json
import logging
import secrets

from sqlalchemy import text

from . import actions, chatlog
from .db import engine
from .guardrails import check_input, redact_pii
from .tickets import PRIORITIES, resolve_user

logger = logging.getLogger(__name__)

PROPOSAL_MINUTES = 10
MAX_SUMMARY = 500
MIN_SUMMARY = 3

CREATE_TICKET = "create_ticket"
PASSWORD_RESET = "request_password_reset"
# audit_logs.action names
AUDIT_ACTION = {CREATE_TICKET: "create_ticket", PASSWORD_RESET: "reset_password"}


class ProposalError(Exception):
    """A confirm request that cannot be accepted. status_code maps to the HTTP response."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def clean_arguments(name: str, arguments: dict | None) -> dict | None:
    """Validates what the model proposed. Returns the safe arguments, or None to drop it."""
    if name == PASSWORD_RESET:
        # The account is always the signed-in user. The model cannot choose it.
        return {}
    if name != CREATE_TICKET or not isinstance(arguments, dict):
        return None
    summary = arguments.get("summary")
    if not isinstance(summary, str):
        return None
    summary, _found = redact_pii(summary.strip())
    summary = summary[:MAX_SUMMARY].strip()
    if len(summary) < MIN_SUMMARY or check_input(summary)["blocked"]:
        return None
    priority = arguments.get("priority")
    return {"summary": summary, "priority": priority if priority in PRIORITIES else "medium"}


def _label(name: str, arguments: dict) -> str:
    if name == CREATE_TICKET:
        return f"Create a {arguments['priority']} priority ticket: {arguments['summary']}"
    return "Change your password on this website"


def attach_conversation(action_id: str, conversation_id: int) -> None:
    """Links a stored proposal to the conversation it came from, once that conversation exists."""
    with engine.begin() as conn:
        conn.execute(
            text("update pending_actions set conversation_id = :c where id = :id"),
            {"c": conversation_id, "id": action_id},
        )


def propose(user_email: str, name: str, arguments: dict | None) -> dict | None:
    """Stores a proposal for this user. Returns what the UI shows, or None if it is not valid."""
    clean = clean_arguments(name, arguments)
    if clean is None:
        return None
    user = resolve_user(user_email)
    action_id = secrets.token_urlsafe(32)
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into pending_actions (id, owner_user_id, name, arguments, expires_at) "
                "values (:id, :u, :n, cast(:a as json), now() + make_interval(mins => :m))"
            ),
            {"id": action_id, "u": user["id"], "n": name, "a": json.dumps(clean), "m": PROPOSAL_MINUTES},
        )
    return {"id": action_id, "name": name, "arguments": clean, "label": _label(name, clean)}


def _claim(conn, action_id: str, user_id: int, new_status: str) -> dict:
    """Takes the proposal for this owner, once. Raises ProposalError if it cannot be taken."""
    res = conn.execute(
        text(
            "update pending_actions set status = :s, decided_at = now() "
            "where id = :id and owner_user_id = :u and status = 'pending' and expires_at > now() "
            "returning name, arguments, conversation_id"
        ),
        {"s": new_status, "id": action_id, "u": user_id},
    )
    if res.rowcount == 1:
        row = res.mappings().first()
        args = row["arguments"]
        return {
            "name": row["name"],
            "arguments": args if isinstance(args, dict) else json.loads(args),
            "conversation_id": row.get("conversation_id"),
        }

    # Not taken: find out why, so the answer is clear.
    why = conn.execute(
        text(
            "select owner_user_id, status, expires_at <= now() as expired "
            "from pending_actions where id = :id"
        ),
        {"id": action_id},
    ).mappings().first()
    if why is None:
        raise ProposalError(404, "This action was not found.")
    if why["owner_user_id"] != user_id:
        raise ProposalError(403, "This action belongs to another user.")
    if why["status"] != "pending":
        raise ProposalError(409, "This action was already handled.")
    raise ProposalError(410, "This action expired. Please ask again.")


def _audit(conn, user_id: int, name: str, payload: dict) -> None:
    """actor is the user id and the payload holds no email, name or ticket text."""
    conn.execute(
        text("insert into audit_logs (actor, action, payload) values (:a, :n, cast(:p as json))"),
        {"a": f"user:{user_id}", "n": AUDIT_ACTION.get(name, name), "p": json.dumps(payload)},
    )


def _run(name: str, arguments: dict, user: dict, conversation_id: int | None = None) -> dict:
    """Executes a confirmed action. Returns {outcome, message, result, audit}."""
    if name == CREATE_TICKET:
        extra = {"conversation_id": conversation_id} if conversation_id is not None else {}
        result = actions.create_ticket_action(arguments["summary"], arguments["priority"], user["email"], **extra)
        if conversation_id is not None:
            chatlog.mark_escalated(conversation_id)
        if result["jira_sync"] == "synced":
            message = f"Ticket {result['ref']} created and synced to Jira as {result['external_ref']}."
        else:
            message = f"Ticket {result['ref']} created. The Jira sync is pending, so the key will follow."
        shown = {
            "ref": result["ref"],
            "priority": result["priority"],
            "external_ref": result["external_ref"],
            "jira_sync": result["jira_sync"],
        }
        audit = {"ticket_id": result["id"], "priority": result["priority"], "jira_sync": result["jira_sync"]}
        return {"message": message, "result": shown, "audit": audit}
    if name == PASSWORD_RESET:
        # The password is changed on the website itself (POST /auth/change-password). It never goes
        # through the chat text or the model. Confirming only opens the form.
        return {
            "message": "Confirmed. Enter your current password and a new one below.",
            "result": {"next": "password_form"},
            "audit": {"status": "form_opened"},
        }
    raise ProposalError(400, "Unknown action.")


def decide(action_id: str, user_email: str, confirm: bool) -> dict:
    """Confirms or cancels a proposal for this user."""
    user = resolve_user(user_email)

    if not confirm:
        with engine.begin() as conn:
            proposal = _claim(conn, action_id, user["id"], "cancelled")
            _audit(conn, user["id"], proposal["name"], {"decision": "cancelled"})
        return {"status": "cancelled", "name": proposal["name"], "message": "Cancelled. Nothing was done."}

    with engine.begin() as conn:
        proposal = _claim(conn, action_id, user["id"], "confirmed")

    try:
        done = _run(proposal["name"], proposal["arguments"], user, proposal.get("conversation_id"))
        payload = {"decision": "confirmed", "outcome": "done", **done["audit"]}
        response = {"status": "done", "name": proposal["name"], "message": done["message"], "result": done["result"]}
    except actions.ActionError as e:
        payload = {"decision": "confirmed", "outcome": "failed"}
        response = {"status": "failed", "name": proposal["name"], "message": str(e), "result": None}

    try:
        with engine.begin() as conn:
            conn.execute(
                text("update pending_actions set status = :s where id = :id and status = 'confirmed'"),
                {"s": "done" if response["status"] == "done" else "failed", "id": action_id},
            )
            _audit(conn, user["id"], proposal["name"], payload)
    except Exception:
        # The action already ran, so the user still gets the result. The failure is logged.
        logger.exception("Could not write the audit log for %s", proposal["name"])
    return response