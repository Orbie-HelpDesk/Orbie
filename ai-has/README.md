# AI-HAS: AI-Powered Enterprise Helpdesk & Workflow Automation System

Capstone project: Applied Generative AI for IT Solution Development.

## What runs today
- FastAPI backend (`backend/app/`) with a chat endpoint, `POST /chat`.
- Google Gemini through its OpenAI-compatible endpoint (chat and embeddings).
- PostgreSQL with pgvector, hosted on Supabase and shared by the team.
- Hybrid search (vector plus full-text, merged by rank) over the markdown files in `kb/`.
- A plain HTML, CSS and JavaScript chat page served at `/`.

If the best match is too far away or the model replies NO_ANSWER, the API returns a "not found" message with `escalate=true`.

## Planned (not built yet)
- n8n workflows (ticket creation, password reset webhooks)
- Guardrails (PII redaction, prompt injection checks)
- Intent classification and function calling
- Agent and admin dashboard
- Real ticket creation (the "Create a ticket" button is a placeholder)

## Quick start (Supabase)
```bash
cp .env.example .env      # fill in GEMINI_API_KEY and DATABASE_URL (ask the team lead)
docker compose up --build
```
- Chat page: http://localhost:8000
- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

Valid model names depend on your key. To list them:
```bash
docker compose run --rm backend python -m app.list_models
```

## Try it locally without touching Supabase
The `local-db` profile starts a throwaway Postgres with pgvector on host port 5433. Use the URL below only for these commands. Do not put it in a shared `.env`.

```bash
# 1. start the local database
docker compose --profile local-db up -d db

# 2. create the tables and seed demo users
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas \
  backend python -m app.init_db

# 3. index the knowledge base
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas \
  backend python -m app.ingest

# 4. run the app against it (same command as the normal service, with the URL overridden)
docker compose run --rm --service-ports \
  -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend
```
`-e` overrides the value in `.env`. To reset the local data: `docker compose --profile local-db down -v`.

`init_db` and `ingest` refuse to run unless the `DATABASE_URL` host is `db` or `localhost`. Passing `--allow-remote` overrides this, so only use it when the team lead has said to write to Supabase.

Ingest needs a valid `GEMINI_API_KEY` in `.env`, because it creates embeddings.

## Database schema
`backend/schema.sql` creates every table with `CREATE ... IF NOT EXISTS`, so it never changes existing data. `python -m app.init_db` applies it and seeds three demo users (employee, agent, admin at example.com).

**Note:** `schema.sql` was written from `models.py` and from how `ingest.py` and `rag.py` use `kb_chunks`. It must match the real Supabase definition, which the team lead needs to verify. Do not run `init_db` against Supabase until that is done.

## Tests
```bash
pip install -r backend/requirements.txt
pytest
```

## Structure
- `backend/`: FastAPI app (config, db, models, rag, ingest, init_db) and `schema.sql`
- `frontend/`: chat UI
- `kb/`: IT knowledge base SOPs (markdown)
- `docs/`: architecture, prompt taxonomy
- `tests/`: pytest tests
- `n8n/`: exported workflows (planned, empty)

## Progress
- [x] Week 1–2: architecture, schema, Docker, taxonomy
- [ ] Week 3–4: RAG pipeline
- [ ] Week 5–6: automation + UI
- [ ] Week 7: guardrails + QA
- [ ] Week 8: docs + defense
