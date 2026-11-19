# AI-HAS: AI-Powered Enterprise Helpdesk & Workflow Automation System

Capstone project: Applied Generative AI for IT Solution Development.

## Quick start
```bash
cp .env.example .env      # add your OPENAI_API_KEY
docker compose up --build
```
- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health
- n8n: http://localhost:5678
- ChromaDB: http://localhost:8001

## Structure
- `backend/`: FastAPI app (config, db, models)
- `frontend/`: chat UI + admin dashboard (Week 5–6)
- `kb/`: IT knowledge base SOPs (markdown)
- `docs/`: architecture, prompt taxonomy
- `tests/`: synthetic test queries + eval scripts (Week 7)
- `n8n/`: exported workflows

## Progress
- [x] Week 1–2: architecture, schema, Docker, taxonomy
- [ ] Week 3–4: RAG pipeline
- [ ] Week 5–6: automation + UI
- [ ] Week 7: guardrails + QA
- [ ] Week 8: docs + defense
