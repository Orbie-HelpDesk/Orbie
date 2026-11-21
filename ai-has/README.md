# AI-HAS: AI-Powered Enterprise Helpdesk & Workflow Automation System

![CI](https://github.com/Orbie-HelpDesk/ITPE-FinalSum/actions/workflows/ci.yml/badge.svg)

The CI check (ruff, pytest, docker build) is advisory unless branch protection is enabled on `main`. The team lead should check whether the repo plan allows branch protection.

Capstone project: Applied Generative AI for IT Solution Development.

## What it does
- **Chat** (`/`): employees describe an IT problem and get an answer from **Orbie**, the assistant. Answers are grounded in the knowledge base in `kb/`, with citations. AI-HAS is the product name.
- **Guardrails:** every message is checked for prompt injection and length before it reaches the AI. Personal data (emails, phone numbers, card numbers, API keys, passwords) is redacted first.
- **Tickets:** when the answer is "not found", the "Create a ticket" button creates a real ticket. Ticket text is also PII-redacted.
- **Actions with confirmation:** Orbie can propose opening a ticket (with a mock Jira key from n8n) or sending a password reset link (mock Active Directory). It only proposes. A card with Confirm and Cancel appears, and nothing runs until you confirm. Every decision is written to `audit_logs`. See "Actions from chat" below.
- **Admin dashboard** (`/admin`): ticket KPIs, a ticket list with a status filter, and status changes (open, in progress, resolved).
- **Evaluation:** scripts that measure retrieval accuracy, deflection, escalation, guardrails and latency.

If the best match is too far away (distance above 0.5) or the model replies NO_ANSWER, the API returns a "not found" message with `escalate=true`.

## Stack
FastAPI (Python 3.12), SQLAlchemy 2, Google Gemini through its OpenAI-compatible endpoint (openai SDK), PostgreSQL with pgvector on Supabase, and a plain HTML, CSS and JavaScript frontend. No frameworks and no build step.

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Chat page |
| GET | `/admin` | Admin dashboard (no login yet) |
| POST | `/chat` | Guardrails, then hybrid search and answer. Returns `answer`, `citations`, `escalate`, `distances`, `guardrails`, `proposed_action` (null, or `{id, name, arguments, label}`) |
| POST | `/actions/confirm` | Body `{action_id, confirm}`. Runs (true) or drops (false) a proposed action. Only the owner can decide it, once. 403 other owner, 404 unknown, 409 already handled, 410 expired |
| POST | `/tickets` | Create a ticket from `summary` (3 to 500 chars) and `priority` |
| GET | `/tickets` | List tickets, optional `?status=` |
| PATCH | `/tickets/{id}` | Change status |
| GET | `/stats` | Ticket totals, by priority, average resolution hours |
| GET | `/health` | App and database status |
| GET | `/docs` | API docs |

Pages and static files are sent with `Cache-Control: no-cache`, so a rebuilt frontend is not hidden by an old cached copy.

## Security basics
- **Rate limits** per client IP, in memory: `POST /chat` 20 per minute, `POST /tickets` 10 per minute and `POST /actions/confirm` 20 per minute. Over the limit the API returns 429 with `Retry-After`. Set `RATE_LIMIT_ENABLED`, `RATE_LIMIT_CHAT_PER_MIN`, `RATE_LIMIT_TICKETS_PER_MIN` and `RATE_LIMIT_ACTIONS_PER_MIN` in `.env` (names and defaults are in `.env.example`). `RATE_LIMIT_ENABLED=false` switches all limits off, for example for a load test. The counters are per process and reset on restart. The limit uses the direct client IP, so behind a reverse proxy all users share one IP.
- **Security headers** on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
- **Safe errors:** an unexpected error returns `{"detail": "Something went wrong. Please try again."}` with status 500. The details only go to the server log, so no stack trace, database URL or key reaches the client. Normal 400, 404, 429 and 422 responses are unchanged.

## Quick start (Supabase)
```bash
cp .env.example .env      # fill in GEMINI_API_KEY and DATABASE_URL (ask the team lead)
docker compose up --build
```
- Chat page: http://localhost:8000
- Admin dashboard: http://localhost:8000/admin
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
`-e` overrides the value in `.env`. To look at the data: `docker compose exec db psql -U aihas -d aihas -c "select id, summary, priority, status from tickets order by id desc limit 5;"`. To reset the local data: `docker compose --profile local-db down -v`.

Ingest needs a valid `GEMINI_API_KEY` in `.env`, because it creates embeddings.

### Safety guard for scripts that write data
`init_db` and `ingest` refuse to run unless the `DATABASE_URL` host is `db` or `localhost`. The check reads the URL the same way SQLAlchemy does, so it also looks at `?host=` and `?hostaddr=`, and it refuses a missing or empty host (libpq would fall back to `PGHOST`). Its message never contains the URL or the password.

Passing `--allow-remote` overrides the guard, for example `python -m app.init_db --allow-remote`. Only use it when the team lead has said to write to Supabase.

## Database schema
`backend/schema.sql` creates every table with `CREATE ... IF NOT EXISTS`, so it never changes existing data. `python -m app.init_db` applies it and seeds three demo users (`employee@example.com`, `agent@example.com`, `admin@example.com`).

**Note:** `schema.sql` was written from `models.py` and from how `ingest.py` and `rag.py` use `kb_chunks`. It must match the real Supabase definition, which the team lead needs to verify. Do not run `init_db` against Supabase until that is done.

**New table:** `pending_actions` (additive, `create table if not exists`) holds proposed actions. It exists only in the local database so far. The team lead should review it in `schema.sql` before it is applied to Supabase. Until then, on Supabase, a proposal cannot be stored, so `/chat` logs the error and gives the normal answer. `audit_logs.actor` now holds `user:<id>`, not an email.

Tickets are owned by `employee@company.com`. If that user does not exist (for example on a database seeded by `init_db`), the first user with role `employee` is used and a warning is logged.

## Evaluation
Two question sets:
- **Generated** (`eval_data/questions.json`): written by Gemini from the articles, so it is easy.
- **Hard** (`eval_data/hard_questions.json`): 51 hand-written cases. Typos and slang, Taglish, one-word messages, two problems in one message, questions that need a different article, near-miss out-of-scope questions (HR, payroll, building access), prompt injections, PII, and harmless messages that must not be blocked. It was written before any results were seen and is not tuned to them.

`eval_run` reports, per set: retrieval accuracy, deflection, out-of-scope escalation, guardrail results and latency. It also scores each answered question for **faithfulness** (is every claim supported by the retrieved article) and **answer relevance** using the Gemini chat model as judge, lists the worst five, and runs a **threshold sweep** of `MAX_DISTANCE` from 0.30 to 0.70 with a recommendation. The recommendation is advice and is never applied automatically.

These commands call the Gemini API and use your quota. Run them against the local database (see above).
```bash
# see what would run and the most calls it could use, without calling anything
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas   backend python -m app.eval_run --set hard --limit 10 --dry-run

docker compose run --rm -e DATABASE_URL=... backend python -m app.eval_run --set generated
docker compose run --rm -e DATABASE_URL=... backend python -m app.eval_run --set hard --limit 10
docker compose run --rm -e DATABASE_URL=... backend python -m app.eval_run --set all
```
Options: `--set generated|hard|all` (default `generated`), `--limit N` (evenly spaced sample of retrieval questions; guardrail checks cost nothing and always run), `--no-judge` (skip the two judge calls per answer), `--pause` (seconds between calls, default 1), `--timeout` (seconds per call, default 90), `--resume`, `--dry-run`, `--report PATH`.

Each call is retried with exponential backoff, and every result is saved to `eval_data/.cache` (git-ignored) as soon as it is known. If a run stops, `--resume` reuses the saved results and only repeats questions that failed. Calls per question: 1 embedding, 1 chat (only if the best match is close enough) and 2 judge calls (only for answered questions). The report is written to `eval_data/report.md` with the date and a "How to read this" section.

`python -m app.eval_generate` regenerates the generated set from the knowledge base, and `python -m app.tune` prints distances for a few questions.

### Load test
```bash
docker compose run --rm -e DATABASE_URL=... backend python -m app.loadtest                       # mock mode, no Gemini, no database
docker compose run --rm -e DATABASE_URL=... backend python -m app.loadtest --mode retrieval-only -n 50 -c 5 --yes
python -m app.loadtest --mode full --url http://localhost:8000 -n 30 -c 3 --yes                  # against a running server
```
It sends `-n` requests to `POST /chat` with `-c` concurrent workers and reports requests per second, error rate, and median, p95 and p99 latency. The first lines of the output say which mode ran:
- **mock** (default): the app runs in-process with a stubbed answer step. No Gemini calls and no database. It measures the app's own overhead only.
- **retrieval-only**: real embedding and database search, no chat model. Spends one embedding call per request.
- **full**: real requests to a running server. Spends up to one embedding and one chat call per request. Start that server with `RATE_LIMIT_ENABLED=false`, or the rate limit answers 429 (counted separately).

The modes that spend quota refuse to run without `--yes`. The result is written as a marked "Load test" section of `report.md`, and `eval_run` keeps that section when it rewrites the report (`--no-report` skips it).

## n8n workflows (mock Jira and Active Directory)
Two workflows in `n8n/` back the actions the assistant can propose. Nothing real is called: the ticket workflow returns a made-up Jira key (`HELP-1234`) and the password workflow returns `{"status": "reset_link_sent"}`. No email is sent.

| File | Webhook path | Receives | Returns |
|---|---|---|---|
| `n8n/create-ticket.json` | `/webhook/create-ticket` | `{summary, priority, user_email}` | `{"key": "HELP-<number>"}` |
| `n8n/password-reset.json` | `/webhook/password-reset` | `{user_email}` | `{"status": "reset_link_sent"}` |

n8n is optional and only starts with its compose profile. It is published on `127.0.0.1:5678` only, so it is not reachable from other machines.

```bash
docker compose --profile n8n up -d n8n
```
1. Open http://localhost:5678 and create the local owner account (first start only).
2. Import: **Workflows > Add workflow > the ... menu > Import from file**, and pick `n8n/create-ticket.json`. Repeat for `n8n/password-reset.json`. (The `./n8n` folder is also mounted read-only at `/workflows` in the container.)
3. Open each workflow and switch **Active** on. Only active workflows answer the production URL `/webhook/...`.
4. Check it from your machine (the optional secret header is only needed if you set `N8N_WEBHOOK_SECRET`):
   ```bash
   curl -X POST http://localhost:5678/webhook/create-ticket -H "Content-Type: application/json" -d '{"summary":"Test","priority":"low","user_email":"a@b.com"}'
   ```

**Backend URL.** Inside the backend container `localhost` is the container itself, so use the compose service name: `N8N_WEBHOOK_URL=http://n8n:5678/webhook` (already in `.env.example`). The default in `config.py`, `http://localhost:5678/webhook`, is only right when the backend runs outside Docker.

**Settings** (names and placeholder values are in `.env.example`; never commit real values):
- `N8N_WEBHOOK_URL`: base URL of the webhooks.
- `N8N_WEBHOOK_SECRET`: optional. When set, the backend sends it in the `X-Webhook-Secret` header and both workflows answer 401 to any request without it. Set the same value for the `n8n` service and the backend (both read it from `.env`). Empty means no check.
- `N8N_ENCRYPTION_KEY`: optional key n8n uses for its stored credentials.
- `N8N_IMAGE_TAG`: optional n8n image version (default `1.70.3`).

Without the UI, import and activate from the command line (each file holds one workflow, so use `--separate` with the folder), then restart n8n:
```bash
docker compose exec n8n n8n import:workflow --separate --input=/workflows
docker compose exec n8n n8n update:workflow --all --active=true
docker compose --profile n8n restart n8n
```

Workflow data lives in the `n8ndata` volume. Re-export a changed workflow from the n8n editor (**...** menu > **Download**) into `n8n/`.

## Actions from chat
Flow: the user asks for something, Orbie proposes, the user confirms, the backend runs it through n8n.

1. `POST /chat` runs the usual guardrails and PII redaction. If the cleaned message looks like a request for an action (words such as ticket, reset my password, locked out), `rag.propose_action` sends it to the model with two tools, `create_ticket` and `request_password_reset`, using the `tools` parameter of the OpenAI-compatible Gemini endpoint. **This is one extra Gemini chat call for those messages only.** Other messages cost nothing extra. If the model calls a tool, `/chat` skips the knowledge base answer and returns `proposed_action`. If it does not (for example "how do I reset my password?"), the normal answer is returned, so that message used two chat calls in total.
2. The proposal is validated (known tool, summary length, priority, PII redacted, injection check) and stored in `pending_actions` with an id from `secrets.token_urlsafe`, its owner and a 10 minute expiry. The model never chooses the account: a password reset always applies to the owner.
3. The chat page shows a card with Confirm and Cancel. `POST /actions/confirm` receives only the id. What runs is what was stored, so a request cannot change it.
4. Deciding is one atomic `UPDATE ... WHERE id AND owner AND status = 'pending' AND not expired`, and the row count says if this call won. A double click, a replay or two tabs cannot run an action twice, and another user is rejected with 403.
5. Confirming a ticket creates it in our database first, then asks n8n for the Jira key and saves it in `tickets.external_ref`. If n8n is down the ticket still exists and the card says the Jira sync is pending. A password reset needs n8n, so it fails with a clear message if n8n is down. The proposal is used up either way, so the user asks again.
6. Confirm and cancel each write an `audit_logs` row: `actor` is `user:<id>` (never an email), `action` is `create_ticket` or `reset_password`, and `payload` has the decision, the outcome, the ticket id and priority. It holds no email, name or ticket text.

**If tool calling is unreliable.** If the tool call raises an error or comes back malformed (bad JSON, unknown tool), `propose_action` falls back to a small rule: a message that clearly asks to create or open a ticket, or to reset or unlock a password, and is not a "how, what, why..." question. The same confirm step still applies. This was checked end to end with a rejected model key, where the rule path produced the proposals. It has not been checked against a live Gemini tool call, so try it with a real key (see below).

**No login yet.** The user is always `employee@company.com` (or the first employee, with a logged warning), through `current_user_email` in `main.py`. That is the one function to change when real authentication is added. Until then the owner check separates users only once that function returns different users.

**Try it locally** (local database, n8n imported and active as described above):
```bash
docker compose --profile local-db up -d db
docker compose --profile n8n up -d n8n
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend python -m app.init_db
docker compose run --rm --service-ports -e N8N_WEBHOOK_URL=http://n8n:5678/webhook -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend
```
Then open http://localhost:8000 and type "please reset my password" or "create a ticket, my monitor flickers". Check the result with `docker compose exec db psql -U aihas -d aihas -c "select actor, action, payload from audit_logs order by id desc limit 5;"`. If you set `N8N_WEBHOOK_SECRET`, pass it to the backend with `-e N8N_WEBHOOK_SECRET=...` too.

## Tests
No Gemini calls and no real database are needed. Tests stub the database engine.

```bash
# local
pip install -r backend/requirements.txt
pytest

# or in Docker
docker compose run --rm -v "${PWD}/tests:/app/tests" backend sh -c "pip install -q pytest httpx && python -m pytest tests -q"
```
Covered: the n8n actions with a fake n8n (up, down, wrong secret, bad answers), proposals and `/actions/confirm` with an in-memory fake of the new tables (owner check, double confirm runs once, expiry, cancel, audit without personal data), tool calling with a fake model, `/health`, the models import, the local-db guard, ticket creation and its user fallback, the no-cache headers, the rate limits, the security headers and the safe 500.

### Lint and CI
```bash
pip install ruff==0.17.6
ruff check backend tests
```
GitHub Actions (`.github/workflows/ci.yml`) runs ruff, pytest and a `docker build` of `backend/` on every pull request and on pushes to `main`. It uses no secrets. The ruff version is pinned in `ruff.toml` and in the workflow, so change both together.

## Structure
- `backend/app/`: `main.py` (routes), `rag.py` (search and answer), `guardrails.py`, `tickets.py`, `actions.py` (n8n calls), `proposals.py` (propose, confirm, audit), `ingest.py`, `init_db.py`, `safety.py`, `eval_*.py`, `tune.py`
- `backend/schema.sql`: idempotent database schema
- `frontend/chat/` and `frontend/admin/`: the two pages
- `kb/`: IT knowledge base SOPs (markdown)
- `docs/`: architecture, prompt taxonomy
- `tests/`: pytest tests
- `n8n/`: exported workflows (mock Jira ticket, mock password reset)

## Not built yet
- Intent classification beyond the keyword check that decides when to offer tools
- Real Jira and Active Directory (the workflows are mocks)
- Saving conversations and messages to the database (chat history lives in the browser)
- Login and roles for the admin page

## Progress
- [x] Week 1–2: architecture, schema, Docker, taxonomy
- [x] Week 3–4: RAG pipeline
- [x] Week 5–6: ticket creation and admin dashboard
- [x] Week 7: guardrails and evaluation
- [ ] Week 8: docs + defense
