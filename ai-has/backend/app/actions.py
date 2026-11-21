"""Actions the assistant can take through n8n webhooks.

Nothing here runs by itself. proposals.py calls these functions only after the user
has confirmed the action.
"""
import logging
import re

import httpx

from .config import settings
from .guardrails import check_input, redact_pii
from .tickets import PRIORITIES, create_ticket, set_external_ref

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Webhook-Secret"
# Matches the external_ref column (60 characters). Rejects anything odd from the webhook.
JIRA_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,59}$")
RESET_STATUSES = {"reset_link_sent"}


class ActionError(Exception):
    """An action could not be done. The message is safe to show to the user."""


def _post(path: str, payload: dict) -> dict:
    """POST JSON to one n8n webhook. Raises ActionError with a clear message on any failure."""
    url = f"{settings.n8n_webhook_url.rstrip('/')}/{path}"
    headers = {}
    if settings.n8n_webhook_secret:
        headers[SECRET_HEADER] = settings.n8n_webhook_secret
    try:
        res = httpx.post(url, json=payload, headers=headers, timeout=settings.n8n_timeout_seconds)
    except httpx.TimeoutException:
        raise ActionError("n8n did not answer in time. Is the n8n service running and the workflow active?")
    except httpx.HTTPError:
        raise ActionError("n8n is not reachable. Start it with: docker compose --profile n8n up -d n8n")
    if res.status_code in (401, 403):
        raise ActionError("n8n rejected the request. Check that N8N_WEBHOOK_SECRET matches on both sides.")
    if res.status_code == 404:
        raise ActionError("n8n has no active workflow at this address. Import and activate it first.")
    if res.status_code >= 400:
        raise ActionError(f"n8n returned an error ({res.status_code}).")
    try:
        data = res.json()
    except ValueError:
        raise ActionError("n8n returned an unreadable answer.")
    if not isinstance(data, dict):
        raise ActionError("n8n returned an unexpected answer.")
    return data


def create_ticket_action(
    summary: str,
    priority: str = "medium",
    user_email: str = "",
    conversation_id: int | None = None,
) -> dict:
    """Creates the ticket in our database, then asks n8n for the Jira key.

    The local ticket always exists. If n8n is down, jira_sync is "pending".
    """
    # Free text is checked and redacted before it is saved or sent to n8n.
    # The email is an identifier the workflow needs, so it is not redacted.
    clean, _found = redact_pii(summary)
    if check_input(clean)["blocked"]:
        raise ActionError("That ticket text was not accepted. Please describe the IT problem again.")
    if priority not in PRIORITIES:
        priority = "medium"

    kwargs = {"user_email": user_email} if user_email else {}
    ticket = create_ticket(clean, priority, source="ai", conversation_id=conversation_id, **kwargs)

    try:
        data = _post("create-ticket", {"summary": clean, "priority": priority, "user_email": user_email})
        key = data.get("key")
        if not isinstance(key, str) or not JIRA_KEY_RE.match(key):
            raise ActionError("n8n returned no valid Jira key.")
        set_external_ref(ticket["id"], key)
    except ActionError as e:
        logger.warning("Jira sync pending for ticket %s: %s", ticket["id"], e)
        return {**ticket, "external_ref": None, "jira_sync": "pending", "sync_note": str(e)}
    return {**ticket, "external_ref": key, "jira_sync": "synced"}


def request_password_reset(user_email: str) -> dict:
    """Asks n8n (mock Active Directory) to send a reset link. No email is sent by the mock."""
    data = _post("password-reset", {"user_email": user_email})
    status = data.get("status")
    if status not in RESET_STATUSES:
        raise ActionError("The password reset was not accepted by the directory service.")
    return {"status": status}
