import logging
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from . import analytics, audit, chatlog, kb, llm, memory, proposals
from .auth import (
    STAFF,
    authenticate,
    current_user,
    require_admin,
    require_staff,
    safe_next,
    user_from_request,
)
from .config import settings
from .db import engine
from .guardrails import check_input, redact_pii
from .intent import ACTION_INTENTS, OUT_OF_SCOPE, UNKNOWN, classify
from .passwords import MAX_PASSWORD_LENGTH
from .rag import answer, propose_action
from .ratelimit import limit_actions, limit_chat, limit_kb, limit_tickets
from .sessions import COOKIE_NAME, create_token, lifetime_seconds, secret_ok
from .tickets import create_ticket, get_stats, list_tickets, update_status
from .timeutil import check_timezone

app = FastAPI(title="AI-HAS", version="0.2.0")

logger = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
GENERIC_ERROR = "Something went wrong. Please try again."


STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def json_only_and_same_origin(request: Request, call_next):
    """Blocks simple cross-site form posts.

    Every state-changing request must be JSON, which a plain HTML form cannot send, and if
    the browser sends an Origin header it must be this site. Defined before the middleware
    below so that one stays outermost and its security headers also cover these answers.
    """
    if request.method in STATE_CHANGING:
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            return JSONResponse(
                status_code=415,
                content={"detail": "Requests that change data must be JSON (Content-Type: application/json)."},
            )
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse(status_code=403, content={"detail": "Cross-site requests are not allowed."})
    return await call_next(request)


@app.middleware("http")
async def security_headers_and_safe_errors(request: Request, call_next):
    """Adds security headers to every response and hides unexpected errors.

    Normal HTTPException responses (400, 404, 429) and the 422 validation format are
    built by FastAPI inside call_next and pass through unchanged. Only an exception
    nobody handled becomes a generic 500. Its details go to the server log, never to
    the client, so a database URL or key in an error message cannot leak.
    """
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        response = JSONResponse(status_code=500, content={"detail": GENERIC_ERROR})
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


@app.exception_handler(kb.KbError)
async def kb_error(request: Request, exc: kb.KbError):
    """A knowledge base request that cannot be done, with a message that is safe to show."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(llm.ProviderUnavailable)
async def ai_provider_unavailable(request: Request, exc: llm.ProviderUnavailable):
    """The AI provider failed after its retries. A fixed message and a Retry-After, nothing from the error."""
    return JSONResponse(
        status_code=503,
        content={"detail": llm.BUSY_MESSAGE},
        headers={"Retry-After": str(llm.RETRY_AFTER_SECONDS)},
    )


# Browsers must revalidate pages and scripts, so a rebuilt frontend is never hidden by an old cached copy
NO_CACHE = {"Cache-Control": "no-cache"}


class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response

FRONTEND_DIR = Path("/frontend")
CHAT_DIR = FRONTEND_DIR / "chat"
ADMIN_DIR = FRONTEND_DIR / "admin"
LOGIN_DIR = FRONTEND_DIR / "login"

# Chat page files: /static/styles.css, /static/app.js, /static/field.js
app.mount("/static", NoCacheStaticFiles(directory=CHAT_DIR, check_dir=False), name="static")

# Admin page files: /admin-assets/admin.css, /admin-assets/admin.js
app.mount("/admin-assets", NoCacheStaticFiles(directory=ADMIN_DIR, check_dir=False), name="admin-assets")

# Login page files: /login-assets/login.css, /login-assets/login.js
app.mount("/login-assets", NoCacheStaticFiles(directory=LOGIN_DIR, check_dir=False), name="login-assets")


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = Field(default=None, ge=1)  # continue this conversation, or start one


class TicketRequest(BaseModel):
    summary: str = Field(min_length=3, max_length=500)
    priority: str = "medium"
    conversation_id: int | None = Field(default=None, ge=1)  # the chat this ticket came from


class StatusUpdate(BaseModel):
    status: str


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class FeedbackRequest(BaseModel):
    value: Literal[-1, 0, 1]  # -1 thumbs down, 1 thumbs up, 0 clears the rating


class ConfirmRequest(BaseModel):
    action_id: str = Field(min_length=10, max_length=100)
    confirm: bool


FORBIDDEN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>No access</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 15vh auto; padding: 0 1rem">
<h1>No access</h1>
<p>The dashboard is for agents and admins. Your account does not have that role.</p>
<p><a href="/">Back to the chat</a></p>
</body></html>"""


def _to_login(path: str) -> RedirectResponse:
    target = "/login" if path == "/" else f"/login?next={path}"
    return RedirectResponse(target, status_code=302, headers=NO_CACHE)


@app.get("/")
def home(request: Request):
    if user_from_request(request) is None:
        return _to_login("/")
    return FileResponse(CHAT_DIR / "index.html", headers=NO_CACHE)


