import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from . import audit, proposals
from .auth import (
    STAFF,
    authenticate,
    current_user,
    require_staff,
    safe_next,
    user_from_request,
)
from .config import settings
from .db import engine
from .guardrails import check_input, redact_pii
from .passwords import MAX_PASSWORD_LENGTH
from .rag import answer, propose_action
from .ratelimit import limit_actions, limit_chat, limit_tickets
from .sessions import COOKIE_NAME, create_token, lifetime_seconds, secret_ok
from .tickets import create_ticket, get_stats, list_tickets, update_status

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


class TicketRequest(BaseModel):
    summary: str = Field(min_length=3, max_length=500)
    priority: str = "medium"


class StatusUpdate(BaseModel):
    status: str


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


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


@app.get("/stats", dependencies=[Depends(require_staff)])
def stats():
    return get_stats()


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


def _store_proposal(user_email: str, clean_message: str) -> dict | None:
    """A stored proposal for this message, or None. Any failure falls back to a normal answer."""
    try:
        raw = propose_action(clean_message)
        if raw is None:
            return None
        return proposals.propose(user_email, raw["name"], raw["arguments"])
    except Exception:
        logger.exception("Could not prepare a proposed action")
        return None


@app.post("/chat", dependencies=[Depends(limit_chat)])
def chat(req: ChatRequest, user: dict = Depends(current_user)):
    user_email = user["email"]
    # 1) Block injection attempts and oversized messages
    verdict = check_input(req.message)
    if verdict["blocked"]:
        print(f"[guardrail] blocked: {verdict['reason']}", flush=True)
        return {
            "answer": verdict["message"],
            "citations": [],
            "escalate": False,
            "distances": [],
            "proposed_action": None,
            "guardrails": {"blocked": True, "reason": verdict["reason"], "redacted": []},
        }

    # 2) Hide personal data before it reaches the AI or the database
    clean, found = redact_pii(req.message)
    if found:
        print(f"[guardrail] redacted: {found}", flush=True)

    # 3) Does the user ask for an action? The model may only propose it. It is stored for
    #    this user and runs only after POST /actions/confirm.
    guardrails = {"blocked": False, "reason": None, "redacted": found}
    proposal = _store_proposal(user_email, clean)
    if proposal:
        return {
            "answer": ACTION_MESSAGE,
            "citations": [],
            "escalate": False,
            "distances": [],
            "proposed_action": proposal,
            "guardrails": guardrails,
        }

    # 4) Normal RAG flow, using only the cleaned text
    result = answer(clean)
    result["proposed_action"] = None
    result["guardrails"] = {"blocked": False, "reason": None, "redacted": found}
    return result


@app.post("/tickets", dependencies=[Depends(limit_tickets)])
def new_ticket(req: TicketRequest, user: dict = Depends(current_user)):
    # Personal data never gets saved into a ticket
    clean, found = redact_pii(req.summary)
    try:
        ticket = create_ticket(clean, req.priority, source="escalation", user_email=user["email"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
