import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from . import proposals
from .db import engine
from .guardrails import check_input, redact_pii
from .rag import answer
from .ratelimit import limit_actions, limit_chat, limit_tickets
from .tickets import DEFAULT_USER, create_ticket, get_stats, list_tickets, update_status

app = FastAPI(title="AI-HAS", version="0.2.0")

logger = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
GENERIC_ERROR = "Something went wrong. Please try again."


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

# Chat page files: /static/styles.css, /static/app.js, /static/field.js
app.mount("/static", NoCacheStaticFiles(directory=CHAT_DIR, check_dir=False), name="static")

# Admin page files: /admin-assets/admin.css, /admin-assets/admin.js
app.mount("/admin-assets", NoCacheStaticFiles(directory=ADMIN_DIR, check_dir=False), name="admin-assets")


class ChatRequest(BaseModel):
    message: str


class TicketRequest(BaseModel):
    summary: str = Field(min_length=3, max_length=500)
    priority: str = "medium"


class StatusUpdate(BaseModel):
    status: str


class ConfirmRequest(BaseModel):
    action_id: str = Field(min_length=10, max_length=100)
    confirm: bool


def current_user_email() -> str:
    """Who is asking. There is no login yet, so this is the default employee.

    This is the one place to change when real authentication is added.
    """
    return DEFAULT_USER


@app.get("/")
def home():
    return FileResponse(CHAT_DIR / "index.html", headers=NO_CACHE)


@app.get("/admin")
def admin():
    return FileResponse(ADMIN_DIR / "admin.html", headers=NO_CACHE)


@app.get("/stats")
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


@app.post("/chat", dependencies=[Depends(limit_chat)])
def chat(req: ChatRequest):
    # 1) Block injection attempts and oversized messages
    verdict = check_input(req.message)
    if verdict["blocked"]:
        print(f"[guardrail] blocked: {verdict['reason']}", flush=True)
        return {
            "answer": verdict["message"],
            "citations": [],
            "escalate": False,
            "distances": [],
            "guardrails": {"blocked": True, "reason": verdict["reason"], "redacted": []},
        }

    # 2) Hide personal data before it reaches the AI or the database
    clean, found = redact_pii(req.message)
    if found:
        print(f"[guardrail] redacted: {found}", flush=True)

    # 3) Normal RAG flow, using only the cleaned text
    result = answer(clean)
    result["guardrails"] = {"blocked": False, "reason": None, "redacted": found}
    return result


@app.post("/tickets", dependencies=[Depends(limit_tickets)])
def new_ticket(req: TicketRequest):
    # Personal data never gets saved into a ticket
    clean, found = redact_pii(req.summary)
    try:
        ticket = create_ticket(clean, req.priority, source="escalation")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ticket["redacted"] = found
    return ticket


@app.get("/tickets")
def get_tickets(status: str | None = None):
    return list_tickets(status)


@app.patch("/tickets/{ticket_id}")
def change_ticket(ticket_id: int, req: StatusUpdate):
    try:
        result = update_status(ticket_id, req.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return result


@app.post("/actions/confirm", dependencies=[Depends(limit_actions)])
def confirm_action(req: ConfirmRequest, user_email: str = Depends(current_user_email)):
    """Runs (confirm=true) or drops (confirm=false) an action the assistant proposed.

    Only the id comes from the client. What runs was stored when the action was proposed,
    and only its owner can decide it, once.
    """
    try:
        return proposals.decide(req.action_id, user_email, req.confirm)
    except proposals.ProposalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