@app.get("/admin")
def admin(request: Request):
    user = user_from_request(request)
    if user is None:
        return _to_login("/admin")
    if user["role"] not in STAFF:
        return HTMLResponse(FORBIDDEN_PAGE, status_code=403, headers=NO_CACHE)
    return FileResponse(ADMIN_DIR / "admin.html", headers=NO_CACHE)


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    if user_from_request(request) is not None:
        return RedirectResponse(safe_next(next), status_code=302, headers=NO_CACHE)
    return FileResponse(LOGIN_DIR / "login.html", headers=NO_CACHE)


def _cookie_flags() -> dict:
    return {"path": "/", "httponly": True, "samesite": "lax", "secure": settings.cookie_secure}


@app.post("/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    if not secret_ok():
        raise HTTPException(
            status_code=503, detail="Login is not configured. Set SESSION_SECRET (at least 32 characters)."
        )
    ip = request.client.host if request.client else "unknown"
    user = authenticate(req.email, req.password, ip)
    response.set_cookie(COOKIE_NAME, create_token(user["id"]), max_age=lifetime_seconds(), **_cookie_flags())
    response.headers["Cache-Control"] = "no-store"
    return user


@app.post("/auth/logout")
def logout(request: Request, response: Response):
    """Clears the cookie. The signed token itself stays valid until it expires."""
    user = user_from_request(request)
    if user:
        audit.record(audit.actor_for(user["id"]), "logout", {})
    response.delete_cookie(COOKIE_NAME, **_cookie_flags())
    response.headers["Cache-Control"] = "no-store"
    return {"status": "logged_out"}


@app.get("/auth/me")
def me(response: Response, user: dict = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    return user


def time_zone(tz: str | None = Query(None, max_length=64)) -> str:
    """The ?tz= name (for example Asia/Singapore), checked against the database. UTC if absent."""
    try:
        return check_timezone(tz)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown time zone.")


@app.get("/stats", dependencies=[Depends(require_staff)])
def stats(tz: str = Depends(time_zone)):
    return get_stats(tz)


@app.get("/stats/analytics", dependencies=[Depends(require_staff)])
def stats_analytics(days: int = Query(30, ge=1, le=365), tz: str = Depends(time_zone)):
    """Questions per day, deflection, latency, thumbs, top articles and top escalated questions."""
    return analytics.compute(days, tz)


@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        db = "connected"
    except Exception as e:
        db = f"error: {type(e).__name__}"
    return {"status": "ok", "database": db}


ACTION_MESSAGE = "I can do that for you. Please review it and confirm below. Nothing happens until you do."


REFUSAL_MESSAGE = (
    "I can only help with IT questions, like passwords, VPN, software access, Wi-Fi, printers or email. "
    "For anything else, please contact the right team."
)
CLARIFY_MESSAGE = (
    "I want to be sure I help with the right thing. Could you tell me a bit more: what is not working, "
    "on which device or app, and any error message you see?"
)
STILL_UNCLEAR_MESSAGE = (
    "I still couldn't work out what you need. Would you like me to escalate this to a human agent?"
)


def _store_proposal(user_email: str, clean_message: str, intent_name: str) -> dict | None:
    """A stored proposal for this message, or None. Any failure falls back to a normal answer."""
    try:
        raw = propose_action(clean_message, intent_name)
        if raw is None:
            return None
        return proposals.propose(user_email, raw["name"], raw["arguments"])
    except Exception:
        logger.exception("Could not prepare a proposed action")
        return None


def _check_conversation(conversation_id: int | None, user_id: int) -> bool:
    """True if saving is on for this request. 404 if the conversation is not the caller's.

    A database failure does not stop the chat: the request is answered without saving.
    """
    if not settings.persist_chats:
        return False
    if conversation_id is None:
        return True
    try:
        owned = chatlog.owns(conversation_id, user_id)
    except Exception:
        logger.exception("Could not check the conversation, so this exchange is not saved")
        return False
    if not owned:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return True


def _save_exchange(response: dict, user: dict, conversation_id: int | None, user_text: str, started: float, **fields) -> dict:
    """Saves the exchange and adds conversation_id and message_id to the response (null if not saved)."""
    response["conversation_id"] = None
    response["message_id"] = None
    if conversation_id == 0:  # saving is off for this request
        return response
    try:
        saved = chatlog.save_exchange(
            user["id"],
            conversation_id,
            user_text,
            response["answer"],
            citations=response["citations"],
            confidence=chatlog.confidence_from_distances(response["distances"]),
            latency_ms=int((time.perf_counter() - started) * 1000),
            **fields,
        )
    except Exception:
        logger.exception("Could not save the chat exchange")
        return response
    response.update(saved)
    return response


@app.post("/chat", dependencies=[Depends(limit_chat)])
def chat(req: ChatRequest, user: dict = Depends(current_user)):
    started = time.perf_counter()
    user_email = user["email"]
    saving = _check_conversation(req.conversation_id, user["id"])
    # 0 means "do not save". Otherwise the conversation to continue, or None to start one.
    conversation_id = (req.conversation_id if saving else 0)

    # 1) Block injection attempts and oversized messages
    verdict = check_input(req.message)
    if verdict["blocked"]:
        print(f"[guardrail] blocked: {verdict['reason']}", flush=True)
        response = {
            "answer": verdict["message"],
            "citations": [],
            "escalate": False,
            "distances": [],
            "proposed_action": None,
            "intent": None,
            "guardrails": {"blocked": True, "reason": verdict["reason"], "redacted": [], "output": None},
        }
        # The raw text is never saved, only a placeholder with the blocked flag
        return _save_exchange(
            response, user, conversation_id, "", started,
            intent=chatlog.blocked_intent(verdict["reason"]), outcome=chatlog.OUTCOME_BLOCKED, blocked=True,
        )

    # 2) Hide personal data before it reaches the AI or the database
    clean, found = redact_pii(req.message)
    if found:
        print(f"[guardrail] redacted: {found}", flush=True)

    # 3) What does the user want? Rules decide when they are sure. Only when they are not, one
    #    small model call decides (see intent.py).
    guardrails = {"blocked": False, "reason": None, "redacted": found, "output": None}
    #    A follow-up like "it still doesn't work" is classified, and searched, as a standalone question.
    turns = chatlog.recent_turns(req.conversation_id if saving else None, memory.max_turns())
    standalone = memory.standalone_question(clean, turns)
    found_intent = classify(standalone)
    name = found_intent.name
    shown_intent = found_intent.as_dict()

    def plain(answer_text: str, escalate: bool = False) -> dict:
        return {
            "answer": answer_text,
            "citations": [],
            "escalate": escalate,
            "distances": [],
            "proposed_action": None,
            "intent": shown_intent,
            "guardrails": guardrails,
        }

    # 3a) An action intent: offer the action. The model may only propose it. It is stored for this
    #     user and runs only after POST /actions/confirm.
    if name in ACTION_INTENTS:
        proposal = _store_proposal(user_email, clean, name)
        if proposal:
            response = plain(ACTION_MESSAGE)
            response["proposed_action"] = proposal
            _save_exchange(
                response, user, conversation_id, clean, started,
                intent=name, outcome=chatlog.OUTCOME_ACTION,
            )
            if response["conversation_id"] is not None:
                try:
                    proposals.attach_conversation(proposal["id"], response["conversation_id"])
                except Exception:
                    logger.exception("Could not link the proposed action to its conversation")
            return response
        # No proposal (the model chose not to propose one, or it was not valid): the knowledge base answers

    # 3b) Not about IT: a polite refusal, with no model call and no search
    elif name == OUT_OF_SCOPE:
        return _save_exchange(
            plain(REFUSAL_MESSAGE), user, conversation_id, clean, started,
            intent=name, outcome=chatlog.OUTCOME_REFUSED,
        )

    # 3c) Unclear: ask one clarifying question, and if the next answer is still unclear, escalate
    elif name == UNKNOWN:
        if turns and turns[-1]["role"] == "assistant" and turns[-1]["outcome"] == chatlog.OUTCOME_CLARIFY:
            return _save_exchange(
                plain(STILL_UNCLEAR_MESSAGE, escalate=True), user, conversation_id, clean, started,
                intent=name, outcome=chatlog.OUTCOME_ESCALATED, escalated=True,
            )
        return _save_exchange(
            plain(CLARIFY_MESSAGE), user, conversation_id, clean, started,
            intent=name, outcome=chatlog.OUTCOME_CLARIFY,
        )

    # 4) how_to and vpn_issue (and an action that was not offered): the knowledge base answers,
    #    using only the cleaned text
    result = answer(clean, history=memory.history_messages(turns), retrieval_query=standalone)
    result["proposed_action"] = None
    result["intent"] = shown_intent
    guardrails["output"] = result.pop("output_check", None)  # what the output check did, if the model answered
    result["guardrails"] = guardrails
    escalated = bool(result["escalate"])
    return _save_exchange(
        result, user, conversation_id, clean, started,
        intent=name,
        outcome=chatlog.OUTCOME_ESCALATED if escalated else chatlog.OUTCOME_ANSWERED,
        escalated=escalated,
    )


@app.post("/tickets", dependencies=[Depends(limit_tickets)])
def new_ticket(req: TicketRequest, user: dict = Depends(current_user)):
    # Personal data never gets saved into a ticket
    clean, found = redact_pii(req.summary)
    extra = {}
    if req.conversation_id is not None and settings.persist_chats:
        try:
            owned = chatlog.owns(req.conversation_id, user["id"])
        except Exception:
            logger.exception("Could not check the conversation, so the ticket is not linked to it")
            owned = None
        if owned is False:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if owned:
            extra["conversation_id"] = req.conversation_id
    try:
        ticket = create_ticket(clean, req.priority, source="escalation", user_email=user["email"], **extra)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if extra:
        chatlog.mark_escalated(req.conversation_id)
    ticket["redacted"] = found
    return ticket


@app.get("/tickets", dependencies=[Depends(require_staff)])
def get_tickets(status: str | None = None):
    return list_tickets(status)


@app.patch("/tickets/{ticket_id}")
def change_ticket(ticket_id: int, req: StatusUpdate, user: dict = Depends(require_staff)):
    try:
        result = update_status(ticket_id, req.status, actor=audit.actor_for(user["id"]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return result


@app.post("/actions/confirm", dependencies=[Depends(limit_actions)])
def confirm_action(req: ConfirmRequest, user: dict = Depends(current_user)):
    """Runs (confirm=true) or drops (confirm=false) an action the assistant proposed.

    Only the id comes from the client. What runs was stored when the action was proposed,
    and only its owner can decide it, once.
    """
    try:
        return proposals.decide(req.action_id, user["email"], req.confirm)
    except proposals.ProposalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


# ---------- Conversations and feedback ----------
CONVERSATION_NOT_FOUND = "Conversation not found."


@app.get("/conversations")
def my_conversations(limit: int = Query(50, ge=1, le=100), user: dict = Depends(current_user)):
    """The logged-in user's own conversations, the most recently active first."""
    return chatlog.list_conversations(user["id"], limit)


@app.get("/conversations/{conversation_id}")
def read_conversation(conversation_id: int, user: dict = Depends(current_user)):
    """The owner can read it, and so can an agent or admin. Anyone else gets 404."""
    conversation = chatlog.get_conversation(conversation_id)
    own = conversation is not None and conversation["user_id"] == user["id"]
    if conversation is None or not (own or user["role"] in STAFF):
        raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND)
    if not own:
        # Staff reading someone else's chat leaves a trace: who, and which conversation (no text)
        audit.record(audit.actor_for(user["id"]), "conversation_read", {"conversation_id": conversation_id})
    return conversation


@app.delete("/conversations/{conversation_id}", dependencies=[Depends(limit_actions)])
def delete_conversation(conversation_id: int, user: dict = Depends(current_user)):
    """Deletes one of your own conversations. Its tickets stay, no longer linked to it."""
    if not chatlog.delete_conversation(conversation_id, user["id"], audit.actor_for(user["id"])):
        raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND)
    return {"status": "deleted"}


@app.patch("/messages/{message_id}/feedback", dependencies=[Depends(limit_actions)])
def rate_message(message_id: int, req: FeedbackRequest, user: dict = Depends(current_user)):
    """Thumbs up or down on an assistant message. Only in your own conversations."""
    if not chatlog.set_feedback(message_id, user["id"], req.value):
        raise HTTPException(status_code=404, detail="Message not found.")
    return {"message_id": message_id, "feedback": req.value}


# ---------- Knowledge base management (admin only) ----------
class KbSave(BaseModel):
    content: str


class KbTest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    source: str | None = Field(default=None, max_length=100)  # the article being edited, to highlight its chunks


def kb_body_size(request: Request) -> None:
    """Refuses an obviously too large body before it is read. The exact byte limit is checked in kb.py."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.kb_max_bytes * 6 + 4096:
        raise HTTPException(status_code=413, detail=f"The article is too long. The limit is {settings.kb_max_bytes} bytes.")


@app.get("/kb")
def kb_list(admin: dict = Depends(require_admin)):
    return kb.list_articles()


@app.post("/kb/test", dependencies=[Depends(limit_kb)])
def kb_test(req: KbTest, admin: dict = Depends(require_admin)):
    """One question through the chat's search, to see which chunks and distances come back. No answer call."""
    return kb.try_retrieval(req.question.strip(), req.source)


@app.get("/kb/{source}")
def kb_read(source: str, admin: dict = Depends(require_admin)):
    return kb.read_article(source)


@app.put("/kb/{source}", dependencies=[Depends(limit_kb), Depends(kb_body_size)])
def kb_save(source: str, req: KbSave, response: Response, admin: dict = Depends(require_admin)):
    """Creates or updates an article from markdown and re-indexes only that article."""
    result = kb.save_article(source, req.content, audit.actor_for(admin["id"]))
    if result["created"]:
        response.status_code = 201
    return result


@app.delete("/kb/{source}", dependencies=[Depends(limit_kb)])
def kb_delete(source: str, admin: dict = Depends(require_admin)):
    return kb.delete_article(source, audit.actor_for(admin["id"]))
