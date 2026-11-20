from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from .db import engine
from .guardrails import check_input, redact_pii
from .rag import answer
from .tickets import create_ticket, get_stats, list_tickets, update_status

app = FastAPI(title="AI-HAS", version="0.2.0")

FRONTEND_DIR = Path("/frontend")
CHAT_DIR = FRONTEND_DIR / "chat"
ADMIN_DIR = FRONTEND_DIR / "admin"

# Chat page files: /static/styles.css, /static/app.js, /static/field.js
app.mount("/static", StaticFiles(directory=CHAT_DIR, check_dir=False), name="static")

# Admin page files: /admin-assets/admin.css, /admin-assets/admin.js
app.mount("/admin-assets", StaticFiles(directory=ADMIN_DIR, check_dir=False), name="admin-assets")


class ChatRequest(BaseModel):
    message: str


class TicketRequest(BaseModel):
    summary: str = Field(min_length=3, max_length=500)
    priority: str = "medium"


class StatusUpdate(BaseModel):
    status: str


@app.get("/")
def home():
    return FileResponse(CHAT_DIR / "index.html")


@app.get("/admin")
def admin():
    return FileResponse(ADMIN_DIR / "admin.html")


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


@app.post("/chat")
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


@app.post("/tickets")
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