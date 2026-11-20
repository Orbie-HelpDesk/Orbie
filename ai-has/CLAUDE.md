# AI-HAS project rules

AI-HAS is an IT helpdesk chatbot (capstone, Applied Generative AI for IT Solution Development).

## Stack
- Backend: Python 3.12, FastAPI, SQLAlchemy 2, pydantic-settings, in backend/app/ (main.py, rag.py, ingest.py, db.py, models.py, config.py, tune.py).
- Database: PostgreSQL with pgvector on Supabase, shared by the whole team. Tables in models.py plus kb_chunks (used by ingest.py and rag.py through raw SQL).
- LLM: Google Gemini through its OpenAI-compatible endpoint, using the openai Python SDK. Model names come from .env (CHAT_MODEL, EMBED_MODEL). Never hardcode model names.
- Frontend: plain HTML, CSS and vanilla JavaScript in frontend/, served by FastAPI at /static. No frameworks, no build step.
- Knowledge base: markdown files in kb/. Run: docker compose up --build. App at http://localhost:8000.

## Current behaviour
- POST /chat {message} runs hybrid search (vector plus full-text, merged by rank), returns {answer, citations, escalate, distances}. If the best distance is above 0.5 or the model replies NO_ANSWER, it returns a "not found" message with escalate=true.
- The chat page keeps history in the browser (localStorage). The "Create a ticket" button is a placeholder.

## Hard rules
- Work on a feature branch. Never push to main. Small, focused commits with clear messages.
- Do NOT add Co-Authored-By, Claude-Session or "Generated with Claude Code" lines to commit messages or pull request descriptions.
- Never read, print or commit .env or any key or password. Use .env.example for new variables, with placeholder values.
- The Supabase database is shared. Never drop, truncate, rename or edit existing tables or data there. Schema changes must be additive and idempotent (CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS) and are only run against Supabase after I say so. Test against the local database profile instead.
- Keep the stack as is: no new frameworks for the frontend. Add Python dependencies only when needed and list them in requirements.txt.
- Answers must stay grounded in the knowledge base. Never ask for or repeat passwords.
- Write code that matches the existing style. Add or update tests in tests/ for new logic.
- Do not write em dashes in code comments, docs or messages.

## Working style
- First read the relevant files and show me a short plan. Wait for my OK before large changes.
- When done: run the tests, give me a summary of changes, the manual steps to try it in the browser, and anything you were unsure about.
