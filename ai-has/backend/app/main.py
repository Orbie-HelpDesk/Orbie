from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text

from .db import engine
from .rag import answer

app = FastAPI(title="AI-HAS", version="0.1.0")

FRONTEND_DIR = Path("/frontend")

# Serves styles.css and app.js at /static/...
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


class ChatRequest(BaseModel):
    message: str


@app.get("/")
def home():
    return FileResponse(FRONTEND_DIR / "index.html")


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
    return answer(req.message)